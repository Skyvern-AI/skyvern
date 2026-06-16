from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.build_phase import (
    BuildPhase,
    advance_to_composing,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.typed_value_policy import safe_typed_default_value, should_reject_type_text_value

from ._shared import _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
from .banned_blocks import (
    _CODE_ONLY_SELECTOR_ACTION_TOOLS,
    _CODE_ONLY_TARGET_EVIDENCE_KEYS,
    _code_only_browser_schema_guidance,
    _code_only_browser_unavailable_summary,
    _copilot_banned_block_alternatives,
    _copilot_banned_block_types,
    _copilot_block_authoring_policy,
    _copilot_block_policy,
    _render_block_policy_detail,
)
from .completion import _maybe_run_completion_verification_from_page_observation
from .page_observation import (
    _record_composition_page_observation,
    _resolve_url_title,
)
from .scouting import (
    _attach_scout_page_summary,
    _capture_scout_source_url,
    _clear_pending_browser_interaction_observation,
    _consume_scout_source_url,
    _mark_page_inspected,
    _mark_pending_browser_interaction_observation,
    _maybe_attach_reached_download_target,
    _record_scouted_interaction,
    _register_scout_interaction_observation,
    _resolve_scout_role_name,
    _steer_evaluate_result,
)

LOG = structlog.get_logger()


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
    return {
        "ok": False,
        "error": (
            f"Block type {block_type!r} is not available in the workflow copilot. "
            f"{_render_block_policy_detail(normalized, policy)} {_copilot_banned_block_alternatives(ctx)}"
        ),
    }


async def _validate_block_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
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
    return result


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
    expr = params.get("expression", "").lower()
    if ".click()" in expr or ".click(" in expr:
        return {
            "ok": False,
            "error": "Do not use evaluate to click elements. Use the 'click' tool with a CSS selector instead.",
        }
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
    await _capture_scout_source_url(ctx)
    deterministic_result = _strip_intent_for_code_only_selector_action(params, ctx, tool_name="click")
    if deterministic_result is not None:
        return deterministic_result
    selector = params.get("selector", "")
    if not selector:
        return None
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
    if should_reject_type_text_value(value=text, selector=selector, intent=intent):
        return {
            "ok": False,
            "error": (
                "type_text cannot type raw credentials, secrets, OTP/TOTP codes, API keys, tokens, or "
                "password-like values. Use the saved credential flow instead of inline secret text."
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
    await _capture_scout_source_url(ctx)
    return _strip_intent_for_code_only_selector_action(params, ctx, tool_name="select_option")


async def _press_key_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    await _capture_scout_source_url(ctx)
    return _strip_intent_for_code_only_selector_action(params, ctx, tool_name="press_key")


async def _navigate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    _clear_pending_browser_interaction_observation(ctx)
    if result.get("ok"):
        data = result.pop("data", {})
        result["url"] = data.get("url", "")
        result["next_step"] = (
            "Page loaded. You MUST now use evaluate, "
            "get_browser_screenshot, or click to inspect page content "
            "before responding."
        )
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
    _clear_pending_browser_interaction_observation(ctx)
    source_url = _consume_scout_source_url(ctx)
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, title = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="click", url=url)
        result["data"] = {
            "selector": data.get("selector", ""),
            "url": url,
            "title": title,
        }
        navigated = bool(source_url) and bool(url) and source_url != url
        role, accessible_name = await _resolve_scout_role_name(
            ctx, data.get("selector", ""), allow_browser_read=not navigated
        )
        _record_scouted_interaction(
            ctx,
            tool_name="click",
            selector=data.get("selector", ""),
            source_url=source_url,
            role=role,
            accessible_name=accessible_name,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="click", selector=data.get("selector", ""), source_url=source_url, url=url
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if page_evidence is not None:
            _attach_scout_page_summary(result, page_evidence)
            if settings.COPILOT_DOWNLOAD_SCOUT_ACT_REQUIRED_ENABLED:
                await _maybe_attach_reached_download_target(ctx, result, url=url, page_evidence=page_evidence)
    return result


_TYPE_READBACK_SETTLE_SECONDS = 0.3


async def _verify_scout_type_landed(
    ctx: AgentContext,
    *,
    selector: str,
    typed_length: Any,
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
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None

    async def _read_back() -> Any:
        try:
            readback = await asyncio.wait_for(
                server.call_internal_tool("skyvern_get_value", {"selector": selector}),
                timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
            )
        except Exception:
            LOG.debug("scout type-landed read-back failed; leaving the type result unverified", exc_info=True)
            return None
        if not isinstance(readback, dict) or not readback.get("ok"):
            return None
        return (readback.get("data") or {}).get("value")

    value = await _read_back()
    if isinstance(value, str) and value.strip() == "":
        # A controlled/React input can mirror its value asynchronously, so a first read may be
        # transiently empty; settle briefly and re-read once before declaring the type lost.
        await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
        value = await _read_back()
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
        selector = data.get("selector", "")
        typed_length = data.get("text_length", 0)
        url, _ = await _resolve_url_title(raw, ctx)
        result["data"] = {
            "selector": selector,
            "typed_length": typed_length,
            "url": url,
        }
        landing_failure = await _verify_scout_type_landed(ctx, selector=selector, typed_length=typed_length)
        if landing_failure is not None:
            return landing_failure
        _mark_pending_browser_interaction_observation(ctx, tool_name="type_text", url=url)
        role, accessible_name = await _resolve_scout_role_name(ctx, selector)
        typed_value = (
            safe_typed_default_value(
                pending_typed_value,
                selector=selector,
                role=role,
                accessible_name=accessible_name,
            )
            if isinstance(typed_length, int)
            and typed_length > 0
            and isinstance(pending_typed_value, str)
            and len(pending_typed_value) == typed_length
            else None
        )
        _record_scouted_interaction(
            ctx,
            tool_name="type_text",
            selector=selector,
            source_url=source_url,
            typed_length=typed_length,
            typed_value=typed_value or "",
            role=role,
            accessible_name=accessible_name,
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


async def _evaluate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        _mark_page_inspected(ctx)
        result["data"].pop("sdk_equivalent", None)
        if "url" not in result["data"]:
            url, _ = await _resolve_url_title(raw, ctx)
            if url:
                result["data"]["url"] = url
        url = str(result["data"].get("url") or "")
        title = str(result["data"].get("title") or "")
        if not title:
            _, title = await _resolve_url_title(raw, ctx)
        observation_step = _record_composition_page_observation(
            ctx,
            source_tool="evaluate",
            url=url,
            title=title,
            observed_data=result["data"],
            append_to_flow=True,
            reached_via="auto",
        )
        if observation_step is not None:
            result["observation_step"] = observation_step
            result["data"]["observation_step"] = observation_step
        if _copilot_block_authoring_policy(
            ctx
        ) == BlockAuthoringPolicy.CODE_ONLY_BROWSER and _code_only_has_target_page_evidence(result["data"]):
            ctx.code_only_target_page_evidence_seen = True
        await _maybe_run_completion_verification_from_page_observation(
            ctx,
            url=url,
            title=title,
            observed_data=result["data"],
        )
        await _steer_evaluate_result(ctx, result, url=url)
    return result


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
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="select_option", url=url)
        result["data"] = {
            "selector": data.get("selector", ""),
            "value": data.get("value", ""),
            "url": url,
        }
        role, accessible_name = await _resolve_scout_role_name(ctx, data.get("selector", ""))
        _record_scouted_interaction(
            ctx,
            tool_name="select_option",
            selector=data.get("selector", ""),
            source_url=source_url,
            value=data.get("value", ""),
            role=role,
            accessible_name=accessible_name,
        )
        observation_step, page_evidence = await _register_scout_interaction_observation(
            ctx, tool_name="select_option", selector=data.get("selector", ""), source_url=source_url, url=url
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
        url, _ = await _resolve_url_title(raw, ctx)
        _mark_pending_browser_interaction_observation(ctx, tool_name="press_key", url=url)
        result["data"] = {
            "key": data.get("key", ""),
            "selector": data.get("selector", ""),
            "url": url,
        }
        _record_scouted_interaction(
            ctx,
            tool_name="press_key",
            selector=data.get("selector", ""),
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
    }


_EVALUATE_BASE_DESCRIPTION = (
    "Execute JavaScript in the browser and return the result. "
    "Use this to inspect DOM state, read values, or run arbitrary JS."
)
# Scout-ACT framing: an interaction-gated affordance (a download, a row-expand, a post-login
# area) only reveals its result after it is clicked, so it must be acted on here rather than
# inspected passively. For a download the copilot then compiles the terminal download step.
_EVALUATE_SCOUT_ACT_DESCRIPTION = (
    _EVALUATE_BASE_DESCRIPTION
    + " Some results only appear AFTER an interaction (a download, a row-expand, a post-login "
    "area); scout-ACT those affordances here (click the control via JS) instead of inspecting "
    "passively. For a download, click the download control with this tool rather than authoring "
    "the download yourself — the copilot then compiles the terminal download step for you."
)


def _evaluate_overlay_description() -> str:
    if settings.COPILOT_DOWNLOAD_SCOUT_ACT_REQUIRED_ENABLED:
        return _EVALUATE_SCOUT_ACT_DESCRIPTION
    return _EVALUATE_BASE_DESCRIPTION


def _build_skyvern_mcp_overlays() -> dict[str, SchemaOverlay]:
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
            description=_evaluate_overlay_description(),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            timeout=30,
            pre_hook=_evaluate_pre_hook,
            post_hook=_evaluate_post_hook,
        ),
        "click": SchemaOverlay(
            description=(
                "Click an element in the browser. Prefer a CSS selector ALONE for a target "
                "you can identify from page evidence — a selector-only click is instant and "
                "deterministic. Use `intent` only when you cannot derive a selector: an "
                "`intent`-only click routes through a slower full-page AI scan, and if you "
                "pass both, the selector wins and the `intent` is ignored. When a shared class "
                "matches many elements (e.g. one button per result row), scope the selector to "
                "the specific item (its container, a unique attribute, or :nth-of-type) instead "
                "of relying on `intent` to disambiguate. "
                "IMPORTANT: jQuery pseudo-selectors like :contains(), :eq(), :first, "
                ":visible are NOT valid CSS. Use standard selectors: "
                "'button.download', 'a[href*=\"pdf\"]', '#submit-btn', "
                "'table tr:nth-of-type(2) td a'."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "button", "click_count"}),
            forced_args={"selector_mode": "direct"},
            requires_browser=True,
            timeout=15,
            pre_hook=_click_pre_hook,
            post_hook=_click_post_hook,
        ),
        "type_text": SchemaOverlay(
            description=(
                "Type text into an input element. Prefer a CSS selector ALONE to target the "
                "field — a selector-only type is instant and deterministic. Use `intent` only "
                "when you cannot derive a selector: an `intent`-only type routes through a "
                "slower full-page AI scan, and if you pass both, the selector wins and the "
                "`intent` is ignored. "
                "Optionally clear the field first. Use this for form filling. "
                "NEVER type inline passwords, API keys, tokens, cookies, TOTP/OTP "
                "codes, private keys, or other raw credentials/secrets received in "
                "chat — stop and follow the CREDENTIAL HANDLING refusal rule in the "
                "system prompt instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "delay"}),
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
                "selector to target the element precisely; use `intent` (alone) only when you "
                "cannot derive a selector — passing both lets the selector win and ignores the "
                "`intent`. For free-text inputs, use type_text instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "timeout"}),
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
                "Optionally focus an element first via selector or intent. "
                "Use for form submission, tab navigation, or closing dialogs."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            required_overrides=["key"],
            requires_browser=True,
            pre_hook=_press_key_pre_hook,
            post_hook=_press_key_post_hook,
        ),
    }
