"""Copilot agent — multi-turn tool-use agent for workflow building.

Uses the OpenAI Agents SDK with LiteLLM for multi-provider LLM support.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.experimentation.llm_prompt_config import LLMAPIHandler
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

import structlog
import yaml
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.block_goal_wrapping import wrap_block_goals
from skyvern.forge.sdk.copilot.context import COPILOT_RESPONSE_TYPES, AgentResult, CopilotContext, StructuredContext
from skyvern.forge.sdk.copilot.output_utils import extract_final_text, parse_final_response
from skyvern.forge.sdk.copilot.tracing_setup import _copilot_model_name, ensure_tracing_initialized, is_tracing_enabled
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
)
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.utils.strings import escape_code_fences

LOG = structlog.get_logger()

WORKFLOW_KNOWLEDGE_BASE_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "skyvern" / "workflow_knowledge_base.txt"
)

MAX_TURNS = 25
_USER_SUPPLIED_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")
_UNTESTED_DRAFT_REQUEST_RE = re.compile(
    r"\b(?:"
    r"without\s+testing|"
    r"without\s+(?:a\s+)?test(?:\s+run)?|"
    r"do\s+not\s+test|"
    r"don['’]?t\s+test|"
    r"skip\s+(?:the\s+)?test(?:ing)?|"
    r"no\s+test(?:ing)?|"
    r"untested\s+draft|"
    r"draft\s+only"
    r")\b",
    re.IGNORECASE,
)

_BLOCK_GOAL_CONTEXT_PREAMBLE = (
    "Interpret the latest user message in the context of the prior conversation. If it is a correction, "
    "refinement, or continuation, preserve the established website, actions, filters, and output requirements "
    "except where the latest message changes them."
)


async def _resolve_live_browser_session_id(
    chat_request: WorkflowCopilotChatRequest,
    organization_id: str,
) -> str | None:
    """Validate against a debug session for the same (org, workflow_permanent_id);
    return None on any failure so the caller falls back to auto-create."""
    requested = chat_request.browser_session_id
    if not requested:
        return None

    try:
        debug_session = await app.DATABASE.debug.get_debug_session_by_browser_session_id(
            browser_session_id=requested,
            organization_id=organization_id,
        )
        if debug_session is None:
            LOG.warning(
                "Copilot received an unknown browser_session_id; ignoring",
                organization_id=organization_id,
                requested_session_id=requested,
            )
            return None
        if debug_session.workflow_permanent_id != chat_request.workflow_permanent_id:
            LOG.warning(
                "Copilot browser_session_id is bound to a different workflow; ignoring",
                organization_id=organization_id,
                requested_session_id=requested,
                expected_wpid=chat_request.workflow_permanent_id,
                actual_wpid=debug_session.workflow_permanent_id,
            )
            return None

        # Trust the DB row over a CDP probe here — get_browser_state opens a
        # fresh Playwright connection. ensure_browser_session does the
        # attachability probe right before use so stale rows still recover.
        persistent = await app.PERSISTENT_SESSIONS_MANAGER.get_session(requested, organization_id)
        if persistent is None or is_final_status(persistent.status) or not persistent.browser_address:
            LOG.warning(
                "Copilot live browser session is not yet usable; falling back to auto-create",
                organization_id=organization_id,
                requested_session_id=requested,
                status=persistent.status if persistent else None,
                has_browser_address=bool(persistent.browser_address) if persistent else False,
            )
            return None

        LOG.info(
            "Copilot reusing live browser session",
            organization_id=organization_id,
            session_id=requested,
        )
        return requested
    except Exception as exc:
        LOG.warning(
            "Copilot live-session validation raised; falling back to auto-create",
            organization_id=organization_id,
            requested_session_id=requested,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None


def _format_chat_history(chat_history: list[WorkflowCopilotChatHistoryMessage]) -> str:
    if not chat_history:
        return ""
    lines = [f"{msg.sender}: {msg.content}" for msg in chat_history]
    return "\n".join(lines)


def _build_block_goal_main_goal(
    user_message: str,
    chat_history_text: str,
    global_llm_context: str | None,
) -> str:
    raw_current_message = (user_message or "").strip()
    if not raw_current_message:
        return ""
    current_message = escape_code_fences(raw_current_message)
    structured_context = StructuredContext.from_json_str(global_llm_context)
    context_sections: list[str] = []
    if structured_context.user_goal.strip():
        prior_goal = escape_code_fences(structured_context.user_goal.strip())
        context_sections.append(f"Prior high-level goal:\n{prior_goal}")
    if chat_history_text.strip():
        escaped_chat_history = escape_code_fences(chat_history_text.strip())
        context_sections.append(f"Recent chat history:\n{escaped_chat_history}")
    if not context_sections:
        return current_message

    return (
        _BLOCK_GOAL_CONTEXT_PREAMBLE
        + "\n\n"
        + "\n\n".join(context_sections)
        + f"\n\nLatest user message:\n{current_message}"
    )


def _build_system_prompt(
    tool_usage_guide: str,
    security_rules: str = "",
) -> str:
    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    return prompt_engine.load_prompt(
        template="workflow-copilot-agent",
        workflow_knowledge_base=workflow_knowledge_base,
        current_datetime=datetime.now(timezone.utc).isoformat(),
        tool_usage_guide=tool_usage_guide,
        security_rules=security_rules,
    )


def _build_user_context(
    workflow_yaml: str,
    chat_history_text: str,
    global_llm_context: str,
    debug_run_info_text: str,
    user_message: str,
) -> str:
    """Render untrusted context into the user message with code fencing.

    Every argument is treated as untrusted and passed through
    ``escape_code_fences`` before the template interpolates it into a
    triple-backtick block. Without this, a value containing a literal
    ``` would close the fence early and let the model see the rest as
    system-level content (the classic code-fence breakout). The old
    copilot path in ``workflow_copilot.py`` and ``feasibility_gate.py``
    both apply the same guard.
    """
    return prompt_engine.load_prompt(
        template="workflow-copilot-user",
        workflow_yaml=escape_code_fences(workflow_yaml or ""),
        chat_history=escape_code_fences(chat_history_text),
        global_llm_context=escape_code_fences(global_llm_context or ""),
        debug_run_info=escape_code_fences(debug_run_info_text),
        user_message=escape_code_fences(user_message),
    )


def _extract_user_supplied_credential_ids(user_message: str) -> list[str]:
    return list(dict.fromkeys(_USER_SUPPLIED_CREDENTIAL_ID_RE.findall(user_message or "")))


def _user_requests_untested_workflow_draft(user_message: str) -> bool:
    return bool(_UNTESTED_DRAFT_REQUEST_RE.search(user_message or ""))


def _missing_credential_ids_result(
    missing_credential_ids: list[str],
    global_llm_context: str | None,
) -> AgentResult:
    formatted_ids = ", ".join(f"`{credential_id}`" for credential_id in missing_credential_ids)
    id_word = "ID" if len(missing_credential_ids) == 1 else "IDs"
    was_word = "was" if len(missing_credential_ids) == 1 else "were"
    structured = StructuredContext.from_json_str(global_llm_context)
    structured.decisions_made.append(
        f"credential reference validation blocked missing ids: {', '.join(missing_credential_ids)}"
    )
    return AgentResult(
        user_response=(
            f"The credential {id_word} {formatted_ids} {was_word} not found in this organization. "
            "Please choose one of these paths: provide/select a valid credential ID, create the credential "
            "in the Credentials UI and return with its ID, or explicitly tell me to continue with an "
            "unvalidated draft workflow that will not be run until credentials are available."
        ),
        updated_workflow=None,
        global_llm_context=structured.to_json_str(),
        response_type="ASK_QUESTION",
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=True,
    )


def _credential_lookup_failed_result(global_llm_context: str | None) -> AgentResult:
    structured = StructuredContext.from_json_str(global_llm_context)
    structured.decisions_made.append("credential reference validation failed before workflow build")
    return AgentResult(
        user_response=(
            "I couldn't verify the supplied credential ID against this organization, so I can't build or run "
            "the workflow with it yet. Please provide/select a valid credential ID, create the credential in "
            "the Credentials UI and return with its ID, or explicitly tell me to continue with an unvalidated "
            "draft workflow that will not be run until credentials are available."
        ),
        updated_workflow=None,
        global_llm_context=structured.to_json_str(),
        response_type="ASK_QUESTION",
        workflow_yaml=None,
        workflow_was_persisted=False,
        clear_proposed_workflow=True,
    )


async def _credential_validation_result_for_user_message(
    *,
    user_message: str,
    organization_id: str,
    global_llm_context: str | None,
    allow_untested_workflow_draft: bool = False,
) -> AgentResult | None:
    if allow_untested_workflow_draft:
        return None

    credential_ids = _extract_user_supplied_credential_ids(user_message)
    if not credential_ids:
        return None

    try:
        existing_credentials = await app.DATABASE.credentials.get_credentials_by_ids(
            credential_ids,
            organization_id=organization_id,
        )
    except Exception:
        LOG.warning(
            "Copilot failed to validate user-supplied credential IDs",
            organization_id=organization_id,
            credential_ids=credential_ids,
            exc_info=True,
        )
        return _credential_lookup_failed_result(global_llm_context)

    found_ids = {credential.credential_id for credential in existing_credentials}
    missing_ids = [credential_id for credential_id in credential_ids if credential_id not in found_ids]
    if not missing_ids:
        return None
    return _missing_credential_ids_result(missing_ids, global_llm_context)


def _build_tool_usage_guide(tool_names_and_descriptions: list[tuple[str, str]]) -> str:
    if not tool_names_and_descriptions:
        return ""
    return "\n".join(
        f"- **{name}** — {description or 'No description provided.'}"
        for name, description in tool_names_and_descriptions
    )


def _is_explicit_false(value: Any) -> bool:
    # LLMs occasionally serialise JSON booleans as strings; coerce the common spellings.
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() in {"false", "no", "0"}


def _normalize_failure_reason(failure_reason: str | None) -> str:
    if not failure_reason:
        return "The workflow test run failed."

    normalized = failure_reason.split("Call log:", 1)[0].strip()
    normalized = " ".join(normalized.split())
    if len(normalized) > 240:
        normalized = normalized[:237].rstrip() + "..."
    return normalized or "The workflow test run failed."


_FAILURE_FOLLOW_UP = {
    "NAVIGATION_FAILURE": " Can you confirm the URL is correct?",
    "PROXY_ERROR": " Want me to retry with a different proxy location?",
    "PAGE_LOAD_TIMEOUT": " Can you confirm the URL and try again in a moment?",
    "ANTI_BOT_DETECTION": " Want me to retry with a different proxy location?",
    "AUTH_FAILURE": " The site rejected the login — is the stored password still valid?",
    "CREDENTIAL_ERROR": " I couldn't find a credential to use — can you link one in Settings?",
}


def _rewrite_failed_test_response(user_response: str, ctx: CopilotContext) -> str:
    has_keepable_draft = ctx.last_workflow is not None and bool(ctx.last_workflow_yaml)
    keep_draft_affordance = " Keep the draft to iterate on, or discard." if has_keepable_draft else ""

    if ctx.last_test_ok is False and ctx.last_update_block_count is not None:
        if ctx.last_update_block_count <= 0:
            draft_phrase = "a draft workflow"
        else:
            block_word = "block" if ctx.last_update_block_count == 1 else "blocks"
            draft_phrase = f"a draft workflow with {ctx.last_update_block_count} {block_word}"

        failure_summary = _normalize_failure_reason(ctx.last_test_failure_reason)
        follow_up = _FAILURE_FOLLOW_UP.get(ctx.last_failure_category_top or "", "")
        return (
            f"I created {draft_phrase} and tested it, but the test failed. "
            f"Failure: {failure_summary}.{follow_up}{keep_draft_affordance}"
        )

    if ctx.last_test_ok is None and ctx.last_update_block_count is not None and ctx.last_workflow is not None:
        if ctx.allow_untested_workflow_draft:
            return (
                "I drafted the workflow without testing it, as requested. "
                "You can accept it to save, but it has not been verified end-to-end."
            )
        if has_keepable_draft:
            return (
                "I drafted an update but wasn't able to verify it this turn. "
                "Keep the draft to iterate on it manually, or discard."
            )
        return (
            "I drafted an update but wasn't able to verify it this turn. "
            "Could you share more context about what you'd like me to do?"
        )

    return user_response


def _shape_ask_question_response(user_response: str, ctx: CopilotContext) -> str:
    from skyvern.forge.sdk.copilot.enforcement import build_probable_site_block_user_question

    site_block_question = build_probable_site_block_user_question(ctx)
    if site_block_question is not None:
        return site_block_question
    return user_response


def _verified_workflow_or_none(ctx: CopilotContext) -> tuple[Any, str | None]:
    """Surface a proposal only when it passed a test this turn AND yaml is on hand."""
    if ctx.last_workflow is not None and ctx.last_workflow_yaml and ctx.last_test_ok is True:
        return ctx.last_workflow, ctx.last_workflow_yaml
    return None, None


def _build_exit_result(
    ctx: CopilotContext,
    user_response: str,
    global_llm_context: str | None,
    cancelled: bool = False,
) -> AgentResult:
    """AgentResult for agent-loop exits that don't go through ``_translate_to_agent_result``."""
    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    return AgentResult(
        user_response=user_response,
        updated_workflow=verified_workflow,
        global_llm_context=global_llm_context,
        workflow_yaml=verified_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
        total_tokens=ctx.total_tokens_used,
        cancelled=cancelled,
    )


_TIMEOUT_REPLY_DEFAULT = "I ran out of time processing your request. Here's what I have so far."
_TIMEOUT_REPLY_UNVALIDATED = (
    "I ran out of time before I could finish testing. I have a draft workflow you can keep — "
    "accept it to save (note: it hasn't been verified end-to-end), or discard."
)
_TIMEOUT_REPLY_TESTED = "I ran out of time, but I have a tested draft for you. Accept it to save, or discard."

_MAX_TURNS_REPLY_DEFAULT = "I've reached the maximum number of steps. Here's what I have so far."
_MAX_TURNS_REPLY_UNVALIDATED = (
    "I've reached the maximum number of steps before I could finish testing. I have a draft "
    "workflow you can keep — accept it to save (note: it hasn't been verified end-to-end), or discard."
)
_MAX_TURNS_REPLY_TESTED = (
    "I've reached the maximum number of steps, but I have a tested draft for you. Accept it to save, or discard."
)
_UNEXPECTED_ERROR_REPLY_DEFAULT = "An unexpected error occurred. Please try again."
_UNEXPECTED_ERROR_REPLY_UNVALIDATED = (
    "I hit an unexpected issue before I could finish testing. I have a draft workflow you can keep — "
    "accept it to save (note: it hasn't been verified end-to-end), or discard."
)
_UNEXPECTED_ERROR_REPLY_TESTED = (
    "I hit an unexpected issue, but I have a tested draft for you. Accept it to save, or discard."
)
_CANCEL_REPLY_DEFAULT = "Cancelled by user."
_CANCEL_REPLY_UNVALIDATED = (
    "Cancelled. I have a draft workflow you can keep — accept it to save "
    "(note: it hasn't been verified end-to-end), or discard."
)
_CANCEL_REPLY_TESTED = "Cancelled. I have a tested draft for you. Accept it to save, or discard."


def _build_wip_exit_result(
    ctx: CopilotContext,
    global_llm_context: str | None,
    *,
    default_reply: str,
    unvalidated_reply: str,
    tested_reply: str,
    cancelled: bool = False,
) -> AgentResult:
    """Selected non-success exits surface the most recent successfully parsed workflow."""
    # When an unverified edit/run has overwritten ``last_workflow`` since the
    # last verified shape, prefer the verified shape. ``unvalidated=True``
    # triggers the route's rollback so auto-accept does not silently keep the
    # failed/in-flight shape.
    if (
        ctx.last_good_workflow is not None
        and ctx.last_good_workflow_yaml
        and ctx.last_workflow is not ctx.last_good_workflow
        and not ctx.last_test_suspicious_success
    ):
        return AgentResult(
            user_response=tested_reply,
            updated_workflow=ctx.last_good_workflow,
            global_llm_context=global_llm_context,
            workflow_yaml=ctx.last_good_workflow_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            total_tokens=ctx.total_tokens_used,
            unvalidated=True,
            cancelled=cancelled,
        )
    if (
        ctx.last_workflow is not None
        and ctx.last_workflow_yaml
        and ctx.last_test_ok is not False
        and not ctx.last_test_suspicious_success
    ):
        unvalidated = ctx.last_test_ok is not True
        reply = unvalidated_reply if unvalidated else tested_reply
        return AgentResult(
            user_response=reply,
            updated_workflow=ctx.last_workflow,
            global_llm_context=global_llm_context,
            workflow_yaml=ctx.last_workflow_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            total_tokens=ctx.total_tokens_used,
            unvalidated=unvalidated,
            cancelled=cancelled,
        )
    return _build_exit_result(ctx, default_reply, global_llm_context, cancelled=cancelled)


def _build_timeout_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_TIMEOUT_REPLY_DEFAULT,
        unvalidated_reply=_TIMEOUT_REPLY_UNVALIDATED,
        tested_reply=_TIMEOUT_REPLY_TESTED,
    )


def _build_cancelled_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    if ctx.copilot_total_timeout_exceeded:
        LOG.info("Copilot cancellation resolved as total timeout")
        return _build_timeout_exit_result(ctx, global_llm_context)
    return _build_cancel_exit_result(ctx, global_llm_context)


def _build_max_turns_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_MAX_TURNS_REPLY_DEFAULT,
        unvalidated_reply=_MAX_TURNS_REPLY_UNVALIDATED,
        tested_reply=_MAX_TURNS_REPLY_TESTED,
    )


def _build_unexpected_error_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_UNEXPECTED_ERROR_REPLY_DEFAULT,
        unvalidated_reply=_UNEXPECTED_ERROR_REPLY_UNVALIDATED,
        tested_reply=_UNEXPECTED_ERROR_REPLY_TESTED,
    )


def _build_cancel_exit_result(ctx: CopilotContext, global_llm_context: str | None) -> AgentResult:
    return _build_wip_exit_result(
        ctx,
        global_llm_context,
        default_reply=_CANCEL_REPLY_DEFAULT,
        unvalidated_reply=_CANCEL_REPLY_UNVALIDATED,
        tested_reply=_CANCEL_REPLY_TESTED,
        cancelled=True,
    )


def _translate_to_agent_result(
    result: RunResultStreaming,
    ctx: CopilotContext,
    global_llm_context: str | None,
    chat_request: WorkflowCopilotChatRequest,
    organization_id: str,
) -> AgentResult:
    # Deferred tools.py imports here and below: tools.py -> routes.workflow_copilot -> this module (circular at import time).
    from skyvern.forge.sdk.copilot.tools import _process_workflow_yaml

    text = extract_final_text(result)
    if not text:
        text = '{"type": "REPLY", "user_response": "I\'m not sure how to help with that. Could you rephrase?"}'

    action_data = parse_final_response(text)
    user_response = action_data.get("user_response") or "Done."

    resp_type = action_data.get("type", "REPLY")
    if resp_type not in COPILOT_RESPONSE_TYPES:
        resp_type = "REPLY"

    last_workflow = ctx.last_workflow
    last_workflow_yaml = ctx.last_workflow_yaml

    if resp_type == "REPLACE_WORKFLOW":
        LOG.warning("Agent used inline REPLACE_WORKFLOW instead of update_workflow tool")
        workflow_yaml = action_data.get("workflow_yaml", "")
        if workflow_yaml:
            # REPLACE_WORKFLOW bypasses _update_workflow, so the post-emission
            # reject has to run here too. Skip processing on detection; leave
            # last_workflow / last_workflow_yaml at their pre-REPLACE values so
            # the rejected YAML does not latch onto ctx.
            from skyvern.forge.sdk.copilot.tools import (
                _banned_block_reject_message,
                _detect_new_banned_blocks,
                _detect_stale_block_metadata,
                _record_banned_block_reject_span,
                _stale_block_metadata_message,
                _timing_only_challenge_wait_reject_message,
            )

            wait_block_error = _timing_only_challenge_wait_reject_message(ctx, workflow_yaml)
            if wait_block_error:
                user_response = f"{user_response}\n\n(Note: {wait_block_error})"
                ctx.last_test_ok = None
                workflow_yaml = ""
            banned_items = _detect_new_banned_blocks(workflow_yaml, ctx.last_workflow_yaml)
            if banned_items:
                _record_banned_block_reject_span("replace_workflow_inline", banned_items)
                user_response = f"{user_response}\n\n(Note: {_banned_block_reject_message(banned_items)})"
                workflow_yaml = ""
            stale_metadata = _detect_stale_block_metadata(workflow_yaml, ctx.last_workflow_yaml or ctx.workflow_yaml)
            if stale_metadata:
                user_response = f"{user_response}\n\n(Note: {_stale_block_metadata_message(stale_metadata)})"
                ctx.last_test_ok = None
                workflow_yaml = ""
        if workflow_yaml:
            block_goal_main_goal = ctx.block_goal_main_goal or ctx.user_message
            if block_goal_main_goal:
                workflow_yaml = wrap_block_goals(workflow_yaml, block_goal_main_goal)
            else:
                LOG.warning("REPLACE_WORKFLOW inline path missing block-goal context; skipping block-goal wrap")
            try:
                last_workflow = _process_workflow_yaml(
                    workflow_id=chat_request.workflow_id,
                    workflow_permanent_id=chat_request.workflow_permanent_id,
                    organization_id=organization_id,
                    workflow_yaml=workflow_yaml,
                )
                last_workflow_yaml = workflow_yaml
            except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
                LOG.warning("Failed to process final workflow YAML", error=str(e))
                user_response = (
                    f"{user_response}\n\n"
                    f"(Note: The proposed workflow had a validation error: {str(e)[:200]}. "
                    f"Please ask me to fix it.)"
                )

    # Inline REPLACE_WORKFLOW bypasses _update_workflow, so ctx.last_workflow
    # is whatever the tool layer last saw. Write the REPLACE candidate onto
    # ctx and invalidate any prior passing test: the REPLACE yaml itself was
    # never run, so a leftover ``last_test_ok is True`` from an earlier tested
    # (but different) yaml must not promote this untested one.
    if resp_type == "REPLACE_WORKFLOW" and last_workflow is not ctx.last_workflow:
        ctx.last_workflow = last_workflow
        ctx.last_workflow_yaml = last_workflow_yaml
        ctx.last_test_ok = None

    # An unverified edit/run sits in ``last_workflow`` after a recorded
    # failure — surface the verified prior shape and skip the failure rewrite
    # (which would describe the failed-shape block count).
    salvaged_reply = (
        resp_type == "REPLY"
        and ctx.last_good_workflow is not None
        and ctx.last_good_workflow_yaml
        and ctx.last_workflow is not ctx.last_good_workflow
        and bool(ctx.last_failed_workflow_yaml or ctx.last_test_ok is False)
        and not ctx.last_test_suspicious_success
    )

    # ASK_QUESTION replies carry a specific clarifying question — often the
    # "stop and ask" unblocker the system prompt now requires when the agent
    # cannot test. The generic rewrite would replace it with a vague
    # "Could you share more context", so skip it for ASK_QUESTION (and for
    # salvaged replies, which already describe the verified prefix).
    if resp_type == "ASK_QUESTION":
        user_response = _shape_ask_question_response(str(user_response), ctx)
    elif not salvaged_reply:
        user_response = _rewrite_failed_test_response(str(user_response), ctx)
    verified_workflow, verified_yaml = _verified_workflow_or_none(ctx)
    # Default-true preserves backwards-compat with stale prompts and missing fields.
    agent_admits_incomplete = _is_explicit_false(action_data.get("goal_reached"))

    last_workflow = None
    last_workflow_yaml = None
    unvalidated = False
    if verified_workflow is not None and not agent_admits_incomplete:
        last_workflow, last_workflow_yaml = verified_workflow, verified_yaml
    elif salvaged_reply:
        last_workflow, last_workflow_yaml = ctx.last_good_workflow, ctx.last_good_workflow_yaml
        unvalidated = True
    elif resp_type == "REPLY" and ctx.last_workflow is not None and ctx.last_workflow_yaml:
        # Failures are often environmental (captcha, transient block); surface the draft so the user can keep iterating.
        last_workflow = ctx.last_workflow
        last_workflow_yaml = ctx.last_workflow_yaml
        unvalidated = True

    # ASK_QUESTION blocks on user input — never surface a verified workflow
    # under it; auto_accept would silently apply a stepping-stone partial.
    if resp_type == "ASK_QUESTION":
        last_workflow = None
        last_workflow_yaml = None

    llm_context_raw = action_data.get("global_llm_context")
    structured = StructuredContext.from_json_str(global_llm_context)
    if isinstance(llm_context_raw, dict):
        try:
            structured = StructuredContext.model_validate(llm_context_raw)
        except Exception:
            pass
    elif isinstance(llm_context_raw, str):
        structured = StructuredContext.from_json_str(llm_context_raw)
    structured.merge_turn_summary(ctx.tool_activity)
    enriched_context = structured.to_json_str()

    return AgentResult(
        user_response=str(user_response),
        updated_workflow=last_workflow,
        global_llm_context=enriched_context or None,
        response_type=resp_type,
        workflow_yaml=last_workflow_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
        total_tokens=ctx.total_tokens_used,
        clear_proposed_workflow=resp_type == "ASK_QUESTION",
        unvalidated=unvalidated,
    )


def _build_feasibility_clarification_result(
    question: str,
    rationale: str | None,
    user_message: str,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
) -> AgentResult:
    """Construct an AgentResult for the feasibility-gate fast-path.

    Preserves structured cross-turn context, sets user_goal from the
    classifier's rationale (or the raw user message as a fallback), and
    appends a decisions_made entry so a follow-up turn can see that a
    clarification was already asked and return ``proceed`` instead of
    re-asking.
    """
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    if not structured.user_goal:
        structured.user_goal = (rationale or user_message)[:300]
    structured.decisions_made.append(f"feasibility-gate clarification asked: {question}")
    enriched_context = structured.to_json_str()

    return AgentResult(
        user_response=question,
        updated_workflow=None,
        global_llm_context=enriched_context,
        response_type="ASK_QUESTION",
        workflow_yaml=prior_workflow_yaml or None,
        workflow_was_persisted=False,
        clear_proposed_workflow=True,
    )


async def run_copilot_agent(
    stream: EventSourceStream,
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
    llm_api_handler: LLMAPIHandler | None,
    api_key: str | None = None,
    security_rules: str = "",
) -> AgentResult:
    allow_untested_workflow_draft = _user_requests_untested_workflow_draft(chat_request.message)
    credential_validation_result = await _credential_validation_result_for_user_message(
        user_message=chat_request.message,
        organization_id=organization_id,
        global_llm_context=global_llm_context,
        allow_untested_workflow_draft=allow_untested_workflow_draft,
    )
    if credential_validation_result is not None:
        return credential_validation_result

    # Preflight feasibility classifier — fires on every turn so mid-session pivots
    # to impossible targets are caught the same as first-turn structural mismatches.
    from skyvern.forge.sdk.copilot.feasibility_gate import run_feasibility_gate

    feasibility_verdict = await run_feasibility_gate(
        user_message=chat_request.message,
        workflow_yaml=chat_request.workflow_yaml or "",
        chat_history=_format_chat_history(chat_history),
        global_llm_context=global_llm_context or "",
        handler=llm_api_handler,
    )
    if feasibility_verdict.verdict == "ask_clarification" and feasibility_verdict.question:
        return _build_feasibility_clarification_result(
            question=feasibility_verdict.question,
            rationale=feasibility_verdict.rationale,
            user_message=chat_request.message,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
        )

    try:
        from agents import Agent, trace
        from agents.exceptions import MaxTurnsExceeded
        from agents.mcp import MCPServerManager
    except ModuleNotFoundError as e:
        if e.name == "agents":
            LOG.error(
                "OpenAI Agents SDK dependency missing",
                error=str(e),
                workflow_permanent_id=chat_request.workflow_permanent_id,
            )
            return AgentResult(
                user_response=(
                    "Copilot backend is missing the OpenAI Agents SDK dependency. "
                    "Rebuild or redeploy the backend image so `openai-agents` is installed."
                ),
                updated_workflow=None,
                global_llm_context=global_llm_context,
                workflow_yaml=chat_request.workflow_yaml or None,
            )
        raise

    from skyvern.cli.mcp_tools import mcp as skyvern_mcp
    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotNonRetriableNavError,
        CopilotTotalTimeoutError,
        run_with_enforcement,
    )
    from skyvern.forge.sdk.copilot.hooks import CopilotRunHooks
    from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
    from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config
    from skyvern.forge.sdk.copilot.session_factory import create_copilot_session
    from skyvern.forge.sdk.copilot.tools import (
        NATIVE_TOOLS,
        _build_skyvern_mcp_overlays,
        get_skyvern_mcp_alias_map,
    )

    validated_browser_session_id = await _resolve_live_browser_session_id(chat_request, organization_id)
    chat_history_text = _format_chat_history(chat_history)

    ctx = CopilotContext(
        organization_id=organization_id,
        workflow_id=chat_request.workflow_id,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_yaml=chat_request.workflow_yaml or "",
        browser_session_id=validated_browser_session_id,
        stream=stream,
        api_key=api_key,
        user_message=chat_request.message,
        allow_untested_workflow_draft=allow_untested_workflow_draft,
        block_goal_main_goal=_build_block_goal_main_goal(
            user_message=chat_request.message,
            chat_history_text=chat_history_text,
            global_llm_context=global_llm_context,
        ),
        workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
    )

    model_name, run_config, llm_key, supports_vision = resolve_model_config(llm_api_handler)
    ctx.supports_vision = supports_vision
    ensure_tracing_initialized()

    alias_map = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()

    mcp_server = SkyvernOverlayMCPServer(
        transport=skyvern_mcp,
        overlays=overlays,
        alias_map=alias_map,
        allowlist=frozenset(alias_map.values()),
        context_provider=lambda: ctx,
    )

    tool_info: list[tuple[str, str]] = [(tool.name, tool.description or "") for tool in NATIVE_TOOLS]
    tool_info.extend((name, overlay.description or "") for name, overlay in overlays.items())

    tool_usage_guide = _build_tool_usage_guide(tool_info)
    system_prompt = _build_system_prompt(
        tool_usage_guide=tool_usage_guide,
        security_rules=security_rules,
    )

    agent = Agent(
        name="workflow-copilot",
        instructions=system_prompt,
        tools=list(NATIVE_TOOLS),
        mcp_servers=[mcp_server],
        model=model_name,
    )

    user_message = _build_user_context(
        workflow_yaml=chat_request.workflow_yaml or "",
        chat_history_text=chat_history_text,
        global_llm_context=global_llm_context or "",
        debug_run_info_text=debug_run_info_text,
        user_message=chat_request.message,
    )

    LOG.info(
        "Starting copilot agent loop",
        workflow_permanent_id=chat_request.workflow_permanent_id,
        user_message_len=len(user_message),
        llm_key=llm_key,
    )

    trace_context: Any = contextlib.nullcontext()
    if is_tracing_enabled():
        trace_context = trace(
            workflow_name="Copilot workflow",
            group_id=chat_request.workflow_copilot_chat_id,
            metadata={
                "workflow_permanent_id": chat_request.workflow_permanent_id,
                "organization_id": organization_id,
                "llm_key": llm_key,
                "user_message_len": str(len(user_message)),
            },
        )

    chat_id = chat_request.workflow_copilot_chat_id or chat_request.workflow_permanent_id
    session = create_copilot_session(chat_id)
    model_token = _copilot_model_name.set(model_name)
    try:
        with trace_context:
            try:
                async with MCPServerManager([mcp_server]) as manager:
                    agent.mcp_servers = list(manager.active_servers)
                    result = await run_with_enforcement(
                        agent=agent,
                        initial_input=user_message,
                        ctx=ctx,
                        stream=stream,
                        max_turns=MAX_TURNS,
                        hooks=CopilotRunHooks(ctx),
                        run_config=run_config,
                        session=session,
                    )
                return _translate_to_agent_result(
                    result,
                    ctx,
                    global_llm_context,
                    chat_request,
                    organization_id,
                )
            except asyncio.CancelledError:
                # Re-raising would leave the route with ``agent_result is None``
                # and skip its ``workflow_was_persisted`` rollback decision.
                LOG.info("Copilot run cancelled")
                return _build_cancelled_exit_result(ctx, global_llm_context)
            except MaxTurnsExceeded:
                return _build_max_turns_exit_result(ctx, global_llm_context)
            except CopilotTotalTimeoutError:
                return _build_timeout_exit_result(ctx, global_llm_context)
            except CopilotNonRetriableNavError as exc:
                LOG.warning(
                    "Copilot run halted on non-retriable navigation error",
                    url=exc.url,
                    error_message=exc.error_message,
                    organization_id=organization_id,
                )
                # Non-retriable nav errors prove the current workflow doesn't
                # work; zero the proposal even if other tools succeeded.
                return AgentResult(
                    user_response=(
                        f"The target URL could not be reached. Error: {exc.error_message}. "
                        "Please verify the URL and try again."
                    ),
                    updated_workflow=None,
                    global_llm_context=global_llm_context,
                    workflow_yaml=None,
                    workflow_was_persisted=ctx.workflow_persisted,
                    total_tokens=ctx.total_tokens_used,
                )
    except Exception as e:
        LOG.error("Copilot agent error", error=str(e), exc_info=True)
        return _build_unexpected_error_exit_result(ctx, global_llm_context)
    finally:
        _copilot_model_name.reset(model_token)
        session.close()
