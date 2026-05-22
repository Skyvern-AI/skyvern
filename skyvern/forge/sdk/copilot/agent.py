"""Copilot agent — multi-turn tool-use agent for workflow building.

Uses the OpenAI Agents SDK with LiteLLM for multi-provider LLM support.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentelemetry import trace as otel_trace

if TYPE_CHECKING:
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.experimentation.llm_prompt_config import LLMAPIHandler
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream
    from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

import structlog
import yaml
from litellm.exceptions import NotFoundError as LiteLLMNotFoundError
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.context import COPILOT_RESPONSE_TYPES, AgentResult, CopilotContext, StructuredContext
from skyvern.forge.sdk.copilot.output_policy import (
    UNVALIDATED_DISCLOSURE_PHRASES,
    WORKFLOW_PRESENT_SENTINEL,
    CopilotOutputKind,
    OutputPolicyReason,
    OutputPolicyVerdict,
    build_output_policy_diagnostics,
    derive_output_kind,
    evaluate_output_policy,
    hard_block_output_policy_verdict,
    normalize_response_scaffolding,
    output_policy_verdict_from_trace_data,
    output_policy_verdict_to_trace_data,
    url_origin,
)
from skyvern.forge.sdk.copilot.output_utils import (
    extract_final_text,
    parse_final_response,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CREDENTIAL_DEFERRED_DRAFT_REASONS,
    RAW_SECRET_REFUSAL_SENTINEL,
    RequestPolicy,
    build_request_policy,
    redact_raw_secrets_for_prompt,
)
from skyvern.forge.sdk.copilot.tracing_setup import _copilot_model_name, ensure_tracing_initialized, is_tracing_enabled
from skyvern.forge.sdk.copilot.turn_context import TurnContextAssembler, TurnContextInputs, TurnContextPacket
from skyvern.forge.sdk.copilot.turn_intent import (
    NO_MUTATION_TURN_INTENT_MODES,
    TurnIntent,
    TurnIntentMode,
    build_turn_intent,
)
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatSender,
)
from skyvern.forge.sdk.trace import apply_context_attrs
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.utils.strings import escape_code_fences
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()

WORKFLOW_KNOWLEDGE_BASE_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "skyvern" / "workflow_knowledge_base.txt"
)

_COPILOT_TURN_SPAN_NAME = "copilot.turn"
_USER_MESSAGE_PREVIEW_MAX_CHARS = 40


def _build_user_message_preview(message: str) -> str:
    flattened = (message or "").replace("\r", " ").replace("\n", " ").strip()
    redacted = redact_raw_secrets_for_prompt(flattened)
    if len(redacted) <= _USER_MESSAGE_PREVIEW_MAX_CHARS:
        return redacted
    return redacted[: _USER_MESSAGE_PREVIEW_MAX_CHARS - 1] + "…"


def _derive_turn_index(
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    explicit: int | None,
) -> int:
    # `chat_history` may be a truncated tail of the full message log, so this
    # fallback can undercount long sessions; prefer the explicit count.
    if explicit is not None:
        return explicit
    return sum(1 for m in chat_history if m.sender == WorkflowCopilotChatSender.USER) + 1


@contextlib.contextmanager
def _copilot_turn_span(
    *,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    turn_index: int | None,
) -> Iterator[None]:
    tracer = otel_trace.get_tracer("skyvern")
    with tracer.start_as_current_span(_COPILOT_TURN_SPAN_NAME) as span:
        span.set_attribute("skyvern.span.role", "wrapper")
        span.set_attribute("copilot.turn_index", _derive_turn_index(chat_history, turn_index))
        preview = _build_user_message_preview(chat_request.message)
        if preview:
            span.set_attribute("copilot.user_message_preview", preview)
        if chat_request.workflow_copilot_chat_id:
            span.set_attribute("copilot.session_id", chat_request.workflow_copilot_chat_id)
        if chat_request.workflow_permanent_id:
            span.set_attribute("workflow_permanent_id", chat_request.workflow_permanent_id)
        apply_context_attrs(span)
        yield


def _resolve_request_policy_handler(fallback_handler: Any) -> Any:
    with contextlib.suppress(RuntimeError, AttributeError):
        return app.WORKFLOW_COPILOT_FAST_LLM_API_HANDLER or fallback_handler
    return fallback_handler


@dataclass(frozen=True)
class RequestPolicyGuardrailInputs:
    user_message: str
    workflow_yaml: str
    chat_history_text: str
    chat_history_messages: list[WorkflowCopilotChatHistoryMessage]
    global_llm_context: str
    organization_id: str
    handler: Any
    previous_user_message: str | None = None
    workflow_id: str | None = None
    workflow_permanent_id: str | None = None
    workflow_run_id: str | None = None
    browser_session_id: str | None = None


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
    return escape_code_fences(raw_current_message)


def _request_policy_agent_inputs(
    policy: RequestPolicy,
    *,
    user_message: str,
    chat_history_text: str,
    previous_user_message: str | None,
) -> tuple[str, str]:
    if policy.testing_intent == "skip_test" and len(user_message) < 160 and previous_user_message:
        return (
            f"{user_message}\n\nDraft the workflow requested earlier:\n"
            f"{redact_raw_secrets_for_prompt(previous_user_message)}",
            "",
        )
    return user_message, chat_history_text


def _store_request_policy_on_context(
    ctx: CopilotContext,
    policy: RequestPolicy,
    policy_inputs: RequestPolicyGuardrailInputs,
) -> None:
    agent_user_message, policy_chat_history_text = _request_policy_agent_inputs(
        policy,
        user_message=policy_inputs.user_message,
        chat_history_text=policy_inputs.chat_history_text,
        previous_user_message=policy_inputs.previous_user_message,
    )
    ctx.request_policy = policy
    ctx.allow_untested_workflow_draft = policy.testing_intent == "skip_test"
    ctx.user_message = agent_user_message
    ctx.block_goal_main_goal = _build_block_goal_main_goal(
        user_message=agent_user_message,
        chat_history_text=policy_chat_history_text,
        global_llm_context=policy_inputs.global_llm_context,
    )
    ctx.turn_intent = build_turn_intent(
        user_message=policy_inputs.user_message,
        workflow_yaml=policy_inputs.workflow_yaml,
        chat_history=policy_inputs.chat_history_messages,
        global_llm_context=policy_inputs.global_llm_context,
        request_policy=policy,
        workflow_id=policy_inputs.workflow_id,
        workflow_permanent_id=policy_inputs.workflow_permanent_id,
        workflow_run_id=policy_inputs.workflow_run_id,
        browser_session_id=policy_inputs.browser_session_id,
    )


def _turn_intent_log_fields(intent: TurnIntent | None) -> dict[str, Any]:
    if not isinstance(intent, TurnIntent):
        return {}
    return {f"turn_intent_{key}": value for key, value in intent.to_trace_data().items()}


def _turn_intent_trace_fields(intent: TurnIntent | None) -> dict[str, str]:
    return {key: str(value) for key, value in _turn_intent_log_fields(intent).items()}


def _turn_context_log_fields(packet: TurnContextPacket | None) -> dict[str, Any]:
    if not isinstance(packet, TurnContextPacket):
        return {}
    return {f"turn_context_{key}": value for key, value in packet.to_trace_data().items()}


def _turn_context_trace_fields(packet: TurnContextPacket | None) -> dict[str, str]:
    return {key: str(value) for key, value in _turn_context_log_fields(packet).items()}


def _store_turn_context_packet_on_context(
    ctx: CopilotContext,
    *,
    request_policy: RequestPolicy,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    debug_run_info_text: str,
    prior_copilot_workflow_yaml: str | None,
) -> None:
    if not isinstance(ctx.turn_intent, TurnIntent):
        return
    ctx.turn_context_packet = TurnContextAssembler().assemble(
        TurnContextInputs(
            turn_intent=ctx.turn_intent,
            request_policy=request_policy,
            user_message=chat_request.message,
            workflow_yaml=chat_request.workflow_yaml or "",
            prior_workflow_yaml=prior_copilot_workflow_yaml or "",
            chat_history=chat_history,
            debug_run_info_text=debug_run_info_text,
        )
    )


def _build_system_prompt(
    tool_usage_guide: str,
    config: CopilotConfig | None = None,
    security_rules: str | None = None,
) -> str:
    copilot_config = config or CopilotConfig(security_rules=security_rules or "")
    template = copilot_config.prompt_template.removesuffix(".j2")
    workflow_knowledge_base = WORKFLOW_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    return prompt_engine.load_prompt(
        template=template,
        workflow_knowledge_base=workflow_knowledge_base,
        current_datetime=datetime.now(timezone.utc).isoformat(),
        tool_usage_guide=tool_usage_guide,
        security_rules=copilot_config.security_rules,
    )


def _build_dynamic_system_prompt(tool_usage_guide: str, config: CopilotConfig) -> Any:
    base_system_prompt = _build_system_prompt(tool_usage_guide=tool_usage_guide, config=config)

    def instructions(context: Any, _agent: Any) -> str:
        ctx = getattr(context, "context", None)
        policy = getattr(ctx, "request_policy", None)
        if not isinstance(policy, RequestPolicy):
            return base_system_prompt
        policy_summary = escape_code_fences(redact_raw_secrets_for_prompt(policy.prompt_summary()))
        prompt = (
            base_system_prompt
            + "\n\nREQUEST POLICY:\n```yaml\n"
            + policy_summary
            + "\n```\nFollow this policy. If `allow_run_blocks` is false, do not call block-running tools. "
            + "Exception: when `clarification_reason` is `workflow_credential_inputs_unbound` or "
            + "`credential_name_unresolved` and "
            + "`allow_missing_credentials_in_draft` is true, call `update_and_run_blocks`; it will save the draft "
            + "workflow and skip the browser run with a credential setup message. "
            + "If `resolved_credentials` are present, use those `credential_id` values."
        )
        return prompt + _docs_answer_turn_directive(getattr(ctx, "turn_intent", None))

    return instructions


def _docs_answer_turn_directive(turn_intent: TurnIntent | None) -> str:
    """Prompt-side complement to the no-mutation tool gate — keeps a docs-answer
    turn from substituting a routing question or build offer for the inline answer."""
    if not isinstance(turn_intent, TurnIntent) or turn_intent.mode != TurnIntentMode.DOCS_ANSWER:
        return ""
    return (
        "\n\nTURN INTENT: docs_answer\n"
        "This turn is a documentation or explanation question. Answer it inline in the user's language. "
        "Do not ask whether the user wants a workflow change instead, do not re-ask a confirmation the "
        "prior turn already covered, and do not offer to build an example workflow in place of answering."
    )


def _build_user_context(
    workflow_yaml: str,
    chat_history_text: str,
    global_llm_context: str,
    debug_run_info_text: str,
    user_message: str,
    request_policy_summary: str = "",
    user_workflow_change_summary: str = "",
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
    workflow_yaml = redact_raw_secrets_for_prompt(workflow_yaml or "")
    return prompt_engine.load_prompt(
        template="workflow-copilot-user",
        workflow_yaml=escape_code_fences(workflow_yaml),
        workflow_summary=escape_code_fences(_build_workflow_summary(workflow_yaml)),
        chat_history=escape_code_fences(redact_raw_secrets_for_prompt(chat_history_text)),
        global_llm_context=escape_code_fences(redact_raw_secrets_for_prompt(global_llm_context)),
        debug_run_info=escape_code_fences(redact_raw_secrets_for_prompt(debug_run_info_text)),
        request_policy_summary=escape_code_fences(redact_raw_secrets_for_prompt(request_policy_summary)),
        user_message=escape_code_fences(redact_raw_secrets_for_prompt(user_message)),
        user_workflow_change_summary=escape_code_fences(user_workflow_change_summary or ""),
    )


def _truncate_summary_text(value: Any, max_chars: int = 240) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _block_summary_lines(blocks: list[Any], *, depth: int = 0) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth
    for block in blocks:
        if not isinstance(block, dict):
            continue

        label = block.get("label") or "(unlabeled)"
        block_type = block.get("block_type") or "unknown"
        line_parts = [f"{indent}- {label} ({block_type})"]
        next_label = block.get("next_block_label")
        if next_label:
            line_parts.append(f"next={next_label}")

        error_code_mapping = block.get("error_code_mapping")
        if isinstance(error_code_mapping, dict) and error_code_mapping:
            mappings = [f"{code}: {_truncate_summary_text(reason)}" for code, reason in error_code_mapping.items()]
            line_parts.append("error_code_mapping={" + "; ".join(mappings) + "}")

        branch_conditions = block.get("branch_conditions")
        if isinstance(branch_conditions, list) and branch_conditions:
            branch_targets = []
            for branch in branch_conditions:
                if not isinstance(branch, dict):
                    continue
                target = branch.get("next_block_label")
                if target:
                    prefix = "default -> " if branch.get("is_default") else "branch -> "
                    branch_targets.append(prefix + str(target))
            if branch_targets:
                line_parts.append("branches=[" + ", ".join(branch_targets) + "]")

        lines.append("; ".join(line_parts))

        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list) and loop_blocks:
            lines.extend(_block_summary_lines(loop_blocks, depth=depth + 1))

    return lines


def _build_workflow_summary(workflow_yaml: str | None) -> str:
    """Return a compact block index for the model before the full YAML.

    The full workflow YAML remains the source of truth, but large block goals
    can bury later labels and per-block error mappings. This summary gives
    block-specific debug turns a cheap index so an existing label like
    ``block_2`` is not missed before the model inspects details in the YAML.
    """
    if not workflow_yaml:
        return ""
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""

    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return ""
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return ""

    lines = _block_summary_lines(blocks)
    if not lines:
        return ""
    summary = "\n".join(lines)
    max_summary_chars = 12_000
    if len(summary) > max_summary_chars:
        return summary[: max_summary_chars - 80].rstrip() + "\n... workflow summary truncated ..."
    return summary


def _build_tool_usage_guide(tool_names_and_descriptions: list[tuple[str, str]]) -> str:
    if not tool_names_and_descriptions:
        return ""
    return "\n".join(
        f"- **{name}** — {description or 'No description provided.'}"
        for name, description in tool_names_and_descriptions
    )


def _turn_intent_disables_tools(turn_intent: TurnIntent | None) -> bool:
    if not isinstance(turn_intent, TurnIntent) or turn_intent.mode not in NO_MUTATION_TURN_INTENT_MODES:
        return False

    authority = turn_intent.authority
    return not authority.may_update_workflow and not authority.may_run_blocks


def _request_policy_requires_update_and_run_skip_path(request_policy: RequestPolicy | None) -> bool:
    return (
        isinstance(request_policy, RequestPolicy)
        and request_policy.allow_update_workflow
        and not request_policy.allow_run_blocks
        and request_policy.allow_missing_credentials_in_draft
        and request_policy.clarification_reason in CREDENTIAL_DEFERRED_DRAFT_REASONS
    )


def _native_tools_for_turn(
    native_tools: list[Any],
    turn_intent: TurnIntent | None,
    request_policy: RequestPolicy | None = None,
) -> list[Any]:
    if _turn_intent_disables_tools(turn_intent):
        return []
    if _request_policy_requires_update_and_run_skip_path(request_policy):
        return [tool for tool in native_tools if getattr(tool, "name", None) != "update_workflow"]
    return list(native_tools)


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

    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if (
        policy is not None
        and policy.clarification_reason == "workflow_credential_inputs_unbound"
        and ctx.last_workflow is not None
        and ctx.last_update_block_count is not None
    ):
        if ctx.last_update_block_count <= 0:
            draft_phrase = "a draft workflow"
        else:
            block_word = "block" if ctx.last_update_block_count == 1 else "blocks"
            draft_phrase = f"a draft workflow with {ctx.last_update_block_count} {block_word}"
        return (
            f"I applied your requested change as {draft_phrase}. "
            f"I couldn't test the modified workflow because I couldn't find the required credentials — "
            f"please add them via the Credentials UI, then I can try again.{keep_draft_affordance}"
        )

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
# Ends with RAW_SECRET_REFUSAL_SENTINEL so transcript redaction recognizes this refusal in history.
_RAW_SECRET_LEAK_REFUSAL = (
    "I can't show or save that output because it appears to include raw credentials or secrets. "
    "Store credentials in the Skyvern Credentials UI and reply with the saved credential name or a "
    f"credential ID beginning with cred_. {RAW_SECRET_REFUSAL_SENTINEL}."
)
_SAVED_DRAFT_OUTPUT_POLICY_SUFFIX = "I only blocked the chat reply; the workflow draft is still saved."
_CANCEL_REPLY_DEFAULT = "Cancelled by user."
_CANCEL_REPLY_UNVALIDATED = (
    "Cancelled. I have a draft workflow you can keep — accept it to save "
    "(note: it hasn't been verified end-to-end), or discard."
)
_CANCEL_REPLY_TESTED = "Cancelled. I have a tested draft for you. Accept it to save, or discard."
_UNBACKED_WORKFLOW_DELIVERY_REPLY = (
    "I wasn't able to produce a workflow proposal in this turn. Please try again, or provide the missing details "
    "so I can build and test it."
)
_INTERNAL_BLOCK_TAXONOMY_REPLY = (
    "Internal workflow names are not the right interface to use when building with Copilot. "
    "Describe the page action, data to collect, sign-in step, or check you want, and I'll translate that into "
    "a supported workflow update."
)
_BLOCK_YAML_IN_REPLY_REWRITE_NO_PROPOSAL = (
    "I drafted a change to the workflow but haven't applied it yet. Want me to update the workflow now?"
)
_BLOCK_YAML_IN_REPLY_REWRITE_WITH_PROPOSAL = "I made the change you described to the workflow."
_PROPOSAL_ACCEPT_UI_ACTION_RE = re.compile(r"\b(?:accept|always\s+accept)\b", re.IGNORECASE)
_PROPOSAL_REJECT_UI_ACTION_RE = re.compile(r"\b(?:reject|discard)\b", re.IGNORECASE)
_UNVALIDATED_PROPOSAL_AFFORDANCE = (
    "I have a draft workflow proposal. Use Review to inspect it, Accept to save it, or Reject to discard it. "
    "It has not been tested or verified end-to-end."
)
_RECORDED_FAILURE_URL_RE = re.compile(r"https?://[^\s)>,]+")


def _workflow_block_count(ctx: CopilotContext) -> int | None:
    count = getattr(ctx, "last_update_block_count", None)
    if isinstance(count, int) and count > 0:
        return count
    workflow = getattr(ctx, "last_workflow", None)
    definition = getattr(workflow, "workflow_definition", None)
    blocks = getattr(definition, "blocks", None)
    return len(blocks) if isinstance(blocks, list) and blocks else None


def _clean_recorded_failure_text(value: Any, max_chars: int = 240) -> str:
    from skyvern.forge.sdk.copilot.enforcement import redact_browser_session_references

    if not isinstance(value, str):
        return ""
    text = redact_raw_secrets_for_prompt(" ".join(value.split()))
    text = redact_browser_session_references(text)
    text = _RECORDED_FAILURE_URL_RE.sub(lambda match: url_origin(match.group(0)) or "[URL]", text)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text.rstrip(".")


def _recorded_failure_summary(ctx: CopilotContext) -> tuple[str, str]:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    verification = getattr(contract, "verification_result", None)
    diagnosis = getattr(contract, "diagnosis_result", None)
    remaining_blocker = _clean_recorded_failure_text(getattr(verification, "remaining_blocker", None))
    root_cause = _clean_recorded_failure_text(getattr(diagnosis, "root_cause_summary", None))
    fallback_reason = _clean_recorded_failure_text(getattr(ctx, "last_test_failure_reason", None))
    reason = remaining_blocker or root_cause or fallback_reason
    run_status = _clean_recorded_failure_text(getattr(verification, "run_status", None), max_chars=80)
    status_sentence = f" Last run status: {run_status}." if run_status else ""
    return reason, status_sentence


def _last_good_failure_reply(ctx: CopilotContext, tested_reply: str) -> str:
    reason, status_sentence = _recorded_failure_summary(ctx)
    if not reason:
        return tested_reply
    return f"{tested_reply} The latest attempted change did not verify: {reason}.{status_sentence}"


def _recorded_failure_reply(ctx: CopilotContext, *, cancelled: bool = False) -> str | None:
    if cancelled or ctx.last_test_ok is True:
        return None

    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    verification = getattr(contract, "verification_result", None)
    diagnosis = getattr(contract, "diagnosis_result", None)
    repair_decision = getattr(contract, "repair_decision", None)
    diagnosis_input = getattr(contract, "diagnosis_input", None)
    failure_type = getattr(getattr(diagnosis, "suspected_failure_type", None), "value", None) or getattr(
        diagnosis,
        "suspected_failure_type",
        None,
    )
    next_action = getattr(getattr(repair_decision, "next_action", None), "value", None) or getattr(
        repair_decision,
        "next_action",
        None,
    )
    reason, status_sentence = _recorded_failure_summary(ctx)
    if not reason:
        return None

    run_status = _clean_recorded_failure_text(getattr(verification, "run_status", None), max_chars=80).lower()
    block_count = _workflow_block_count(ctx)
    block_phrase = f"a {block_count}-block draft" if block_count else "a draft"
    test_attempted = bool(
        getattr(ctx, "test_after_update_done", False)
        or getattr(ctx, "last_test_ok", None) is not None
        or getattr(diagnosis_input, "workflow_run_id", None)
    )
    test_failed = ctx.last_test_ok is False or run_status == "failed"
    unrecoverable_stop = next_action == "stop" or failure_type == "unrecoverable_tool_error"

    if getattr(ctx, "last_workflow", None) is not None:
        if test_attempted and test_failed and not unrecoverable_stop:
            return f"I built {block_phrase} and tested it, but the test failed: {reason}.{status_sentence}"
        if test_attempted:
            return f"I built {block_phrase} and tested it, but the test couldn't finish: {reason}.{status_sentence}"
        return f"I built {block_phrase}, but I couldn't verify it: {reason}.{status_sentence}"
    return f"I couldn't finish the Copilot turn: {reason}.{status_sentence}"


def _ensure_unvalidated_proposal_affordance(user_response: str) -> str:
    lower = user_response.lower()
    has_ui_affordance = bool(
        _PROPOSAL_ACCEPT_UI_ACTION_RE.search(user_response) and _PROPOSAL_REJECT_UI_ACTION_RE.search(user_response)
    )
    has_unvalidated_disclosure = any(phrase in lower for phrase in UNVALIDATED_DISCLOSURE_PHRASES)
    if has_ui_affordance and has_unvalidated_disclosure:
        return user_response
    if user_response.strip():
        return f"{user_response}\n\n{_UNVALIDATED_PROPOSAL_AFFORDANCE}"
    return _UNVALIDATED_PROPOSAL_AFFORDANCE


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
    recorded_failure_reply = _recorded_failure_reply(ctx, cancelled=cancelled)
    # When an unverified edit/run has overwritten ``last_workflow``, prefer the
    # verified shape while still forcing explicit review.
    if (
        ctx.last_good_workflow is not None
        and ctx.last_good_workflow_yaml
        and ctx.last_workflow is not ctx.last_good_workflow
        and not ctx.last_test_suspicious_success
    ):
        return AgentResult(
            user_response=_last_good_failure_reply(ctx, tested_reply) if recorded_failure_reply else tested_reply,
            updated_workflow=ctx.last_good_workflow,
            global_llm_context=global_llm_context,
            workflow_yaml=ctx.last_good_workflow_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            total_tokens=ctx.total_tokens_used,
            proposal_disposition="review_tested",
            cancelled=cancelled,
        )
    if (
        ctx.last_workflow is not None
        and ctx.last_workflow_yaml
        and ctx.last_test_ok is not False
        and not ctx.last_test_suspicious_success
    ):
        unvalidated = ctx.last_test_ok is not True
        if unvalidated and recorded_failure_reply:
            reply = recorded_failure_reply
        else:
            reply = unvalidated_reply if unvalidated else tested_reply
        return AgentResult(
            user_response=reply,
            updated_workflow=ctx.last_workflow,
            global_llm_context=global_llm_context,
            workflow_yaml=ctx.last_workflow_yaml,
            workflow_was_persisted=ctx.workflow_persisted,
            total_tokens=ctx.total_tokens_used,
            proposal_disposition="review_untested" if unvalidated else "auto_applicable",
            cancelled=cancelled,
        )
    return _build_exit_result(ctx, recorded_failure_reply or default_reply, global_llm_context, cancelled=cancelled)


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
    normalized_scaffolding = normalize_response_scaffolding(resp_type, str(user_response))
    resp_type = normalized_scaffolding.response_type
    user_response = normalized_scaffolding.user_response or "Done."

    last_workflow = ctx.last_workflow
    last_workflow_yaml = ctx.last_workflow_yaml

    if resp_type == "REPLACE_WORKFLOW":
        LOG.warning("Agent used inline REPLACE_WORKFLOW instead of update_workflow tool")
        workflow_yaml = action_data.get("workflow_yaml", "")
        if workflow_yaml:
            inline_policy_verdict = hard_block_output_policy_verdict(
                evaluate_output_policy(
                    request_policy=ctx.request_policy,
                    response_type=resp_type,
                    user_response=str(user_response),
                    workflow_yaml=workflow_yaml,
                    tool_arguments=action_data,
                    has_workflow_proposal=True,
                    output_kind=CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL,
                )
            )
            if not inline_policy_verdict.allowed:
                return _build_output_policy_blocked_result(
                    ctx,
                    inline_policy_verdict,
                    prior_global_llm_context=global_llm_context,
                    prior_workflow_yaml=chat_request.workflow_yaml,
                )
            # REPLACE_WORKFLOW bypasses the update_workflow tool guardrail, so
            # policy and post-emission rejects run here before YAML processing.
            # The final-output policy pass still runs below; leave last_workflow
            # / last_workflow_yaml unchanged until this candidate survives the
            # inline checks.
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
    if _should_surface_untested_draft_despite_question(ctx, resp_type):
        LOG.info(
            "Converting copilot clarification into untested draft proposal",
            workflow_permanent_id=ctx.workflow_permanent_id,
            block_count=ctx.last_update_block_count,
        )
        resp_type = "REPLY"

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
    workflow_attempted = ctx.last_update_block_count is not None or ctx.last_test_ok is not None
    output_kind = derive_output_kind(
        response_type=resp_type,
        request_policy=ctx.request_policy,
        updated_workflow=last_workflow,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        unvalidated=unvalidated,
    )

    raw_output_policy_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type=resp_type,
        user_response=str(user_response),
        global_llm_context=enriched_context,
        workflow_yaml=last_workflow_yaml,
        has_workflow_proposal=last_workflow is not None,
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        unvalidated=unvalidated,
        output_kind=output_kind,
    )
    output_policy_verdict = _copy_output_policy_verdict(raw_output_policy_verdict)
    soft_rewrite_reasons: list[OutputPolicyReason] = []
    if OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK in output_policy_verdict.reason_codes:
        user_response = _INTERNAL_BLOCK_TAXONOMY_REPLY
        soft_rewrite_reasons.append(OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK)
        output_policy_verdict.remove(OutputPolicyReason.INTERNAL_BLOCK_TAXONOMY_LEAK)
    if OutputPolicyReason.WORKFLOW_YAML_IN_REPLY in output_policy_verdict.reason_codes:
        user_response = (
            _BLOCK_YAML_IN_REPLY_REWRITE_WITH_PROPOSAL
            if last_workflow is not None
            else _BLOCK_YAML_IN_REPLY_REWRITE_NO_PROPOSAL
        )
        soft_rewrite_reasons.append(OutputPolicyReason.WORKFLOW_YAML_IN_REPLY)
        output_policy_verdict.remove(OutputPolicyReason.WORKFLOW_YAML_IN_REPLY)
    # Preserve the unbacked-proposal correction when both soft rewrites apply:
    # a reply must not imply a workflow exists when no proposal was produced.
    if OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM in output_policy_verdict.reason_codes:
        user_response = _UNBACKED_WORKFLOW_DELIVERY_REPLY
        soft_rewrite_reasons.append(OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM)
        output_policy_verdict.remove(OutputPolicyReason.UNBACKED_WORKFLOW_DELIVERY_CLAIM)
    if OutputPolicyReason.MISSING_PROPOSAL_STATE in output_policy_verdict.reason_codes:
        soft_rewrite_reasons.append(OutputPolicyReason.MISSING_PROPOSAL_STATE)
        output_policy_verdict.remove(OutputPolicyReason.MISSING_PROPOSAL_STATE)
    if OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE in output_policy_verdict.reason_codes:
        user_response = _ensure_unvalidated_proposal_affordance(str(user_response))
        soft_rewrite_reasons.append(OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE)
        output_policy_verdict.remove(OutputPolicyReason.MISSING_UNVALIDATED_PROPOSAL_AFFORDANCE)
    final_output_kind = (
        _blocked_final_output_kind(output_policy_verdict)
        if not output_policy_verdict.allowed
        else output_policy_verdict.output_kind
    )
    output_policy_diagnostics = build_output_policy_diagnostics(
        raw_verdict=raw_output_policy_verdict,
        final_verdict=output_policy_verdict,
        final_output_kind=final_output_kind,
        hard_block_reason_codes=list(output_policy_verdict.reason_codes),
        soft_rewrite_reason_codes=soft_rewrite_reasons,
    )
    trace_data = output_policy_verdict_to_trace_data(
        output_policy_verdict,
        surface="final_translation",
        response_type=resp_type,
    )
    trace_data.update(output_policy_diagnostics)
    LOG.info(
        "copilot output policy final verdict",
        **trace_data,
    )
    if not output_policy_verdict.allowed:
        return _build_output_policy_blocked_result(
            ctx,
            output_policy_verdict,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
            output_policy_diagnostics=output_policy_diagnostics,
        )

    return AgentResult(
        user_response=str(user_response),
        updated_workflow=last_workflow,
        global_llm_context=enriched_context or None,
        response_type=resp_type,
        workflow_yaml=last_workflow_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
        total_tokens=ctx.total_tokens_used,
        clear_proposed_workflow=resp_type == "ASK_QUESTION",
        proposal_disposition="review_untested" if unvalidated else "auto_applicable",
        output_policy_diagnostics=output_policy_diagnostics,
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


_RETRIABLE_LLM_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "APIError",
    "InternalServerError",
    "RateLimitError",
    "ServiceUnavailableError",
    "Timeout",
}
_RETRIABLE_LLM_ERROR_TEXT = (
    "rate limit",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "connection error",
    "connection reset",
    "internal server error",
    "server error",
    "overloaded",
)
_LLM_ERROR_MODULE_MARKERS = ("openai", "litellm", "anthropic")


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_retriable_llm_error(exc: BaseException) -> bool:
    for item in _iter_exception_chain(exc):
        module = type(item).__module__.lower()
        name = type(item).__name__
        text = str(item).lower()
        if name in _RETRIABLE_LLM_ERROR_NAMES and any(marker in module for marker in _LLM_ERROR_MODULE_MARKERS):
            return True
        if any(marker in module for marker in _LLM_ERROR_MODULE_MARKERS) and any(
            phrase in text for phrase in _RETRIABLE_LLM_ERROR_TEXT
        ):
            return True
    return False


def _fallback_llm_key(config: CopilotConfig, current_llm_key: str) -> str | None:
    fallback_key = config.fallback_llm_key
    if not fallback_key or fallback_key == current_llm_key:
        return None
    return fallback_key


def _build_request_policy_clarification_result(
    policy: RequestPolicy,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
) -> AgentResult:
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    structured.decisions_made.append(
        f"request-policy clarification required: {policy.credential_input_kind}/{policy.clarification_reason}"
    )
    return AgentResult(
        user_response=policy.clarification_question
        or "I need one more detail before I can build and test this workflow safely.",
        updated_workflow=None,
        global_llm_context=structured.to_json_str(),
        response_type="ASK_QUESTION",
        workflow_yaml=prior_workflow_yaml or None,
        workflow_was_persisted=False,
        clear_proposed_workflow=True,
    )


def _agent_output_to_text(agent_output: Any) -> str:
    if isinstance(agent_output, str):
        return agent_output
    if hasattr(agent_output, "model_dump"):
        try:
            return json.dumps(agent_output.model_dump())
        except Exception:
            return str(agent_output)
    try:
        return json.dumps(agent_output, default=str)
    except TypeError:
        return str(agent_output)


def _should_surface_untested_draft_despite_question(ctx: CopilotContext, response_type: str) -> bool:
    if response_type != "ASK_QUESTION" or ctx.last_workflow is None or not ctx.last_workflow_yaml:
        return False
    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if request_policy is None or ctx.last_test_ok is not None:
        return False
    if request_policy.testing_intent == "skip_test" and ctx.allow_untested_workflow_draft:
        return True
    return (
        request_policy.clarification_reason == "workflow_credential_inputs_unbound"
        and not request_policy.allow_run_blocks
        and request_policy.allow_missing_credentials_in_draft
    )


def _copy_output_policy_verdict(verdict: OutputPolicyVerdict) -> OutputPolicyVerdict:
    return OutputPolicyVerdict(
        allowed=verdict.allowed,
        output_kind=verdict.output_kind,
        reason_codes=list(verdict.reason_codes),
    )


def _blocked_final_output_kind(verdict: OutputPolicyVerdict) -> CopilotOutputKind:
    clarification_reasons = {
        OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS,
        OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE,
        OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED,
    }
    if any(reason in clarification_reasons for reason in verdict.reason_codes):
        return CopilotOutputKind.CLARIFICATION_REQUEST
    return CopilotOutputKind.REFUSAL


def _evaluate_copilot_final_output_policy(
    ctx: CopilotContext,
    agent_output: Any,
) -> tuple[OutputPolicyVerdict, str, dict[str, Any]]:
    text = _agent_output_to_text(agent_output)
    action_data = parse_final_response(text)
    response_type = action_data.get("type", "REPLY")
    if response_type not in COPILOT_RESPONSE_TYPES:
        response_type = "REPLY"
    policy_user_response = str(action_data.get("user_response") or text)
    normalized_scaffolding = normalize_response_scaffolding(response_type, policy_user_response)
    response_type = normalized_scaffolding.response_type
    policy_user_response = normalized_scaffolding.user_response or "Done."

    workflow_yaml = None
    if response_type == "REPLACE_WORKFLOW" and isinstance(action_data.get("workflow_yaml"), str):
        workflow_yaml = action_data["workflow_yaml"]
    elif isinstance(getattr(ctx, "last_workflow_yaml", None), str):
        workflow_yaml = ctx.last_workflow_yaml

    workflow_attempted = ctx.last_update_block_count is not None or ctx.last_test_ok is not None
    surface_untested_draft = _should_surface_untested_draft_despite_question(ctx, response_type)
    policy_response_type = "REPLY" if surface_untested_draft else response_type
    if surface_untested_draft:
        policy_user_response = _rewrite_failed_test_response(policy_user_response, ctx)
    updated_workflow_for_kind = (
        ctx.last_workflow if ctx.last_workflow is not None else WORKFLOW_PRESENT_SENTINEL if workflow_yaml else None
    )
    if surface_untested_draft:
        output_kind = (
            CopilotOutputKind.WORKFLOW_UPDATE_PROPOSAL
            if ctx.workflow_persisted
            else CopilotOutputKind.WORKFLOW_DRAFT_PROPOSAL
        )
    else:
        output_kind = derive_output_kind(
            response_type=response_type,
            request_policy=ctx.request_policy,
            updated_workflow=updated_workflow_for_kind,
            workflow_was_persisted=ctx.workflow_persisted,
            workflow_attempted=workflow_attempted,
            unvalidated=False,
        )
    raw_verdict = evaluate_output_policy(
        request_policy=ctx.request_policy,
        response_type=policy_response_type,
        user_response=policy_user_response,
        global_llm_context=action_data.get("global_llm_context"),
        workflow_yaml=workflow_yaml,
        tool_arguments=None,
        has_workflow_proposal=bool(workflow_yaml or ctx.last_workflow is not None),
        workflow_was_persisted=ctx.workflow_persisted,
        workflow_attempted=workflow_attempted,
        output_kind=output_kind,
    )
    hard_verdict = hard_block_output_policy_verdict(raw_verdict)
    diagnostics = build_output_policy_diagnostics(
        raw_verdict=raw_verdict,
        final_verdict=hard_verdict,
        final_output_kind=_blocked_final_output_kind(hard_verdict)
        if not hard_verdict.allowed
        else hard_verdict.output_kind,
        hard_block_reason_codes=list(hard_verdict.reason_codes),
        soft_rewrite_reason_codes=[],
    )
    return hard_verdict, response_type, diagnostics


def _build_copilot_input_guardrails(
    InputGuardrailCls: Any,
    GuardrailFunctionOutputCls: Any,
    *,
    policy_inputs: RequestPolicyGuardrailInputs | None = None,
) -> list[Any]:
    # Guardrail classes are injected after importing the optional Agents SDK in
    # run_copilot_agent, keeping module import safe when the SDK is unavailable.
    async def request_policy_guardrail(context: Any, _agent: Any, _input: Any) -> Any:
        ctx = getattr(context, "context", None)
        policy = getattr(ctx, "request_policy", None)
        if not isinstance(policy, RequestPolicy) and policy_inputs is not None:
            policy = await build_request_policy(
                user_message=policy_inputs.user_message,
                workflow_yaml=policy_inputs.workflow_yaml,
                chat_history=policy_inputs.chat_history_messages,
                global_llm_context=policy_inputs.global_llm_context,
                organization_id=policy_inputs.organization_id,
                handler=policy_inputs.handler,
            )
            if isinstance(ctx, CopilotContext):
                _store_request_policy_on_context(ctx, policy, policy_inputs)
        blocked = isinstance(policy, RequestPolicy) and policy.user_response_policy == "ask_clarification"
        if isinstance(policy, RequestPolicy):
            turn_intent = ctx.turn_intent if isinstance(ctx, CopilotContext) else None
            trace_data = {
                "surface": "agent_input",
                "policy_present": True,
                "blocked": blocked,
                "user_response_policy": policy.user_response_policy,
                **policy.to_trace_data(),
                **_turn_intent_log_fields(turn_intent),
            }
        else:
            trace_data = {"surface": "agent_input", "blocked": False, "policy_present": False}
        LOG.info("copilot request policy input guardrail verdict", **trace_data)
        return GuardrailFunctionOutputCls(output_info=trace_data, tripwire_triggered=blocked)

    return [
        InputGuardrailCls(
            guardrail_function=request_policy_guardrail,
            name="request_policy_guardrail",
            run_in_parallel=False,
        )
    ]


def _build_copilot_output_guardrails(
    OutputGuardrailCls: Any,
    GuardrailFunctionOutputCls: Any,
) -> list[Any]:
    # See _build_copilot_input_guardrails for why SDK classes are passed in.
    def copilot_output_policy_guardrail(context: Any, _agent: Any, agent_output: Any) -> Any:
        ctx = getattr(context, "context", None)
        if not isinstance(ctx, CopilotContext):
            LOG.warning("copilot output guardrail missing CopilotContext", context_type=type(ctx).__name__)
            verdict = OutputPolicyVerdict(
                allowed=False,
                reason_codes=[OutputPolicyReason.OUTPUT_POLICY_CONTEXT_MISSING],
            )
            response_type = "REPLY"
            diagnostics = build_output_policy_diagnostics(
                raw_verdict=verdict,
                final_verdict=verdict,
                final_output_kind=_blocked_final_output_kind(verdict),
                hard_block_reason_codes=list(verdict.reason_codes),
                soft_rewrite_reason_codes=[],
            )
        else:
            verdict, response_type, diagnostics = _evaluate_copilot_final_output_policy(ctx, agent_output)
        trace_data = output_policy_verdict_to_trace_data(
            verdict,
            surface="agent_output",
            response_type=response_type,
        )
        trace_data.update(diagnostics)
        LOG.info("copilot output policy guardrail verdict", **trace_data)
        return GuardrailFunctionOutputCls(output_info=trace_data, tripwire_triggered=not verdict.allowed)

    return [
        OutputGuardrailCls(
            guardrail_function=copilot_output_policy_guardrail,
            name="copilot_output_policy_guardrail",
        )
    ]


def _output_policy_verdict_from_guardrail_exception(exc: BaseException) -> OutputPolicyVerdict:
    guardrail_result = getattr(exc, "guardrail_result", None)
    guardrail_output = getattr(guardrail_result, "output", None)
    return output_policy_verdict_from_trace_data(getattr(guardrail_output, "output_info", None))


def _output_policy_diagnostics_from_guardrail_exception(exc: BaseException) -> dict[str, Any] | None:
    guardrail_result = getattr(exc, "guardrail_result", None)
    guardrail_output = getattr(guardrail_result, "output", None)
    data = getattr(guardrail_output, "output_info", None)
    if not isinstance(data, dict):
        return None
    keys = {
        "raw_output_kind",
        "final_output_kind",
        "hard_block_reason_codes",
        "soft_rewrite_reason_codes",
        "raw_would_have_failed",
        "contained_failure",
        "final_output_policy_allowed",
    }
    return {key: data[key] for key in keys if key in data}


def _build_output_policy_blocked_result(
    ctx: CopilotContext,
    verdict: OutputPolicyVerdict,
    prior_global_llm_context: str | None,
    prior_workflow_yaml: str | None,
    output_policy_diagnostics: dict[str, Any] | None = None,
) -> AgentResult:
    preserved_workflow = ctx.last_workflow if ctx.last_workflow is not None and ctx.last_workflow_yaml else None
    preserved_workflow_yaml = ctx.last_workflow_yaml if preserved_workflow is not None else None
    structured = StructuredContext.from_json_str(prior_global_llm_context)
    structured.decisions_made.append(
        "output-policy blocked final output: " + ", ".join(reason.value for reason in verdict.reason_codes)
    )
    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    add_saved_draft_copy = False
    if (
        request_policy is not None
        and request_policy.clarification_question
        and OutputPolicyReason.REQUEST_POLICY_CLARIFICATION_BYPASS in verdict.reason_codes
    ):
        user_response = request_policy.clarification_question
        add_saved_draft_copy = True
    elif OutputPolicyReason.RAW_SECRET_LEAK in verdict.reason_codes:
        user_response = _RAW_SECRET_LEAK_REFUSAL
        add_saved_draft_copy = True
    elif OutputPolicyReason.UNAPPROVED_CREDENTIAL_REFERENCE in verdict.reason_codes:
        user_response = (
            "I need you to confirm which saved credential should be used before I can continue. "
            "Please reply with the credential name from the Credentials UI, or adjust the workflow to avoid "
            "using credentials."
        )
        add_saved_draft_copy = True
    elif OutputPolicyReason.CREDENTIAL_SCOPE_BROADENED in verdict.reason_codes:
        user_response = (
            "The selected credential is not approved for one of the URLs in this workflow. "
            "Please use a saved credential tested for that URL, update the block URL to match the credential's "
            "tested site, or adjust the workflow to avoid using credentials."
        )
        add_saved_draft_copy = True
    elif preserved_workflow is not None:
        user_response = (
            "I could not safely return that chat reply, but the workflow draft is still saved. "
            "Please review the draft or adjust the request and try again."
        )
    else:
        user_response = "I could not safely return that chat reply. Please adjust the request and try again."
    if preserved_workflow is not None and add_saved_draft_copy:
        user_response = f"{user_response} {_SAVED_DRAFT_OUTPUT_POLICY_SUFFIX}"
    return AgentResult(
        user_response=user_response,
        updated_workflow=preserved_workflow,
        global_llm_context=structured.to_json_str(),
        response_type="ASK_QUESTION",
        workflow_yaml=preserved_workflow_yaml or prior_workflow_yaml,
        workflow_was_persisted=ctx.workflow_persisted,
        total_tokens=ctx.total_tokens_used,
        clear_proposed_workflow=False,
        proposal_disposition=(
            "no_proposal"
            if preserved_workflow is None
            else "review_untested"
            if ctx.last_test_ok is not True
            else "review_tested"
        ),
        output_policy_diagnostics=output_policy_diagnostics
        or build_output_policy_diagnostics(
            raw_verdict=verdict,
            final_verdict=verdict,
            final_output_kind=_blocked_final_output_kind(verdict),
            hard_block_reason_codes=list(verdict.reason_codes),
            soft_rewrite_reason_codes=[],
        ),
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
    config: CopilotConfig | None = None,
    turn_index: int | None = None,
    prior_copilot_workflow_yaml: str | None = None,
) -> AgentResult:
    # Initialize tracing before opening the turn span so Logfire's OTel provider
    # is installed; otherwise the very first turn lands the parent span on
    # OTel's no-op ProxyTracer when running locally with COPILOT_TRACING_ENABLED.
    ensure_tracing_initialized()
    with _copilot_turn_span(
        chat_request=chat_request,
        chat_history=chat_history,
        turn_index=turn_index,
    ):
        return await _run_copilot_turn_impl(
            stream=stream,
            organization_id=organization_id,
            chat_request=chat_request,
            chat_history=chat_history,
            global_llm_context=global_llm_context,
            debug_run_info_text=debug_run_info_text,
            llm_api_handler=llm_api_handler,
            api_key=api_key,
            security_rules=security_rules,
            config=config,
            prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
        )


async def _run_copilot_turn_impl(
    *,
    stream: EventSourceStream,
    organization_id: str,
    chat_request: WorkflowCopilotChatRequest,
    chat_history: list[WorkflowCopilotChatHistoryMessage],
    global_llm_context: str | None,
    debug_run_info_text: str,
    llm_api_handler: LLMAPIHandler | None,
    api_key: str | None,
    security_rules: str,
    config: CopilotConfig | None,
    prior_copilot_workflow_yaml: str | None = None,
) -> AgentResult:
    copilot_config = config or CopilotConfig(security_rules=security_rules)
    chat_history_text = _format_chat_history(chat_history)
    safe_chat_history_text = redact_raw_secrets_for_prompt(chat_history_text)
    safe_workflow_yaml = redact_raw_secrets_for_prompt(chat_request.workflow_yaml or "")
    safe_global_llm_context = redact_raw_secrets_for_prompt(global_llm_context or "")
    previous_user_messages = [msg.content for msg in chat_history if msg.sender == "user"]
    previous_user_message = previous_user_messages[-1] if previous_user_messages else None

    try:
        from agents import Agent, GuardrailFunctionOutput, InputGuardrail, OutputGuardrail, trace
        from agents.exceptions import (
            InputGuardrailTripwireTriggered,
            MaxTurnsExceeded,
            OutputGuardrailTripwireTriggered,
        )
        from agents.mcp import MCPServerManager
        from agents.run_context import RunContextWrapper
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

    ctx = CopilotContext(
        organization_id=organization_id,
        workflow_id=chat_request.workflow_id,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_yaml=chat_request.workflow_yaml or "",
        browser_session_id=None,
        stream=stream,
        api_key=api_key,
        user_message=chat_request.message,
        workflow_copilot_chat_id=chat_request.workflow_copilot_chat_id,
    )
    policy_inputs = RequestPolicyGuardrailInputs(
        user_message=chat_request.message,
        workflow_yaml=safe_workflow_yaml,
        chat_history_text=safe_chat_history_text,
        chat_history_messages=list(chat_history),
        global_llm_context=safe_global_llm_context,
        organization_id=organization_id,
        handler=_resolve_request_policy_handler(llm_api_handler),
        previous_user_message=previous_user_message,
        workflow_id=chat_request.workflow_id,
        workflow_permanent_id=chat_request.workflow_permanent_id,
        workflow_run_id=getattr(chat_request, "workflow_run_id", None),
        browser_session_id=getattr(chat_request, "browser_session_id", None),
    )
    request_policy_guardrails = _build_copilot_input_guardrails(
        InputGuardrail,
        GuardrailFunctionOutput,
        policy_inputs=policy_inputs,
    )
    # Run the request-policy guardrail as the authoritative input gate before
    # feasibility checks, browser/session setup, model execution, or tool calls.
    # Do not also attach it to the main Agent; the SDK would invoke it again and
    # duplicate policy telemetry.
    request_policy_guardrail_result = await request_policy_guardrails[0].run(
        Agent(name="workflow-copilot-request-policy", instructions=""),
        chat_request.message,
        RunContextWrapper(context=ctx),
    )
    request_policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    if request_policy is not None:
        _store_turn_context_packet_on_context(
            ctx,
            request_policy=request_policy,
            chat_request=chat_request,
            chat_history=chat_history,
            debug_run_info_text=debug_run_info_text,
            prior_copilot_workflow_yaml=prior_copilot_workflow_yaml,
        )
    if request_policy is not None and request_policy_guardrail_result.output.tripwire_triggered:
        return _build_request_policy_clarification_result(
            request_policy,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
        )
    if request_policy is None:
        raise RuntimeError("Copilot request-policy input guardrail did not populate request policy")

    agent_user_message, safe_chat_history_text = _request_policy_agent_inputs(
        request_policy,
        user_message=chat_request.message,
        chat_history_text=safe_chat_history_text,
        previous_user_message=previous_user_message,
    )

    # Preflight feasibility classifier — fires on every turn so mid-session pivots
    # to impossible targets are caught the same as first-turn structural mismatches.
    from skyvern.forge.sdk.copilot.feasibility_gate import run_feasibility_gate

    feasibility_verdict = await run_feasibility_gate(
        user_message=agent_user_message,
        workflow_yaml=safe_workflow_yaml,
        chat_history=safe_chat_history_text,
        global_llm_context=safe_global_llm_context,
        handler=llm_api_handler,
    )
    if feasibility_verdict.verdict == "ask_clarification" and feasibility_verdict.question:
        return _build_feasibility_clarification_result(
            question=feasibility_verdict.question,
            rationale=feasibility_verdict.rationale,
            user_message=agent_user_message,
            prior_global_llm_context=global_llm_context,
            prior_workflow_yaml=chat_request.workflow_yaml,
        )

    from skyvern.cli.mcp_tools import mcp as skyvern_mcp
    from skyvern.forge.sdk.copilot.enforcement import (
        CopilotNonRetriableNavError,
        CopilotTotalTimeoutError,
        CopilotUnrecoverableToolError,
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
    ctx.browser_session_id = validated_browser_session_id

    model_name, run_config, llm_key, supports_vision = resolve_model_config(
        llm_api_handler,
        copilot_config=copilot_config,
    )
    ctx.supports_vision = supports_vision
    output_guardrails = _build_copilot_output_guardrails(OutputGuardrail, GuardrailFunctionOutput)

    alias_map = get_skyvern_mcp_alias_map()
    overlays = _build_skyvern_mcp_overlays()
    if _turn_intent_disables_tools(ctx.turn_intent):
        alias_map = {}
        overlays = {}

    native_tools = _native_tools_for_turn(list(NATIVE_TOOLS), ctx.turn_intent, ctx.request_policy)
    tool_info: list[tuple[str, str]] = [(tool.name, tool.description or "") for tool in native_tools]
    tool_info.extend((name, overlay.description or "") for name, overlay in overlays.items())

    tool_usage_guide = _build_tool_usage_guide(tool_info)
    system_prompt = _build_dynamic_system_prompt(
        tool_usage_guide=tool_usage_guide,
        config=copilot_config,
    )

    user_workflow_change_summary = ""
    if (
        isinstance(ctx.turn_context_packet, TurnContextPacket)
        and ctx.turn_context_packet.workflow_change_context is not None
    ):
        user_workflow_change_summary = ctx.turn_context_packet.workflow_change_context.rendered_summary

    user_message = _build_user_context(
        workflow_yaml=safe_workflow_yaml,
        chat_history_text=safe_chat_history_text,
        global_llm_context=safe_global_llm_context,
        debug_run_info_text=redact_raw_secrets_for_prompt(debug_run_info_text),
        user_message=agent_user_message,
        user_workflow_change_summary=user_workflow_change_summary,
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
                **{f"request_policy_{key}": str(value) for key, value in request_policy.to_trace_data().items()},
                **_turn_intent_trace_fields(ctx.turn_intent),
                **_turn_context_trace_fields(ctx.turn_context_packet),
            },
        )

    chat_id = chat_request.workflow_copilot_chat_id or chat_request.workflow_permanent_id

    async def _run_attempt(
        attempt_model_name: str,
        attempt_run_config: Any,
        attempt_llm_key: str,
    ) -> RunResultStreaming:
        mcp_server = SkyvernOverlayMCPServer(
            transport=skyvern_mcp,
            overlays=overlays,
            alias_map=alias_map,
            allowlist=frozenset(alias_map.values()),
            context_provider=lambda: ctx,
        )
        agent = Agent(
            name="workflow-copilot",
            instructions=system_prompt,
            tools=native_tools,
            mcp_servers=[mcp_server],
            model=attempt_model_name,
            output_guardrails=output_guardrails,
        )
        session = create_copilot_session(chat_id)
        model_token = _copilot_model_name.set(attempt_model_name)
        try:
            async with MCPServerManager([mcp_server]) as manager:
                agent.mcp_servers = list(manager.active_servers)
                attempts = 2 if ctx.allow_untested_workflow_draft else 1
                for attempt in range(attempts):
                    try:
                        result = await run_with_enforcement(
                            agent=agent,
                            initial_input=user_message,
                            ctx=ctx,
                            stream=stream,
                            max_turns=copilot_config.max_turns,
                            hooks=CopilotRunHooks(ctx),
                            run_config=attempt_run_config,
                            session=session,
                            copilot_config=copilot_config,
                        )
                        break
                    except Exception as exc:
                        if (
                            attempt + 1 < attempts
                            and ctx.last_workflow is None
                            and isinstance(exc, LiteLLMNotFoundError)
                        ):
                            LOG.warning("Retrying untested draft agent loop after model lookup failure")
                            continue
                        raise
            LOG.info(
                "Copilot agent model attempt succeeded",
                workflow_permanent_id=chat_request.workflow_permanent_id,
                llm_key=attempt_llm_key,
            )
            return result
        finally:
            _copilot_model_name.reset(model_token)
            session.close()

    try:
        with trace_context:
            try:
                try:
                    result = await _run_attempt(model_name, run_config, llm_key)
                except Exception as primary_error:
                    fallback_llm_key = _fallback_llm_key(copilot_config, llm_key)
                    if fallback_llm_key is None or not _is_retriable_llm_error(primary_error):
                        raise
                    LOG.warning(
                        "Copilot agent model attempt failed; retrying fallback model",
                        workflow_permanent_id=chat_request.workflow_permanent_id,
                        primary_llm_key=llm_key,
                        fallback_llm_key=fallback_llm_key,
                        error_type=type(primary_error).__name__,
                    )
                    fallback_model_name, fallback_run_config, fallback_resolved_key, fallback_supports_vision = (
                        resolve_model_config(
                            llm_api_handler,
                            copilot_config=copilot_config,
                            llm_key_override=fallback_llm_key,
                        )
                    )
                    ctx.supports_vision = fallback_supports_vision
                    result = await _run_attempt(fallback_model_name, fallback_run_config, fallback_resolved_key)
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
            except InputGuardrailTripwireTriggered:
                return _build_request_policy_clarification_result(
                    request_policy,
                    prior_global_llm_context=global_llm_context,
                    prior_workflow_yaml=chat_request.workflow_yaml,
                )
            except OutputGuardrailTripwireTriggered as exc:
                return _build_output_policy_blocked_result(
                    ctx,
                    _output_policy_verdict_from_guardrail_exception(exc),
                    prior_global_llm_context=global_llm_context,
                    prior_workflow_yaml=chat_request.workflow_yaml,
                    output_policy_diagnostics=_output_policy_diagnostics_from_guardrail_exception(exc),
                )
            except MaxTurnsExceeded:
                return _build_max_turns_exit_result(ctx, global_llm_context)
            except CopilotTotalTimeoutError:
                return _build_timeout_exit_result(ctx, global_llm_context)
            except CopilotUnrecoverableToolError as exc:
                LOG.warning(
                    "Copilot run halted on unrecoverable tool error",
                    tool_name=exc.tool_name,
                    error_message=exc.error_message,
                    organization_id=organization_id,
                )
                return _build_unexpected_error_exit_result(ctx, global_llm_context)
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
