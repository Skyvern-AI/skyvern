from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import structlog

from skyvern.config import settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.build_phase import (
    BuildPhase,
    advance_to_composing,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block
from skyvern.forge.sdk.copilot.composition_browser_expressions import scout_control_state_expression
from skyvern.forge.sdk.copilot.config import (
    BlockAuthoringPolicy,
    download_scout_act_required_for_policy,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_resolution import is_resolved_page_url, load_credentials
from skyvern.forge.sdk.copilot.enforcement import (
    _requested_output_labels_by_path,
    requested_output_extraction_plan,
    requested_output_paths_for_derivation,
    unbound_requested_output_paths_for_designation,
)
from skyvern.forge.sdk.copilot.llm_config import resolve_main_copilot_handler
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay
from skyvern.forge.sdk.copilot.output_designation_resolution import (
    RESOLUTION_PROMPT_TEMPLATE,
    coerce_resolution,
    designation_opportunity,
    render_candidates,
    render_requested_paths,
)
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    live_page_credentials_admissible,
    resolve_credential_for_live_page,
)
from skyvern.forge.sdk.copilot.request_slots import is_canonical_request_slot_path
from skyvern.forge.sdk.copilot.runtime import AgentContext, ScoutedInteraction
from skyvern.forge.sdk.copilot.secret_redaction import redact_raw_secrets_for_prompt
from skyvern.forge.sdk.copilot.secret_scrub import registered_scrub_values
from skyvern.forge.sdk.copilot.typed_value_policy import safe_typed_default_value, should_reject_type_text_value
from skyvern.forge.sdk.schemas.credentials import Credential

from ._shared import (
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _composition_get_structured_evidence,
    _fallback_page_info,
)
from .banned_blocks import (
    _CODE_ONLY_SELECTOR_ACTION_TOOLS,
    _CODE_ONLY_TARGET_EVIDENCE_KEYS,
    _code_only_browser_schema_guidance,
    _code_only_browser_unavailable_summary,
    _copilot_banned_block_alternatives,
    _copilot_banned_block_types,
    _copilot_block_authoring_policy,
    _copilot_block_policy,
    _record_code_native_pending_capability,
    _render_block_policy_detail,
)
from .completion import _maybe_run_completion_verification_from_page_observation
from .page_observation import (
    _record_composition_page_observation,
    _resolve_url_title,
)
from .scouting import (
    _PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS,
    _actionable_targets_for_result,
    _arm_scout_popup_listener,
    _attach_scout_page_summary,
    _capture_post_interaction_screenshot,
    _capture_scout_ambiguity,
    _capture_scout_dynamic_row,
    _capture_scout_role_name,
    _capture_scout_source_url,
    _clear_pending_browser_interaction_observation,
    _click_affordance_target_identities,
    _consume_scout_source_url,
    _mark_page_inspected,
    _mark_pending_browser_interaction_observation,
    _maybe_attach_observed_download_target,
    _maybe_attach_observed_render_target,
    _maybe_attach_reached_download_target,
    _prenav_ambiguity_for_selector,
    _prenav_dynamic_row_for_selector,
    _prenav_role_name_for_selector,
    _record_scouted_interaction,
    _register_scout_interaction_observation,
    _reset_evaluate_tracker,
    _resolve_scout_role_name,
    _scout_session_download_names,
    _selector_live_match_count,
    _steer_evaluate_result,
    account_no_progress_interaction_click,
)

LOG = structlog.get_logger()


def _selector_from_tool_data(data: dict[str, Any], *, prefer_resolved_when_empty: bool = False) -> str:
    raw_selector = data.get("selector")
    selector = raw_selector if isinstance(raw_selector, str) else ""
    if prefer_resolved_when_empty and not selector.strip():
        resolved_selector = data.get("resolved_selector")
        selector = resolved_selector if isinstance(resolved_selector, str) else ""
    return selector.strip()


def _effective_target_text(selector: str, role: str = "", accessible_name: str = "") -> str:
    label = accessible_name.strip() if isinstance(accessible_name, str) else ""
    role_text = role.strip() if isinstance(role, str) else ""
    if label and role_text:
        return f"{role_text} {label}"
    return label or selector


async def _get_block_schema_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    """Short-circuit requests for banned block types with an explicit error.
    Without this pre-hook the underlying MCP tool silently redirects ``task``
    and ``task_v2`` queries to ``navigation``'s schema, which makes the LLM
    think the banned types are available."""
    block_type = params.get("block_type")
    if not isinstance(block_type, str):
        return None
    normalized = normalize_copilot_block_type_alias(block_type)
    if normalized != block_type.strip().lower():
        params["block_type"] = normalized
    policy_entry = _copilot_block_policy(normalized, ctx)
    if policy_entry is None:
        return None
    normalized, policy = policy_entry
    _record_code_native_pending_capability(ctx, policy)
    return {
        "ok": False,
        "error": (
            f"Block type {block_type!r} is not available in the workflow copilot. "
            f"{_render_block_policy_detail(normalized, policy)} {_copilot_banned_block_alternatives(ctx)}"
        ),
    }


_BLOCK_JSON_ALIASES = ("block", "block_definition", "definition", "block_yaml")


def _normalize_block_json_alias(params: dict[str, Any]) -> None:
    """Promote a misnamed block payload (e.g. ``block``) to ``block_json`` in place.

    The model sometimes passes the block under a shorter key than the schema's
    ``block_json``; without this, FastMCP rejects the whole call at signature
    validation before the tool runs. Stray alias keys are always dropped so they
    cannot trip the "unexpected keyword argument" check.
    """
    has_block_json = isinstance(params.get("block_json"), str) and bool(params["block_json"].strip())
    promoted: str | None = None
    for alias in _BLOCK_JSON_ALIASES:
        if alias not in params:
            continue
        value = params.pop(alias)
        if has_block_json or promoted is not None:
            continue
        if isinstance(value, str):
            promoted = value
        elif isinstance(value, (dict, list)):
            promoted = json.dumps(value)
    if promoted is not None:
        params["block_json"] = promoted


async def _validate_block_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    _normalize_block_json_alias(params)
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    block_json = params.get("block_json")
    if not isinstance(block_json, str):
        return None
    try:
        raw = json.loads(block_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    block_type = raw.get("block_type")
    if not isinstance(block_type, str):
        return None
    normalized = normalize_copilot_block_type_alias(block_type.strip().lower())
    if normalized == "code":
        return {
            "ok": False,
            "error": (
                "CODE-ONLY CODE VALIDATION BLOCKED: do not use validate_block for `code` blocks, dummy code "
                "blocks, or probe code blocks in code-only browser mode. validate real code blocks through "
                "update_and_run_blocks."
            ),
        }
    policy_entry = _copilot_block_policy(normalized, ctx)
    if policy_entry is None:
        return None
    normalized, policy = policy_entry
    _record_code_native_pending_capability(ctx, policy)
    return {
        "ok": False,
        "error": (
            f"Block type {block_type!r} is not available in the workflow copilot. "
            f"{_render_block_policy_detail(normalized, policy)} {_copilot_banned_block_alternatives(ctx)}"
        ),
    }


async def _get_block_schema_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    """Scrub banned block types from list-mode responses. Belt-and-suspenders
    against future drift in ``BLOCK_SUMMARIES`` (which currently omits them)."""
    data = result.get("data")
    if isinstance(data, dict):
        block_types = data.get("block_types")
        if isinstance(block_types, dict):
            for banned in _copilot_banned_block_types(ctx):
                block_types.pop(banned, None)
            data["count"] = len(block_types)
        block_type = data.get("block_type")
        if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER and block_type == "code":
            ctx.code_only_code_schema_seen = True
            data["code_only_note"] = _code_only_browser_unavailable_summary()
            data["code_only_guidance"] = _code_only_browser_schema_guidance()
            demonstrated = _demonstrated_step_sources(ctx)
            if demonstrated:
                data["demonstrated_steps"] = demonstrated
    return result


def _demonstrated_step_sources(ctx: AgentContext) -> str:
    """The steps already performed on the page, synthesized into source to build from.

    Invoked exactly as the post-authoring judge invokes it, so the agent reads the source its draft
    will be compared against rather than a laxer variant of it.
    """
    if not ctx.scout_trajectory:
        return ""
    try:
        synthesized = synthesize_code_block(
            list(ctx.scout_trajectory),
            strict_selectors=True,
            reached_download_target=ctx.reached_download_target,
        )
    except Exception:
        LOG.warning("copilot_demonstrated_steps_synthesis_failed", exc_info=True)
        return ""
    if synthesized is None:
        return ""
    if synthesized.diagnostics.truncated:
        # Presenting a capped spine as the whole demonstration is how a step goes missing and comes
        # back as a correction, which is the round-trip this exists to remove.
        return f"{synthesized.code}\n# NOTE: trajectory truncated — later demonstrated steps are not shown above.\n"
    return synthesized.code


def _code_only_pre_run_results_error(ctx: CopilotContext) -> dict[str, Any] | None:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    if ctx.workflow_persisted or ctx.update_workflow_called:
        return None
    for value in (
        ctx.pending_reconciliation_run_id,
        ctx.last_run_blocks_workflow_run_id,
        ctx.last_successful_run_blocks_workflow_run_id,
    ):
        if isinstance(value, str) and value:
            return None
    return {
        "ok": False,
        "error": (
            "CODE-ONLY EXPLORATION PHASE: get_run_results is unavailable before a real workflow run exists. "
            "Use MCP browser tools such as navigate_browser, evaluate, click, type_text, get_browser_screenshot, "
            "console_messages, scroll, select_option, or press_key to understand the page, then call "
            "update_and_run_blocks with real focused code blocks."
        ),
    }


async def _evaluate_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    # Cleared up front so an early reject cannot leave a prior evaluate's expression for this
    # call's post-hook to consume.
    ctx.pending_scout_read_expression = None
    ctx.pending_scout_read_output_path = None
    raw_expression = params.get("expression")
    expr = raw_expression.lower() if isinstance(raw_expression, str) else ""
    if ".click()" in expr or ".click(" in expr:
        return {
            "ok": False,
            "error": "Do not use evaluate to click elements. Use the 'click' tool with a CSS selector instead.",
        }
    if isinstance(raw_expression, str) and raw_expression.strip():
        ctx.pending_scout_read_expression = raw_expression
        raw_output_path = params.get("output_path")
        if isinstance(raw_output_path, str) and raw_output_path.strip():
            ctx.pending_scout_read_output_path = raw_output_path.strip()
    return None


def _code_only_deterministic_targeting_error(tool_name: str) -> str:
    return (
        f"In code-only browser mode, {tool_name} requires a CSS/XPath selector for page mutations "
        "after the reusable workflow has been verified. Use evaluate, screenshots, or page inspection "
        "to derive a selector, then retry with selector only."
    )


def _code_only_selector_action_requires_deterministic_target(ctx: AgentContext) -> bool:
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return False
    if not getattr(ctx, "workflow_persisted", False):
        return False
    return bool(getattr(ctx, "last_full_workflow_test_ok", False))


def _strip_intent_for_code_only_selector_action(
    params: dict[str, Any],
    ctx: AgentContext,
    *,
    tool_name: str,
) -> dict[str, Any] | None:
    if not _code_only_selector_action_requires_deterministic_target(ctx):
        return None
    if tool_name not in _CODE_ONLY_SELECTOR_ACTION_TOOLS:
        return None
    selector = params.get("selector")
    if isinstance(selector, str) and selector.strip():
        if "intent" in params:
            params["intent"] = None
        return None
    if params.get("intent"):
        return {"ok": False, "error": _code_only_deterministic_targeting_error(tool_name)}
    return None


def _code_only_has_target_page_evidence(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        normalized = str(key).strip().lower()
        if normalized in _CODE_ONLY_TARGET_EVIDENCE_KEYS and bool(value):
            return True
        if isinstance(value, dict) and _code_only_has_target_page_evidence(value):
            return True
        if isinstance(value, list) and any(_code_only_has_target_page_evidence(item) for item in value):
            return True
    return False


_JQUERY_SELECTOR_RE = re.compile(r":(?:contains|eq|first|last|gt|lt|nth|visible|hidden|checked)\s*\(", re.IGNORECASE)


async def _click_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    # Cleared up front so an early return below (deterministic result, jQuery reject, no selector)
    # cannot leave a prior click's stash for this click's post-hook to consume.
    ctx.pending_scout_role_name = None
    ctx.pending_scout_click_selector = None
    ctx.pending_scout_ambiguous = None
    ctx.pending_scout_dynamic_row = None
    ctx.pending_scout_download_snapshot = None
    ctx.pending_scout_popup = None
    ctx.pending_scout_popup_content_type = None
    await _capture_scout_source_url(ctx)
    deterministic_result = _strip_intent_for_code_only_selector_action(params, ctx, tool_name="click")
    if deterministic_result is not None:
        return deterministic_result
    selector = params.get("selector", "")
    if not selector:
        return None
    ctx.pending_scout_click_selector = selector if isinstance(selector, str) else None
    if _JQUERY_SELECTOR_RE.search(selector):
        return {
            "ok": False,
            "error": (
                f"Invalid selector: {selector!r}. "
                "jQuery pseudo-selectors like :contains(), :eq(), :first, :visible are NOT valid CSS. "
                "Use standard CSS selectors instead. Examples: "
                "nth-of-type() instead of :eq(), "
                "[data-attr] or tag.class for filtering, "
                "or use the 'evaluate' tool with JS: "
                "document.querySelectorAll('button').forEach("
                "b => {{ if (b.textContent.includes('Download')) b.click() }})"
            ),
        }
    await _capture_scout_role_name(ctx, selector)
    await _capture_scout_ambiguity(ctx, selector)
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        await _capture_scout_dynamic_row(ctx, selector)
        ctx.pending_scout_download_snapshot = await _scout_session_download_names(ctx)
        await _arm_scout_popup_listener(ctx)
    return None


async def _type_text_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    ctx.pending_scout_typed_value = None
    text = params.get("text")
    selector = str(params.get("selector") or "")
    intent = str(params.get("intent") or "")
    # A value already registered as a credential is known to be secret, so it is rejected on that
    # fact rather than on whether it looks secret — a real password need not, and this one reached a
    # plaintext username field because it did not.
    typed_is_registered_secret = isinstance(text, str) and bool(text) and text in set(registered_scrub_values(ctx))
    if typed_is_registered_secret or should_reject_type_text_value(value=text, selector=selector, intent=intent):
        return {
            "ok": False,
            "error": (
                "type_text cannot type raw credentials, secrets, OTP/TOTP codes, API keys, tokens, or "
                "password-like values. Use fill_credential_field with the saved credential and the "
                "field it belongs in, rather than typing the value into a field yourself."
            ),
        }
    if isinstance(text, str) and text:
        ctx.pending_scout_typed_value = text
    result = _strip_intent_for_code_only_selector_action(params, ctx, tool_name="type_text")
    if result is not None:
        # Non-None means the deterministic targeting guard is rejecting the
        # tool call before browser execution; there will be no post-hook to
        # consume this value.
        ctx.pending_scout_typed_value = None
    return result


async def _select_option_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    ctx.pending_scout_ambiguous = None
    ctx.pending_scout_reanchor = None
    await _capture_scout_source_url(ctx)
    result = _strip_intent_for_code_only_selector_action(params, ctx, tool_name="select_option")
    if result is None:
        await _capture_scout_ambiguity(ctx, params.get("selector", ""))
    return result


async def _press_key_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    return _strip_intent_for_code_only_selector_action(params, ctx, tool_name="press_key")


async def _bind_login_credential_for_observed_url(ctx: AgentContext, url: str, result: dict[str, Any]) -> None:
    """Surface the saved credential the observed page vouches for, if the server can bind one.

    A resolver or loader failure leaves the page observation exactly as it was, but the context
    reads stay outside that guard: swallowing them would turn a mis-wired context into a silent
    no-op that no test can catch.
    """
    if not url:
        return

    policy = ctx.request_policy
    if not isinstance(policy, RequestPolicy) or not live_page_credentials_admissible(policy):
        return
    organization_id = ctx.organization_id

    if not is_resolved_page_url(url):
        # A `current_page` inspection stamps its placeholder when the URL read raced the capture.
        # The match is against the page the browser actually reached, so read it again rather than
        # hand over a token no page vouches for.
        url, _ = await _fallback_page_info(ctx)

    async def load_once() -> list[Credential]:
        if ctx.org_credentials_for_turn is None:
            ctx.org_credentials_for_turn = await load_credentials(organization_id)
        return ctx.org_credentials_for_turn

    try:
        record = await resolve_credential_for_live_page(
            policy,
            organization_id=organization_id,
            page_url=url,
            load_org_credentials=load_once,
        )
    except Exception:
        LOG.warning(
            "copilot credential live-page admission",
            outcome="resolver_error",
            seam="page_observation",
            exc_info=True,
        )
        return

    if record.verdict == "resolved" and record.candidates:
        credential = record.candidates[0]
        result["resolved_login_credential_id"] = credential.credential_id
        result["resolved_login_credential_name"] = credential.name
        # The observation may name a placeholder while the match ran against the page read after it,
        # so the page the credential was matched on is reported rather than left to be inferred.
        result["resolved_login_page_url"] = url
    elif record.verdict == "ambiguous":
        result["candidate_login_credentials"] = [
            {"credential_id": candidate.credential_id, "name": candidate.name} for candidate in record.candidates
        ]


async def _navigate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    if result.get("ok"):
        data = result.pop("data", {})
        result["url"] = data.get("url", "")
        await _bind_login_credential_for_observed_url(ctx, result["url"], result)
        result["next_step"] = (
            "Page loaded, and a screenshot of it is attached. Use evaluate or "
            "inspect_page_for_composition when you need the page's structure or selectors "
            "before responding."
        )
        await _capture_post_interaction_screenshot(ctx)
        if (
            _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
            and isinstance(ctx, CopilotContext)
            and ctx.build_phase in {BuildPhase.INITIAL, BuildPhase.DISCOVERING}
        ):
            advance_to_composing(ctx, reason="code_only_browser_navigation_succeeded")
    return result


async def _screenshot_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        _mark_page_inspected(ctx)
        data = result["data"]
        url, title = await _resolve_url_title(raw, ctx)
        _record_composition_page_observation(ctx, source_tool="get_browser_screenshot", url=url, title=title)
        result["data"] = {
            "screenshot_base64": data.get("data", ""),
            "url": url,
            "title": title,
        }
    return result


async def _click_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    ctx.last_scout_act_observe_outcome = None
    ctx.last_scout_act_observe_packet = None
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    pending_role_name = ctx.pending_scout_role_name
    ctx.pending_scout_role_name = None
    pending_ambiguous = ctx.pending_scout_ambiguous
    ctx.pending_scout_ambiguous = None
    pending_reanchor = ctx.pending_scout_reanchor
    ctx.pending_scout_reanchor = None
    pending_dynamic_row = ctx.pending_scout_dynamic_row
    ctx.pending_scout_dynamic_row = None
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        pending_dynamic_row = None
    attempted_selector = ctx.pending_scout_click_selector
    ctx.pending_scout_click_selector = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector = _selector_from_tool_data(data, prefer_resolved_when_empty=True)
        url, title = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="click", url=url)
        result["data"] = {
            "selector": selector,
            "url": url,
            "title": title,
        }
        await _bind_login_credential_for_observed_url(ctx, url, result)
        navigated = bool(source_url) and bool(url) and source_url != url
        role, accessible_name = await _resolve_scout_role_name(ctx, selector, allow_browser_read=not navigated)
        if navigated and not (role and accessible_name):
            role, accessible_name = _prenav_role_name_for_selector(pending_role_name, selector)
        ambiguous = _prenav_ambiguity_for_selector(pending_ambiguous, selector)
        dynamic_row_evidence = _prenav_dynamic_row_for_selector(pending_dynamic_row, selector, source_url)
        if ambiguous:
            role, accessible_name = _prenav_role_name_for_selector(pending_reanchor, selector)
        result["data"]["effective_target"] = _effective_target_text(selector, role, accessible_name)
        _record_scouted_interaction(
            ctx,
            tool_name="click",
            selector=selector,
            source_url=source_url,
            role=role,
            accessible_name=accessible_name,
            ambiguous=ambiguous,
            dynamic_row_evidence=dynamic_row_evidence,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="click", selector=selector, source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
            # A download this click produced is proof the affordance works, so it outranks the
            # href-shape prediction — and is the only source that sees a command-URL download.
            await _maybe_attach_observed_download_target(ctx, result, selector=selector, url=url)
            await _maybe_attach_observed_render_target(ctx, result, selector=selector, url=url)
        if page_evidence is not None:
            _attach_scout_page_summary(result, page_evidence)
            if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
                await _maybe_attach_reached_download_target(ctx, result, url=url, page_evidence=page_evidence)
    account_no_progress_interaction_click(ctx, result)
    try:
        await _attach_reperception_targets_on_non_advancing_click(result, raw, ctx, attempted_selector)
    except Exception:
        LOG.warning("copilot_click_reperception_attach_failed", exc_info=True)
    await _capture_post_interaction_screenshot(ctx)
    return result


_SETTLE_GROUNDED_TARGETS_INSTRUCTION = (
    "The page updated after your click; click one of the grounded targets below instead of re-evaluating."
)


def _grounded_actionable_targets(parsed: dict[str, Any] | None) -> list[dict[str, str]]:
    if parsed is None:
        return []
    return _actionable_targets_for_result(_click_affordance_target_identities(parsed))


def _attach_actionable_targets(result: dict[str, Any], targets: list[dict[str, str]], *, settle_steer: bool) -> None:
    if not targets:
        return
    container = result.get("data")
    if not isinstance(container, dict):
        container = {}
        result["data"] = container
    container["actionable_targets"] = targets
    if settle_steer:
        container["next_action"] = "click"
        container["next_action_reason"] = _SETTLE_GROUNDED_TARGETS_INSTRUCTION
    LOG.info(
        "copilot_click_grounded_targets_attached",
        target_count=len(targets),
        via="settle" if settle_steer else "reperception",
    )


async def _safe_composition_evidence(ctx: AgentContext, url: str, *, timeout_seconds: float) -> dict[str, Any] | None:
    if timeout_seconds <= 0:
        return None
    try:
        return await _composition_get_structured_evidence(
            ctx,
            inspected_url=url,
            current_url=url,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None


async def _click_failure_warrants_settle(
    ctx: AgentContext, attempted_selector: str | None, *, timeout_seconds: float
) -> bool:
    """Pay the bounded settle only for a precondition-gated control: the attempted selector resolves to
    a live element now (exists-but-was-not-actionable). A zero-match invented selector, or one that can
    no longer be read, never warrants it, so the common invented-selector path stays fast."""
    match_count = await _selector_live_match_count(ctx, attempted_selector, timeout_seconds=timeout_seconds)
    return match_count is not None and match_count >= 1


async def _settle_grounded_targets_on_pending_update(
    ctx: AgentContext,
    *,
    url: str,
    attempted_selector: str | None,
) -> list[dict[str, str]]:
    """Bounded re-probe of the side-effect-free extractor until a precondition-gated control's
    grounded targets appear (a just-issued AJAX populated the page) or the settle deadline expires."""
    deadline = time.monotonic() + settings.COPILOT_CLICK_SETTLE_DEADLINE_SECONDS

    def _remaining_budget(default: float) -> float:
        return min(default, max(0.0, deadline - time.monotonic()))

    warrants_budget = _remaining_budget(_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS)
    if warrants_budget <= 0:
        return []
    if not await _click_failure_warrants_settle(ctx, attempted_selector, timeout_seconds=warrants_budget):
        return []
    probes_run = 0
    for _ in range(max(0, settings.COPILOT_CLICK_SETTLE_MAX_PROBES)):
        sleep_budget = _remaining_budget(settings.COPILOT_CLICK_SETTLE_DELAY_SECONDS)
        if sleep_budget <= 0 and time.monotonic() >= deadline:
            break
        await asyncio.sleep(sleep_budget)
        probe_budget = _remaining_budget(settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS)
        if probe_budget <= 0:
            break
        probes_run += 1
        targets = _grounded_actionable_targets(await _safe_composition_evidence(ctx, url, timeout_seconds=probe_budget))
        if targets:
            LOG.info("copilot_click_settle_reprobe", warranted=True, probes_run=probes_run, target_count=len(targets))
            return targets
    LOG.info("copilot_click_settle_reprobe", warranted=True, probes_run=probes_run, target_count=0)
    return []


async def _attach_reperception_targets_on_non_advancing_click(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
    attempted_selector: str | None,
) -> None:
    """Re-perceive after a click that did not advance the build (a zero-match failure or a
    successful-but-hollow observe), so the next attempt copies a grounded selector instead of
    re-emitting an invented one. Side-effect-free parse: the no-progress counter and
    last_scout_act_observe_outcome are settled by the caller and left untouched."""
    ok = bool(result.get("ok"))
    non_advancing = (not ok) or ctx.last_scout_act_observe_outcome == "hollow"
    if not non_advancing:
        return
    existing = result.get("data")
    if isinstance(existing, dict) and existing.get("actionable_targets"):
        return
    if ok:
        immediate = _grounded_actionable_targets(ctx.last_scout_act_observe_packet)
        if immediate:
            _attach_actionable_targets(result, immediate, settle_steer=False)
            return
    url, _ = await _resolve_url_title(raw, ctx)
    if not url:
        return
    parsed = await _safe_composition_evidence(
        ctx, url, timeout_seconds=settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS
    )
    targets = _grounded_actionable_targets(parsed)
    if targets:
        _attach_actionable_targets(result, targets, settle_steer=False)
        return
    settled = await _settle_grounded_targets_on_pending_update(ctx, url=url, attempted_selector=attempted_selector)
    _attach_actionable_targets(result, settled, settle_steer=True)


_TYPE_READBACK_SETTLE_SECONDS = 0.3


async def _read_scout_field_value(ctx: AgentContext, selector: str) -> str | None:
    """Read a field's current value through the discovery MCP surface, or None when unavailable."""
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    try:
        readback = await asyncio.wait_for(
            server.call_internal_tool("skyvern_get_value", {"selector": selector}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("scout field-value read failed; leaving the value unread", exc_info=True)
        return None
    if not isinstance(readback, dict) or not readback.get("ok"):
        return None
    value = (readback.get("data") or {}).get("value")
    return value if isinstance(value, str) else None


_XPATH_SELECTOR_RE = re.compile(r"^\s*(?:xpath=|\(?/)")
_ENGINE_PREFIXED_SELECTOR_RE = re.compile(r"^\s*[A-Za-z][\w-]*=")


def _selector_supports_control_state_probe(selector: str) -> bool:
    """Only bare CSS and XPath resolve inside the probe expression. A Playwright-engine selector
    (``role=``, ``text=``, or a ``>>`` chain) would throw in-page and cost a round-trip to learn nothing.
    """
    if _XPATH_SELECTOR_RE.match(selector):
        return True
    return ">>" not in selector and not _ENGINE_PREFIXED_SELECTOR_RE.match(selector)


async def _probe_scout_control_state(ctx: AgentContext, selector: str) -> tuple[bool | None, bool | None]:
    """Return (readonly, disabled) booleans for a captured type_text target, either None when the control
    state cannot be resolved (unavailable surface, unresolvable/non-CSS-or-XPath selector). No raw field
    value crosses this boundary — the evaluate reads attributes only.
    """
    if not isinstance(selector, str) or not selector.strip():
        return None, None
    if not _selector_supports_control_state_probe(selector):
        return None, None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None, None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_evaluate", {"expression": scout_control_state_expression(selector)}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("scout control-state probe failed; treating editability as unknown", exc_info=True)
        return None, None
    if not isinstance(result, dict) or not result.get("ok"):
        return None, None
    state = (result.get("data") or {}).get("result")
    if not isinstance(state, dict):
        return None, None
    return bool(state.get("readonly")), bool(state.get("disabled"))


def _significant_character_count(value: str) -> int:
    return sum(1 for character in value if character.isalnum())


async def _verify_scout_type_landed(
    ctx: AgentContext,
    *,
    selector: str,
    typed_length: Any,
    prefetched_value: str | None = None,
) -> dict[str, Any] | None:
    """Confirm a non-empty type actually entered the field, else return a failure.

    A marketing/cookie overlay can consume the focus or keystrokes — the field
    stays empty while `skyvern_type` still reports success (the first interaction
    on an overlaid page often just dismisses the overlay). Read the field back; a
    field still empty after a non-empty type means the input did not land. Only
    fires when there is a selector to read and a positive typed length, so it never
    second-guesses intent-only types or masked/formatted values, which keep a
    non-empty value.
    """
    if not isinstance(selector, str) or not selector.strip():
        return None
    if not isinstance(typed_length, int) or typed_length <= 0:
        return None
    if getattr(ctx, "discovery_mcp_server", None) is None:
        return None

    value = prefetched_value if prefetched_value is not None else await _read_scout_field_value(ctx, selector)
    if isinstance(value, str) and value.strip() == "":
        # A controlled/React input can mirror its value asynchronously, so a first read may be
        # transiently empty; settle briefly and re-read once before declaring the type lost.
        await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
        value = await _read_scout_field_value(ctx, selector)
    if isinstance(value, str) and value.strip() == "":
        return {
            "ok": False,
            "error": (
                "type_text reported success but the field is still empty — an overlay "
                "(cookie/marketing popup) likely consumed the keystrokes or focus. "
                "Re-inspect the current page and retry typing into the target field; "
                "the overlay is usually dismissed by that first interaction."
            ),
        }
    # A field holding more than was typed means the text joined a value already there — a re-render
    # can move a selector onto a neighbouring input, so a second fill appends to the wrong field and
    # leaves the intended one empty. Count only alphanumerics: a phone/card/date input that inserts
    # its own separators grows past the typed length without anything having landed in the wrong
    # field, and rejecting those would fail every auto-formatting form.
    if isinstance(value, str) and _significant_character_count(value) > typed_length:
        return {
            "ok": False,
            "error": (
                f"type_text landed in a field that already held a value: {selector!r} now holds "
                f"{_significant_character_count(value)} characters after typing {typed_length}. The selector "
                "likely resolved to a different input than intended, leaving the target field empty. "
                "Re-inspect the current page, confirm which input each value belongs in, clear the field, "
                "and retry."
            ),
        }
    return None


async def _type_text_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    pending_typed_value = ctx.pending_scout_typed_value
    ctx.pending_scout_typed_value = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector = _selector_from_tool_data(data)
        typed_length = data.get("text_length", 0)
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "selector": selector,
            "typed_length": typed_length,
            "url": url,
        }
        has_landing_probe = (
            isinstance(selector, str) and bool(selector.strip()) and isinstance(typed_length, int) and typed_length > 0
        )
        field_value = await _read_scout_field_value(ctx, selector) if has_landing_probe else None
        control_readonly, control_disabled = (
            await _probe_scout_control_state(ctx, selector) if has_landing_probe else (None, None)
        )
        is_readonly_or_disabled = bool(control_readonly) or bool(control_disabled)
        landing_failure = await _verify_scout_type_landed(
            ctx, selector=selector, typed_length=typed_length, prefetched_value=field_value
        )
        if landing_failure is not None and not is_readonly_or_disabled:
            return landing_failure
        _mark_pending_browser_interaction_observation(ctx, tool_name="type_text", url=url)
        role, accessible_name = await _resolve_scout_role_name(ctx, selector)
        value_landed = (
            isinstance(typed_length, int)
            and typed_length > 0
            and isinstance(pending_typed_value, str)
            and len(pending_typed_value) == typed_length
        )
        typed_value = (
            safe_typed_default_value(
                pending_typed_value,
                selector=selector,
                role=role,
                accessible_name=accessible_name,
            )
            if value_landed
            else None
        )
        raw_typed_value = pending_typed_value if value_landed and isinstance(pending_typed_value, str) else ""
        if is_readonly_or_disabled and isinstance(pending_typed_value, str):
            settled_value = field_value
            if settled_value is not None and settled_value != pending_typed_value:
                await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
                settled_value = await _read_scout_field_value(ctx, selector)
            control_value_satisfied: bool | None = (
                settled_value == pending_typed_value if settled_value is not None else None
            )
        else:
            control_value_satisfied = None
        _record_scouted_interaction(
            ctx,
            tool_name="type_text",
            selector=selector,
            source_url=source_url,
            typed_length=typed_length,
            typed_value=typed_value or "",
            raw_typed_value=raw_typed_value,
            role=role,
            accessible_name=accessible_name,
            control_readonly=control_readonly,
            control_disabled=control_disabled,
            control_value_satisfied=control_value_satisfied,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="type_text", selector=selector, source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if page_evidence is not None:
            _attach_scout_page_summary(result, page_evidence)
    return result


# composition_evidence caps a captured value_text at 240 chars, so a witness longer than that cannot
# match one and is only page text riding along in the trajectory.
_WITNESSED_READ_VALUE_MAX_CHARS = 240


def _sole_scalar_leaf(result: object) -> object | None:
    """The one scalar anywhere inside a result, or None if it holds none or more than one.

    A read that answers with a value often returns it wrapped — a one-key dict, a one-element list —
    and the wrapper says nothing about which element on the page carries it. More than one scalar
    means the read described a page instead of answering with a value, so the whole structure is
    walked; stopping early would call a shallow scalar sole while a deeper one went unseen.
    """
    found: object | None = None
    pending: list[object] = [result]
    while pending:
        current = pending.pop()
        if isinstance(current, (str, int, float)):
            if found is not None:
                return None
            found = current
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple, set)):
            pending.extend(current)
    return found


def _witnessed_scalar_value(result: object) -> str:
    """The scalar a read returned, so a later binding can find what still carries it."""
    scalar = _sole_scalar_leaf(result)
    if scalar is None or isinstance(scalar, bool):
        return ""
    text = str(scalar).strip()
    if not text or len(text) > _WITNESSED_READ_VALUE_MAX_CHARS:
        return ""
    # Redacted text cannot support the exact match this exists for, so a value redaction would alter
    # is dropped rather than kept in a form nothing can bind.
    if redact_raw_secrets_for_prompt(text) != text:
        return ""
    return text


def _record_scouted_read(
    ctx: AgentContext,
    *,
    expression: str,
    data: dict[str, Any],
    url: str,
    declared_output_path: str | None = None,
) -> None:
    """Keep a read the scout proved on the live page so authoring replays it instead of guessing a
    locator for a value it has already seen. A structured result is kept as readily as a scalar: the
    shape is what a later binding needs. Naming never gates keeping the evidence."""
    result = data.get("result")
    if not expression or result is None or result == "":
        return
    if isinstance(result, (list, dict)) and not result:
        return
    output_path = "output.scouted_read"
    witnessed = _witnessed_scalar_value(result)
    policy = ctx.request_policy
    requested: set[str] = set()
    if isinstance(policy, RequestPolicy):
        # Derivation's set, not the graded criteria projected to output_path: rekeying clears that
        # field while the criterion stays graded, so a witnessed value stayed anonymous and
        # completion verification had nothing for an outcome the scout had already seen.
        requested = requested_output_paths_for_derivation(ctx)
        named = sorted(path for path in requested if not is_canonical_request_slot_path(path))
        # A rekeyed requested output carries a digest instead of a word, which still keys the
        # producer to the criterion it has to satisfy. Reading into an anonymous path instead leaves
        # completion verification with no evidence for an outcome the scout already demonstrated.
        candidates = named or sorted(requested)
        # Owning the requested output takes a value the read actually observed, not merely being the
        # only path on offer: a read that came back holding no single value describes the page, and
        # promoting it leaves the run claiming an answer nothing witnessed.
        if len(candidates) == 1 and witnessed:
            output_path = candidates[0]
    # Counting candidates can only attribute a read when the turn requests a single output; a request
    # for several fields needs the reader to say which one it just read.
    if declared_output_path and (
        declared_output_path in requested or (not requested and declared_output_path.startswith("output."))
    ):
        output_path = declared_output_path
    # The same tool inspects a page and reads a value, so attribution by elimination promotes an
    # inspection to the requested output. Report what owning the output by declaration alone would
    # bind, so the cost of that rule is known before it decides anything.
    LOG.info(
        "copilot_scouted_read_attribution",
        declared=bool(declared_output_path),
        attributed_by="declaration" if declared_output_path == output_path else "elimination",
        bound_output_path=output_path,
        declaration_only_output_path=declared_output_path or "output.scouted_read",
        requested_output_count=len(requested),
        read_result_shape=type(result).__name__,
    )
    interaction: ScoutedInteraction = {
        "tool_name": "read_value",
        "read_expression": expression,
        "read_output_path": output_path,
        "read_output_path_source": "declared" if declared_output_path == output_path else "elimination",
        "read_result_shape": type(result).__name__,
    }
    if witnessed:
        interaction["read_result_value"] = witnessed
    if url:
        interaction["source_url"] = url
    ctx.scout_trajectory.append(interaction)


def _unread_requested_output_paths(ctx: AgentContext) -> list[str]:
    """Requested outputs no read has yet answered with a value.

    A read that only gathered candidates still names the path it was aiming at, so counting every
    read as a claim made probing indistinguishable from progress: the turn could inspect a tile
    repeatedly and reach authoring with nothing bound to the output it was asked for.
    """
    # The same set derivation binds and the offer names: rekeying clears a criterion's output_path while it stays
    # graded, so projecting the graded criteria alone loses the path the binder still owes (SKY-13226).
    requested = requested_output_paths_for_derivation(ctx)
    if not requested:
        return []
    claimed = {
        str(interaction.get("read_output_path") or "")
        for interaction in ctx.scout_trajectory
        if interaction.get("tool_name") == "read_value" and interaction.get("read_result_value") is not None
    }
    return sorted(requested - claimed)


async def resolve_requested_output_designation_from_page_evidence(ctx: AgentContext) -> None:
    """Decide which value this packet shows for a requested output nothing has bound yet.

    Fires on the page evidence rather than on an authoring step, a tool the model chose to call, or
    a question it happened to ask — all of which can precede the value's render and none of which
    re-fire after it (SKY-13485). Selecting nothing is a first-class answer: no write is refused and
    a structurally different packet can offer the decision again.
    """
    # tools/__init__ imports this module, so the acceptance helper cannot be imported at top level.
    from skyvern.forge.sdk.copilot.tools import _accept_requested_output_reads

    if not requested_output_paths_for_derivation(ctx):
        return
    opportunity = designation_opportunity(
        unbound_paths=unbound_requested_output_paths_for_designation(ctx),
        flow_evidence=ctx.flow_evidence,
        resolved_fingerprints=ctx.resolved_designation_fingerprints,
    )
    if opportunity is None:
        return
    handler = await resolve_main_copilot_handler(ctx.workflow_permanent_id, ctx.organization_id)
    if handler is None:
        return
    prompt = prompt_engine.load_prompt(
        template=RESOLUTION_PROMPT_TEMPLATE,
        requested_paths=render_requested_paths(opportunity, _requested_output_labels_by_path(ctx)),
        candidates=render_candidates(opportunity),
        page_context=opportunity.page_context,
    )
    try:
        raw = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name=RESOLUTION_PROMPT_TEMPLATE),
            timeout=settings.COPILOT_OUTPUT_DESIGNATION_TIMEOUT_SECONDS,
        )
    except Exception:
        # Deliberately leaves the fingerprint unrecorded: a judge that timed out decided nothing, and
        # this page is the only one that can answer for this path.
        LOG.info("copilot_requested_output_designation_resolution", decision="unavailable", exc_info=True)
        return
    reads = coerce_resolution(raw, opportunity)
    LOG.info(
        "copilot_requested_output_designation_resolution",
        decision="selected" if reads else "none",
        requested_paths=list(opportunity.unbound_paths),
        candidate_labels=[candidate.label for candidate in opportunity.candidates],
        selected=[{"output_path": read["output_path"], "candidate_label": read["label"]} for read in reads],
    )
    if not reads:
        # A judged abstention is a decision; the same offer should not be re-asked.
        ctx.resolved_designation_fingerprints.add(opportunity.fingerprint)
        return
    rejected = await _accept_requested_output_reads(ctx, reads, offered_by="page_evidence_resolution")
    if len(rejected) < len(reads):
        # Only a designation the live page actually pinned settles this offer. A probe that missed
        # because the metric ticked between capture and probe left the path unbound, and the
        # fingerprint excludes values, so recording it here would suppress the tile forever.
        ctx.resolved_designation_fingerprints.add(opportunity.fingerprint)
    # Bind now, at the observation that resolved it: reach, completeness and ownership all read the
    # retained plan, and deriving it later inside imposition let the same trajectory be unowned on
    # one attempt and owned on the next.
    requested_output_extraction_plan(ctx)


async def _evaluate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    ctx.scout_observation_contract = None
    data = result.get("data")
    if not result.get("ok") or not isinstance(data, dict) or not data:
        _reset_evaluate_tracker(ctx)
        return result
    _mark_page_inspected(ctx)
    data.pop("sdk_equivalent", None)
    if "url" not in data:
        url, _ = await _resolve_url_title(raw, ctx)
        if url:
            data["url"] = url
    url = str(data.get("url") or "")
    title = str(data.get("title") or "")
    if not title:
        _, title = await _resolve_url_title(raw, ctx)
    observation_step = _record_composition_page_observation(
        ctx,
        source_tool="evaluate",
        url=url,
        title=title,
        observed_data=data,
        append_to_flow=True,
        reached_via="auto",
    )
    if observation_step is not None:
        result["observation_step"] = observation_step
        data["observation_step"] = observation_step
    # The MCP response never echoes the expression; the pre-hook stashed it from the invocation.
    read_expression = ctx.pending_scout_read_expression or ""
    ctx.pending_scout_read_expression = None
    declared_output_path = ctx.pending_scout_read_output_path
    ctx.pending_scout_read_output_path = None
    trajectory_len_before = len(ctx.scout_trajectory)
    _record_scouted_read(
        ctx,
        expression=read_expression,
        data=data,
        url=url,
        declared_output_path=declared_output_path,
    )
    if len(ctx.scout_trajectory) > trajectory_len_before:
        recorded = ctx.scout_trajectory[-1]
        LOG.info(
            "copilot_scouted_read_recorded",
            read_result_shape=recorded.get("read_result_shape"),
            read_output_path=recorded.get("read_output_path"),
            witnessed_value_kept=bool(recorded.get("read_result_value")),
            trajectory_len=len(ctx.scout_trajectory),
        )
        claimed_path = str(recorded.get("read_output_path") or "")
        if claimed_path.startswith("output.") and not recorded.get("read_result_value"):
            # The read owns an output the run has to register, but returned a shape holding no single
            # value, so nothing downstream can say which element on the page carries the answer. Said
            # as a bare fact it went unacted on six times in one turn while the sibling signal below
            # carried a hint and was followed, so it names the next call too (SKY-13226).
            data["claimed_output_without_a_single_value"] = claimed_path
            data["claimed_output_read_hint"] = (
                f"That expression gathered candidates rather than evaluating to one value, so nothing "
                f"records what {claimed_path} is. Read the single value on its own — an expression whose "
                f"result is just that value — and pass output_path={claimed_path} on that call. If you can "
                f"see the value but not author an expression that returns just it, call "
                f'inspect_page_for_composition(target_url="current_page") with requested_output_reads naming '
                f"{claimed_path} and the value as rendered, and the page will pin it."
            )
            LOG.info(
                "copilot_scouted_read_claimed_output_without_value",
                read_output_path=claimed_path,
                read_result_shape=recorded.get("read_result_shape"),
            )
    await resolve_requested_output_designation_from_page_evidence(ctx)
    unread_requested = _unread_requested_output_paths(ctx)
    if unread_requested:
        data["requested_outputs_still_unread"] = unread_requested
        data["requested_output_read_hint"] = (
            "Read each of these values and pass output_path=<the path> on that evaluate call. A read "
            "that names any other path is kept as exploration and registers no output for the run."
        )
        LOG.info("copilot_requested_output_unread_after_read", unread=unread_requested)
    if _copilot_block_authoring_policy(
        ctx
    ) == BlockAuthoringPolicy.CODE_ONLY_BROWSER and _code_only_has_target_page_evidence(data):
        ctx.code_only_target_page_evidence_seen = True
    await _maybe_run_completion_verification_from_page_observation(
        ctx,
        url=url,
        title=title,
        observed_data=data,
    )
    await _steer_evaluate_result(ctx, result, url=url)
    await _widen_thin_evaluate_result(ctx, result, url=url)
    return result


_THIN_EVALUATE_RESULT_CHARS = 400


async def _widen_thin_evaluate_result(ctx: AgentContext, result: dict[str, Any], *, url: str) -> None:
    """Carry the page's visible text back when a read returned almost nothing.

    A narrow read that lands on the wrong one of several similarly named elements comes back short
    and plausible, and the next guess is narrow again. A page's whole visible text is usually the
    size of a few such reads, so answering the thin one with it ends the guessing.
    """
    data = result.get("data")
    if not isinstance(data, dict) or "page_visible_text" in data:
        return
    if len(json.dumps(data, default=str)) > _THIN_EVALUATE_RESULT_CHARS:
        return
    evidence = await _safe_composition_evidence(ctx, url, timeout_seconds=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS)
    excerpt = (evidence or {}).get("visible_text_excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        data["page_visible_text"] = excerpt


async def _scroll_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "direction": data.get("direction", ""),
            "amount": data.get("pixels") or data.get("amount"),
            "url": url,
        }
    return result


async def _select_option_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    pending_ambiguous = ctx.pending_scout_ambiguous
    ctx.pending_scout_ambiguous = None
    pending_reanchor = ctx.pending_scout_reanchor
    ctx.pending_scout_reanchor = None
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector = _selector_from_tool_data(data)
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="select_option", url=url)
        result["data"] = {
            "selector": selector,
            "value": data.get("value", ""),
            "url": url,
        }
        role, accessible_name = await _resolve_scout_role_name(ctx, selector)
        ambiguous = _prenav_ambiguity_for_selector(pending_ambiguous, selector)
        if ambiguous:
            role, accessible_name = _prenav_role_name_for_selector(pending_reanchor, selector)
        _record_scouted_interaction(
            ctx,
            tool_name="select_option",
            selector=selector,
            source_url=source_url,
            value=data.get("value", ""),
            role=role,
            accessible_name=accessible_name,
            ambiguous=ambiguous,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="select_option", selector=selector, source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if page_evidence is not None:
            _attach_scout_page_summary(result, page_evidence)
    return result


async def _press_key_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    if result.get("ok") and result.get("data"):
        data = result["data"]
        selector = _selector_from_tool_data(data)
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="press_key", url=url)
        result["data"] = {
            "key": data.get("key", ""),
            "selector": selector,
            "url": url,
        }
        await _bind_login_credential_for_observed_url(ctx, url, result)
        _record_scouted_interaction(
            ctx,
            tool_name="press_key",
            selector=selector,
            source_url=source_url,
            key=data.get("key", ""),
        )
    return result


def get_skyvern_mcp_alias_map() -> dict[str, str]:
    return {
        "get_block_schema": "skyvern_block_schema",
        "validate_block": "skyvern_block_validate",
        "navigate_browser": "skyvern_navigate",
        "get_browser_screenshot": "skyvern_screenshot",
        "evaluate": "skyvern_evaluate",
        "click": "skyvern_click",
        "type_text": "skyvern_type",
        "scroll": "skyvern_scroll",
        "console_messages": "skyvern_console_messages",
        "select_option": "skyvern_select_option",
        "press_key": "skyvern_press_key",
        "wait_for_either_state": "skyvern_wait_for_either_state",
    }


_EVALUATE_BASE_DESCRIPTION = (
    "Execute JavaScript in the browser and return the result. "
    "Use this to inspect DOM state, read values, or run arbitrary JS."
)
# Scout-ACT framing: a download (or row-expand / post-login) affordance exposes its terminal
# target only once its page is reached. The model reaches that page with navigate/click and
# observes it here — evaluate cannot click — so the copilot can derive the target and compile
# the terminal download step.
_EVALUATE_SCOUT_ACT_DESCRIPTION = (
    _EVALUATE_BASE_DESCRIPTION
    + " Some affordances (a download, a row-expand, a post-login area) only expose their target "
    "once the page holding them is reached. Use this tool to OBSERVE that page — it cannot click; "
    "reach the page with the navigate/click tools first. For a download, observe the page that "
    "exposes the download control rather than authoring the download yourself; the copilot then "
    "compiles the terminal download step for you."
)


def _evaluate_overlay_description(
    block_authoring_policy: BlockAuthoringPolicy | str | None = BlockAuthoringPolicy.STANDARD,
) -> str:
    if download_scout_act_required_for_policy(block_authoring_policy):
        return _EVALUATE_SCOUT_ACT_DESCRIPTION
    return _EVALUATE_BASE_DESCRIPTION


def _build_skyvern_mcp_overlays(
    block_authoring_policy: BlockAuthoringPolicy | str | None = BlockAuthoringPolicy.STANDARD,
) -> dict[str, SchemaOverlay]:
    return {
        "get_block_schema": SchemaOverlay(
            pre_hook=_get_block_schema_pre_hook,
            post_hook=_get_block_schema_post_hook,
        ),
        "validate_block": SchemaOverlay(pre_hook=_validate_block_pre_hook),
        "navigate_browser": SchemaOverlay(
            description=(
                "Navigate the debug browser to a URL. "
                "Use this to reset browser state or navigate to a starting page before running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            post_hook=_navigate_post_hook,
        ),
        "get_browser_screenshot": SchemaOverlay(
            description=(
                "Take a screenshot of the current debug browser session. "
                "Returns a base64-encoded PNG image. "
                "Use this to see what the browser looks like after running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "selector"}),
            forced_args={"inline": True},
            requires_browser=True,
            post_hook=_screenshot_post_hook,
        ),
        "evaluate": SchemaOverlay(
            description=_evaluate_overlay_description(block_authoring_policy),
            hide_params=frozenset({"session_id", "cdp_url"}),
            copilot_params={
                "output_path": {
                    "type": "string",
                    "description": (
                        "The requested output this read fills, such as 'output.visitors'. Set it "
                        "whenever the expression reads a value the user asked for, so the workflow "
                        "returns that value under that name. Read each requested value in its own "
                        "call rather than one expression returning several. Naming a path says the "
                        "expression evaluates to that one value, which is what lets the workflow "
                        "find it again; an expression that gathers candidates is exploration, so "
                        "leave the path off and name it on the follow-up read of the value itself."
                    ),
                }
            },
            requires_browser=True,
            timeout=30,
            pre_hook=_evaluate_pre_hook,
            post_hook=_evaluate_post_hook,
        ),
        "click": SchemaOverlay(
            description=(
                "Click an element in the browser by CSS selector. The click is instant and "
                "deterministic. Derive the selector from page evidence — the selectors reported "
                "by page inspection are verified to match exactly one element. When a shared "
                "class matches many elements (e.g. one button per result row), scope the "
                "selector to the specific item (its container, a unique attribute, or "
                ":nth-of-type). If a selector does not resolve, inspect the page again and "
                "derive a better one. "
                "IMPORTANT: jQuery pseudo-selectors like :contains(), :eq(), :first, "
                ":visible are NOT valid CSS. Use standard selectors: "
                "'button.download', 'a[href*=\"pdf\"]', '#submit-btn', "
                "'table tr:nth-of-type(2) td a'."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "button", "click_count", "intent"}),
            forced_args={"selector_mode": "direct"},
            requires_browser=True,
            timeout=15,
            pre_hook=_click_pre_hook,
            post_hook=_click_post_hook,
        ),
        "type_text": SchemaOverlay(
            description=(
                "Type text into an input element by CSS selector. The type is instant and "
                "deterministic. Derive the selector from page evidence; if it does not resolve, "
                "inspect the page again and derive a better one. "
                "Optionally clear the field first. Use this for form filling. "
                "NEVER type inline passwords, API keys, tokens, cookies, TOTP/OTP "
                "codes, private keys, or other raw credentials/secrets received in "
                "chat — stop and follow the CREDENTIAL HANDLING refusal rule in the "
                "system prompt instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "delay", "intent"}),
            forced_args={"selector_mode": "direct"},
            required_overrides=["text"],
            arg_transforms={"clear_first": "clear"},
            requires_browser=True,
            timeout=15,
            pre_hook=_type_text_pre_hook,
            post_hook=_type_text_post_hook,
        ),
        "scroll": SchemaOverlay(
            description=(
                "Scroll the page in a direction (up/down/left/right) by pixel amount, "
                "or scroll a specific element into view using intent or selector. "
                "Use this to reveal content below the fold."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            post_hook=_scroll_post_hook,
        ),
        "console_messages": SchemaOverlay(
            description=(
                "Read console log messages from the browser. "
                "Use level='error' to find JavaScript errors. "
                "This is a read-only diagnostic tool."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
        ),
        "select_option": SchemaOverlay(
            description=(
                "Select an option from a <select> dropdown. Provide the value to select and a "
                "CSS selector to target the element precisely. For free-text inputs, use "
                "type_text instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "timeout", "intent"}),
            forced_args={"selector_mode": "direct"},
            required_overrides=["value"],
            requires_browser=True,
            timeout=15,
            pre_hook=_select_option_pre_hook,
            post_hook=_select_option_post_hook,
        ),
        "press_key": SchemaOverlay(
            description=(
                "Press a keyboard key (Enter, Tab, Escape, ArrowDown, etc.). "
                "Optionally focus an element first via CSS selector. "
                "Use for form submission, tab navigation, or closing dialogs."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "intent"}),
            required_overrides=["key"],
            requires_browser=True,
            pre_hook=_press_key_pre_hook,
            post_hook=_press_key_post_hook,
        ),
        "wait_for_either_state": SchemaOverlay(
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
        ),
    }
