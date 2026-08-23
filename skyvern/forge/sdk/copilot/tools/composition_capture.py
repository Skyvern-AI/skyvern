from __future__ import annotations

import asyncio
import base64
import copy
import json
from typing import Any
from urllib.parse import urlparse

import structlog

from skyvern.forge.sdk.copilot.challenge_evidence import ChallengeKind, challenge_evidence_unsettled
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION as _COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    CONSENT_OBSTRUCTION_KIND,
    has_bounded_page_schema,
    has_satisfiable_collapsed_disclosure_path,
    merge_visual_composition_evidence,
    page_evidence_needs_visual_fallback,
    parse_composition_html,
    stamp_page_evidence_provenance,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP
from skyvern.forge.sdk.copilot.llm_config import resolve_fast_copilot_handler
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    finalize_runtime_authoring_repair_context_from_page_observation,
    post_run_inspection_cleanly_matches,
    repair_page_evidence_is_admissible,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import (
    _CURRENT_PAGE_INSPECTION_TARGETS,
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _append_flow_evidence,
    _composition_evidence_page_url,
    _composition_get_html,
    _composition_get_structured_evidence_result,
    _discovery_extract_current_url,
    _discovery_navigate,
    _fallback_page_info,
    _requested_capture_targets,
    _workflow_verification_evidence,
)
from .blockers import _allows_post_run_current_page_inspection_budget_bypass
from .discovery import _resolve_discovery_entry_url
from .guardrails import _authority_tool_error
from .mcp_hooks import _bind_login_credential_for_observed_url
from .scouting import (
    _clear_pending_browser_interaction_observation,
    _consume_pending_browser_interaction_observation,
    _mark_post_run_page_observed,
    _page_evidence_matches_url_identity,
)

LOG = structlog.get_logger()


_POST_RUN_REPAIR_CAPTURE_TIMEOUT_SECONDS = 30.0
_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS = 10.0
_COMPOSITION_VISUAL_SUMMARY_PROMPT_NAME = "workflow-copilot-page-evidence-vision"


def _model_facing_inspect_result(result: dict[str, Any]) -> dict[str, Any]:
    """Detach stored evidence and shed select options until the complete JSON packet fits, retaining
    each select's selector, total option count, and explicit omission fact for targeted retrieval."""
    if result.get("ok") is not True:
        return result
    shaped = copy.deepcopy(result)
    if len(json.dumps(shaped)) <= _RECENT_TOOL_OUTPUT_CHAR_CAP:
        return shaped
    data = shaped.get("data")
    if not isinstance(data, dict):
        return shaped
    for form in data.get("forms") or []:
        if not isinstance(form, dict):
            continue
        for field in form.get("fields") or []:
            if not isinstance(field, dict) or field.get("type") != "select":
                continue
            options = field.get("options")
            if not isinstance(options, list) or not options:
                continue
            option_count = field.get("option_count")
            if not isinstance(option_count, int) or isinstance(option_count, bool) or option_count < len(options):
                option_count = len(options)
            field["option_count"] = option_count
            field["options"] = []
            field["options_omitted"] = option_count > 0
            if len(json.dumps(shaped)) <= _RECENT_TOOL_OUTPUT_CHAR_CAP:
                return shaped
    return shaped


async def _composition_get_screenshot(ctx: CopilotContext) -> dict[str, Any]:
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    try:
        return await asyncio.wait_for(
            server.call_internal_tool("skyvern_screenshot", {"inline": True}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"skyvern_screenshot timed out after {_DISCOVERY_PER_CALL_TIMEOUT_SECONDS:g}s"}


def _composition_extract_screenshot_b64(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("screenshot_base64", "data", "image_base64"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _composition_visual_prompt(evidence: dict[str, Any]) -> str:
    # The DOM challenge_state / anti-bot token hits are deliberately NOT fed in:
    # the vision pass classifies obstruction-vs-challenge from the screenshot
    # alone instead of confirming the detector's anchor.
    context = {
        "page_title": evidence.get("page_title") or "",
        "current_url": evidence.get("current_url") or "",
        "form_count": len(evidence.get("forms") or []),
        "result_container_count": len(evidence.get("result_containers") or []),
        "page_obstruction_count": len(evidence.get("page_obstructions") or []),
        "visual_obstruction_candidate_count": len(evidence.get("visual_obstruction_candidates") or []),
        "schema_empty_page": evidence.get("schema_empty_page") is True,
    }
    return (
        "Summarize this screenshot for Workflow Copilot build-time page evidence. "
        "Return JSON only with keys: summary, challenge_detected, challenge_kind, "
        "challenge_location, submit_blocked, blocked_submit_controls, empty_page_visible, "
        "loading_state_visible, page_obstruction_detected, obstruction_kind, "
        "obstruction_location, underlying_page_blocked, visible_dismiss_controls, omissions. "
        "In summary, include the visible page state that would help verify an end-state outcome, "
        "such as cart items, "
        "record rows, visible identifiers, quantities, statuses, prices, confirmations, search results, "
        "or selected values when legible. "
        "Classify any visible artificial barrier from the screenshot alone: a verification challenge is a "
        "widget asking the visitor to prove they are human, or an access-denied block page; a page "
        "obstruction is a dismissible layer such as a cookie/privacy consent dialog, promo or newsletter "
        "modal, chat widget, or loading overlay. "
        f"Use challenge_kind values: {', '.join(kind.value for kind in ChallengeKind)}. "
        f"Use {ChallengeKind.CAPTCHA.value} for any widget asking the visitor to prove they are human — a "
        "checkbox, image grid, slider, puzzle, or a field for characters shown on the page itself — "
        "whatever brand it carries. A field for a one-time code the visitor was sent elsewhere is an "
        "ordinary sign-in step, not a challenge: report challenge_detected false for it. "
        f"Use obstruction_kind values: {CONSENT_OBSTRUCTION_KIND}, promo_modal, chat_widget, "
        "loading_overlay, other. A cookie/privacy consent dialog is always a page obstruction, never a "
        "challenge: report it with page_obstruction_detected true and obstruction_kind "
        f"{CONSENT_OBSTRUCTION_KIND}, and do not set challenge_detected or submit_blocked for it. "
        "Set challenge_detected to true only for a visible verification challenge, set submit_blocked to "
        "true only when that challenge visibly gates a submit/search control, and note where the barrier "
        "appears relative to the page controls. "
        "Do not include raw DOM, code, selectors, personal data, or workflow instructions. "
        "If no challenge is visible, set challenge_detected to false and submit_blocked to false. "
        "If no page obstruction is visible, set page_obstruction_detected to false. "
        "If DOM context shows a schema-empty page, set empty_page_visible to true only when the "
        "screenshot shows a settled page with no visible forms, controls, result data, challenge, "
        "or loading/progress state; set loading_state_visible to true when the page appears to be "
        "waiting, loading, redirecting, or still rendering.\n\n"
        f"DOM evidence context:\n{json.dumps(context, sort_keys=True)}"
    )


async def _composition_visual_handler(ctx: CopilotContext) -> Any | None:
    return await resolve_fast_copilot_handler(
        getattr(ctx, "workflow_permanent_id", None),
        getattr(ctx, "organization_id", None),
    )


def _normalize_visual_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = value.get("summary")
    challenge_detected = value.get("challenge_detected")
    challenge_kind = value.get("challenge_kind")
    challenge_location = value.get("challenge_location")
    submit_blocked = value.get("submit_blocked")
    blocked_submit_controls = value.get("blocked_submit_controls")
    empty_page_visible = value.get("empty_page_visible")
    loading_state_visible = value.get("loading_state_visible")
    page_obstruction_detected = value.get("page_obstruction_detected")
    obstruction_kind = value.get("obstruction_kind")
    obstruction_location = value.get("obstruction_location")
    underlying_page_blocked = value.get("underlying_page_blocked")
    visible_dismiss_controls = value.get("visible_dismiss_controls")
    omissions = value.get("omissions")
    return {
        "summary": summary if isinstance(summary, str) else "",
        "challenge_detected": challenge_detected if isinstance(challenge_detected, bool) else None,
        "challenge_kind": challenge_kind if isinstance(challenge_kind, str) else "",
        "challenge_location": challenge_location if isinstance(challenge_location, str) else "",
        "submit_blocked": submit_blocked if isinstance(submit_blocked, bool) else None,
        "empty_page_visible": empty_page_visible if isinstance(empty_page_visible, bool) else None,
        "loading_state_visible": loading_state_visible if isinstance(loading_state_visible, bool) else None,
        "page_obstruction_detected": page_obstruction_detected if isinstance(page_obstruction_detected, bool) else None,
        "obstruction_kind": obstruction_kind if isinstance(obstruction_kind, str) else "",
        "obstruction_location": obstruction_location if isinstance(obstruction_location, str) else "",
        "underlying_page_blocked": underlying_page_blocked if isinstance(underlying_page_blocked, bool) else None,
        "blocked_submit_controls": [item for item in blocked_submit_controls if isinstance(item, str)]
        if isinstance(blocked_submit_controls, list)
        else [],
        "visible_dismiss_controls": [item for item in visible_dismiss_controls if isinstance(item, str)]
        if isinstance(visible_dismiss_controls, list)
        else [],
        "omissions": [item for item in omissions if isinstance(item, str)] if isinstance(omissions, list) else [],
    }


async def _composition_summarize_screenshot(
    ctx: CopilotContext,
    *,
    evidence: dict[str, Any],
    screenshot_b64: str,
) -> tuple[dict[str, Any] | None, str | None]:
    handler = await _composition_visual_handler(ctx)
    if handler is None:
        return None, "workflow copilot LLM handler is not configured"
    try:
        screenshot_bytes = base64.b64decode(screenshot_b64, validate=True)
    except Exception:
        return None, "screenshot payload was not valid base64"
    try:
        response = await asyncio.wait_for(
            handler(
                prompt=_composition_visual_prompt(evidence),
                prompt_name=_COMPOSITION_VISUAL_SUMMARY_PROMPT_NAME,
                screenshots=[screenshot_bytes],
                organization_id=getattr(ctx, "organization_id", None),
                force_dict=True,
            ),
            timeout=_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return None, f"visual summary timed out after {_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS:g}s"
    except Exception as exc:
        LOG.warning("Composition screenshot visual summary failed", error=str(exc), exc_info=True)
        return None, str(exc)
    normalized = _normalize_visual_summary(response)
    if normalized is None:
        return None, "visual summary response was not a JSON object"
    return normalized, None


async def _augment_composition_evidence_with_visual_fallback(
    ctx: CopilotContext,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    screenshot_result = await _composition_get_screenshot(ctx)
    if not screenshot_result.get("ok"):
        return _composition_add_evidence_omission(
            evidence,
            f"screenshot_capture_failed: {screenshot_result.get('error', 'unknown')}",
        )
    screenshot_b64 = _composition_extract_screenshot_b64(screenshot_result)
    visual_summary, visual_error = await _composition_summarize_screenshot(
        ctx,
        evidence=evidence,
        screenshot_b64=screenshot_b64,
    )
    return merge_visual_composition_evidence(evidence, visual_summary=visual_summary, visual_error=visual_error)


def _composition_add_evidence_omission(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    merged = dict(evidence)
    omissions = [item for item in merged.get("visual_evidence_omissions") or [] if isinstance(item, str)]
    if message:
        omissions.append(message[:160])
    merged["visual_evidence_omissions"] = list(dict.fromkeys(omissions))[:5]
    return merged


def _composition_add_inspection_warning(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    merged = dict(evidence)
    warnings = [item for item in merged.get("inspection_warnings") or [] if isinstance(item, str)]
    if message:
        warnings.append(message[:240])
    merged["inspection_warnings"] = list(dict.fromkeys(warnings))[:5]
    return merged


async def _composition_evidence_after_navigation_failure(
    ctx: CopilotContext,
    *,
    inspected_url: str,
    navigation_error: str,
) -> dict[str, Any] | None:
    current_url, _ = await _fallback_page_info(ctx)
    current_url = current_url or inspected_url
    structured, structured_error = await _composition_get_structured_evidence_result(
        ctx, inspected_url=inspected_url, current_url=current_url
    )
    if structured is not None and has_bounded_page_schema(structured):
        evidence = _composition_add_inspection_warning(
            structured,
            "navigation_error_before_html_capture",
        )
        if page_evidence_needs_visual_fallback(evidence):
            evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence
    if structured_error is not None:
        # A navigation failure is the exceptional path where a screenshot can still
        # observe the page even though the bounded DOM execution context was lost.
        # Keep that visual path without paying for a generic full-DOM serialization.
        evidence = parse_composition_html("", inspected_url=inspected_url, current_url=current_url)
        evidence = _composition_add_inspection_warning(
            evidence,
            "navigation_error_before_evidence_capture",
        )
        evidence = _composition_add_inspection_warning(
            evidence,
            "structured_evidence_unavailable_after_navigation_error",
        )
        evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence if evidence.get("screenshot_used") else None
    # Same size-cap survival as the success path: a heavy page that rendered before the nav
    # error still parses via the stripped-body evaluate instead of yielding hollow evidence.
    html, html_error, html_truncated, _ = await _composition_get_html(ctx)
    if html_error is None:
        evidence = parse_composition_html(
            html,
            inspected_url=inspected_url,
            current_url=current_url,
            requested_targets=_requested_capture_targets(ctx),
        )
        evidence = _composition_add_inspection_warning(
            evidence,
            "navigation_error_before_html_capture",
        )
        if html_truncated:
            evidence = _composition_add_inspection_warning(evidence, "html_sliced_at_cap")
        evidence = await _augment_composition_evidence_with_computed_obstruction_candidates(ctx, evidence)
        if page_evidence_needs_visual_fallback(evidence):
            evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence

    evidence = parse_composition_html("", inspected_url=inspected_url, current_url=current_url)
    evidence = _composition_add_inspection_warning(
        evidence,
        "navigation_error_before_evidence_capture",
    )
    evidence = _composition_add_inspection_warning(
        evidence,
        "html_capture_failed_after_navigation_error",
    )
    evidence = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
    return evidence if evidence.get("screenshot_used") else None


def _inspection_reached_via(*, use_current_page: bool, post_run: bool, earned_interaction: bool) -> str:
    """How the just-inspected state was reached, for the flow-evidence trajectory.

    A target_url inspection navigates there itself ("navigate"); a post-run
    current-page inspection observes the page the run left behind ("post_run"); a
    normal current-page inspection counts as an interaction only when a successful
    browser action immediately earned that credit.
    """
    if not use_current_page:
        return "navigate"
    if post_run:
        return "post_run"
    return "interaction" if earned_interaction else "current_page"


def _latest_interaction_reached_flow_evidence(copilot_ctx: Any) -> tuple[int, str, dict[str, Any]] | None:
    trajectory = getattr(copilot_ctx, "flow_evidence", None)
    if not isinstance(trajectory, list):
        return None
    for entry in reversed(trajectory):
        if not isinstance(entry, dict):
            continue
        reached_via = str(entry.get("reached_via") or "")
        if reached_via not in {"interaction", "post_run"}:
            continue
        evidence = entry.get("evidence")
        step = entry.get("step")
        if isinstance(step, bool) or not isinstance(step, int) or not isinstance(evidence, dict):
            continue
        if not has_bounded_page_schema(evidence):
            continue
        observed_url = _composition_evidence_page_url(evidence)
        if observed_url:
            return step, observed_url, evidence
    return None


def _non_current_inspection_regression_error(copilot_ctx: Any, *, entry_url: str) -> dict[str, Any] | None:
    latest = _latest_interaction_reached_flow_evidence(copilot_ctx)
    if latest is None:
        return None
    observation_step, observed_url, evidence = latest
    if _page_evidence_matches_url_identity(evidence, entry_url):
        return None
    return {
        "ok": False,
        "data": {
            "current_url": observed_url,
            "observation_step": observation_step,
        },
        "error": (
            "inspect_page_for_composition would navigate away from the latest interaction-reached page "
            f'({observed_url}). Use inspect_page_for_composition(target_url="current_page") to inspect '
            "the live page, or compose from the existing page evidence and pass observation_step "
            f"{observation_step} in block_observation_refs for blocks that act on that reached page."
        ),
    }


_COMPOSITION_HOLLOW_RECAPTURE_RETRIES = 2
_COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS = 2.5
# The composition inspect navigates with `domcontentloaded`, so a heavier cap than
# the discovery walker's is safe — the navigate returns at DOM parse, well before
# this ceiling, and only a genuinely stuck load reaches it.
_COMPOSITION_NAVIGATE_TIMEOUT_SECONDS = 30.0


def _normalize_visual_obstruction_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in value:
        if len(candidates) >= 5:
            break
        if not isinstance(item, dict):
            continue
        position = item.get("position")
        coverage = item.get("coverage")
        if position not in {"fixed", "sticky"} or coverage != "viewport":
            continue
        candidates.append(
            {
                "source": "computed_style",
                "position": position,
                "coverage": "viewport",
                "has_visible_controls": item.get("has_visible_controls") is True,
            }
        )
    return candidates


def _merge_visual_obstruction_candidates(
    evidence: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not candidates:
        return evidence
    merged = dict(evidence)
    existing = [item for item in merged.get("visual_obstruction_candidates") or [] if isinstance(item, dict)]
    for candidate in candidates:
        if len(existing) >= 5:
            break
        if candidate not in existing:
            existing.append(candidate)
    merged["visual_obstruction_candidates"] = existing[:5]
    return merged


async def _composition_get_computed_visual_obstruction_candidates(copilot_ctx: Any) -> list[dict[str, Any]]:
    server = getattr(copilot_ctx, "discovery_mcp_server", None)
    if server is None:
        return []
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    value = (result.get("data") or {}).get("result")
    return _normalize_visual_obstruction_candidates(value)


async def _augment_composition_evidence_with_computed_obstruction_candidates(
    copilot_ctx: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if page_evidence_needs_visual_fallback(evidence) or not has_bounded_page_schema(evidence):
        return evidence
    candidates = await _composition_get_computed_visual_obstruction_candidates(copilot_ctx)
    return _merge_visual_obstruction_candidates(evidence, candidates)


def _composition_capture_settled(evidence: dict[str, Any]) -> bool:
    return (
        has_bounded_page_schema(evidence) or has_satisfiable_collapsed_disclosure_path(evidence)
    ) and not challenge_evidence_unsettled(evidence)


async def _capture_composition_evidence(
    copilot_ctx: Any,
    *,
    inspected_url: str,
    current_url: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Capture page evidence, using HTML only to enrich a valid but unsettled structured packet."""
    evidence: dict[str, Any] | None = None
    html_truncated = False
    used_structured = False
    skip_raw = False
    for attempt in range(_COMPOSITION_HOLLOW_RECAPTURE_RETRIES + 1):
        structured, structured_error = await _composition_get_structured_evidence_result(
            copilot_ctx, inspected_url=inspected_url, current_url=current_url
        )
        if structured_error is not None:
            if evidence is not None:
                break
            return None, structured_error
        if structured is not None:
            evidence = structured
            used_structured = True
            if _composition_capture_settled(evidence):
                break
            if attempt < _COMPOSITION_HOLLOW_RECAPTURE_RETRIES:
                await asyncio.sleep(_COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS)
                continue
        # get_html reads body only, so re-parsing cannot see a title-derived challenge signal.
        # Guard the reparse itself, not one route to it: re-looks may be exhausted, or the
        # extractor may have blinked on a later attempt while a signalled packet is in hand.
        if used_structured and challenge_evidence_unsettled(evidence):
            break
        html, html_error, html_truncated, used_stripped = await _composition_get_html(copilot_ctx, skip_raw=skip_raw)
        if html_error is not None:
            if evidence is not None:
                break
            return None, html_error
        # On a heavy page the raw get_html serialization is dropped over the MCP size cap and
        # falls back to the stripped read; once that happens, settle-and-recapture via the
        # stripped path only so a slow page is still retried without re-serializing the full DOM.
        if used_stripped:
            skip_raw = True
        evidence = parse_composition_html(
            html,
            inspected_url=inspected_url,
            current_url=current_url,
            requested_targets=_requested_capture_targets(copilot_ctx),
        )
        used_structured = False
        if _composition_capture_settled(evidence):
            break
        if attempt < _COMPOSITION_HOLLOW_RECAPTURE_RETRIES:
            await asyncio.sleep(_COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS)
    if evidence is not None and html_truncated and not used_structured:
        evidence = _composition_add_inspection_warning(evidence, "html_sliced_at_cap")
    # Structured evidence already carries computed obstruction candidates; only the get_html path augments.
    if evidence is not None and not used_structured:
        evidence = await _augment_composition_evidence_with_computed_obstruction_candidates(copilot_ctx, evidence)
    if evidence is not None and (
        page_evidence_needs_visual_fallback(evidence)
        or (evidence.get("schema_empty_page") is True and not has_bounded_page_schema(evidence))
    ):
        evidence = await _augment_composition_evidence_with_visual_fallback(copilot_ctx, evidence)
    return evidence, None


async def _read_run_session_page_evidence(
    ctx: CopilotContext,
    *,
    run_session_id: str,
    current_url: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """The discovery extractor reads ctx.browser_session_id per call, so the rebind targets the run
    session and is restored in a finally. The browser layer can substitute a replacement session for a
    closed one, so the id the capture actually observed is read back before the restore."""
    prior_session_id = ctx.browser_session_id
    ctx.browser_session_id = run_session_id
    observed_session_id: str | None = run_session_id
    observation_error: str | None = None
    evidence: dict[str, Any] | None = None
    try:
        evidence, observation_error = await asyncio.wait_for(
            _capture_composition_evidence(ctx, inspected_url=current_url, current_url=current_url),
            timeout=_POST_RUN_REPAIR_CAPTURE_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("Post-run run-session page capture failed", exc_info=True)
        observation_error = observation_error or "Post-run page capture against the run session failed."
        evidence = None
    finally:
        # Read before the restore, which is the only point where the substituted id is still visible.
        observed_session_id = ctx.browser_session_id
        ctx.browser_session_id = prior_session_id

    if not observed_session_id:
        # A mid-capture session create that fails clears the id, and an unknown source id grants
        # post-run identity, so an unprovable source has to drop the packet rather than launder it.
        return None, None, observation_error or "Post-run page capture lost its browser session."
    return (evidence if isinstance(evidence, dict) else None), observed_session_id, observation_error


def _post_run_page_source_session_id(ctx: CopilotContext, run_id: str | None) -> str | None:
    """The run's own browser session, when a current-page look lands after a run that executed in a
    different browser than the scout one."""
    if not run_id:
        return None
    run_session_id = ctx.last_run_blocks_browser_session_id
    if not run_session_id or run_session_id == ctx.browser_session_id:
        return None
    return run_session_id


def _preserves_existing_post_run_page_evidence(
    copilot_ctx: CopilotContext,
    stamped: dict[str, Any],
    *,
    run_id: str,
) -> bool:
    if stamped.get("observed_after_workflow_run") is True and repair_page_evidence_is_admissible(stamped):
        return False
    return post_run_inspection_cleanly_matches(copilot_ctx.composition_page_evidence, run_id)


def store_post_run_page_evidence(
    copilot_ctx: Any,
    evidence: dict[str, Any],
    *,
    run_id: str,
    current_url: str,
    source_browser_session_id: str | None,
    run_browser_session_id: str | None,
) -> tuple[dict[str, Any], bool]:
    """Returns the freshly stamped packet and whether an existing same-run packet was preserved
    instead of being replaced by it."""
    stamped = stamp_page_evidence_provenance(
        evidence,
        source_browser_session_id=source_browser_session_id,
        run_id=run_id,
        run_browser_session_id=run_browser_session_id,
    )
    LOG.info(
        "copilot_post_run_page_evidence_sourced",
        run_id=run_id,
        source_browser_session_id=source_browser_session_id,
        run_browser_session_id=run_browser_session_id,
        matched=source_browser_session_id == run_browser_session_id,
        granted=stamped.get("observed_after_workflow_run") is True,
    )
    if current_url and not stamped.get("current_url"):
        stamped["current_url"] = current_url
    if _preserves_existing_post_run_page_evidence(copilot_ctx, stamped, run_id=run_id):
        return stamped, True
    copilot_ctx.composition_page_evidence = stamped
    page_title = stamped.get("page_title")
    if isinstance(page_title, str) and page_title:
        _workflow_verification_evidence(copilot_ctx).page_title = page_title[:160]
    return stamped, False


def _normalized_inspect_url(url: str | None) -> str | None:
    """Normalized full URL for strict same-page comparison, or None when not comparable.

    Preserves scheme, the path's trailing slash, query, and fragment so distinct rendered
    states (http vs https, /p vs /p/, ?q=a vs ?q=b, hash-routed SPA states) never collide;
    only netloc case and an empty root path are normalized.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}{query}{fragment}"


def _same_inspect_target(live_url: str | None, target_url: str | None) -> bool:
    """True when the live page is the exact page a URL-target inspect would navigate to.

    Strict full-URL equality, so a different scheme, trailing slash, query, or fragment
    still navigates. Used to skip the re-navigation when the agent is already standing on
    the requested page.
    """
    live_key = _normalized_inspect_url(live_url)
    target_key = _normalized_inspect_url(target_url)
    return live_key is not None and live_key == target_key


async def _inspect_page_for_composition_impl(
    copilot_ctx: Any,
    target_url: str,
) -> dict[str, Any]:
    """Inspect a known target page and store form/search evidence on ctx.

    This is composition context, not workflow YAML. It is intentionally separate
    from `discover_workflow_entrypoint`: discovery answers "which page?";
    inspection answers "what fields and controls are actually on this page?".
    """
    arguments = {"target_url": target_url}
    authority_error = _authority_tool_error(copilot_ctx, "inspect_page_for_composition")
    if authority_error:
        result = {"ok": False, "error": authority_error}
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    use_current_page = (target_url or "").strip().lower() in _CURRENT_PAGE_INSPECTION_TARGETS
    if not use_current_page:
        _clear_pending_browser_interaction_observation(copilot_ctx)
    bypass_budget_for_post_run_current_page = _allows_post_run_current_page_inspection_budget_bypass(
        copilot_ctx,
        use_current_page=use_current_page,
    )

    run_id = getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
    entry_url: str
    kind: str
    run_page_source_session_id: str | None = None
    observed_run_session_id: str | None = None
    if use_current_page:
        run_page_source_session_id = _post_run_page_source_session_id(copilot_ctx, run_id)
        current_url, _ = await _fallback_page_info(copilot_ctx, run_page_source_session_id)
        entry_url = current_url or "current_page"
        kind = "current_page"
    else:
        resolved_entry_url, kind = _resolve_discovery_entry_url(target_url)
        if resolved_entry_url is None:
            result = {
                "ok": False,
                "data": None,
                "error": "inspect_page_for_composition requires a URL, domain with an explicit path, or target_url='current_page'.",
            }
            record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
            return result
        entry_url = resolved_entry_url
        regression_error = _non_current_inspection_regression_error(copilot_ctx, entry_url=entry_url)
        if regression_error is not None:
            record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, regression_error)
            return regression_error

    # Skip re-navigation when the inspect target is the page the browser is already on. A
    # passive client-side redirect can move the browser without a tool, so for a URL target
    # confirm against the live URL; for current_page the live URL is the target by definition.
    if use_current_page:
        inspect_target_url = current_url
        on_target_page = True
    else:
        live_url, _ = await _fallback_page_info(copilot_ctx)
        on_target_page = _same_inspect_target(live_url, entry_url)
        inspect_target_url = live_url if on_target_page else entry_url

    # Structured inspection is not rationed. Capping it sent the agent to hand-rolled `evaluate`
    # probes once the budget ran out — on a heavy app that meant 22 small DOM reads in place of a
    # handful of structured looks, and the turn's wall clock spent without understanding the page.
    # The calls stay counted for telemetry.
    evidence = None
    observation_error: str | None = None
    with copilot_span(
        "inspect_page_for_composition",
        data={"target_url_kind": kind},
    ):
        if on_target_page:
            # current_page, or a URL target the agent is already on — capture without navigating.
            current_url = inspect_target_url or entry_url
            if run_page_source_session_id:
                evidence, observed_run_session_id, observation_error = await _read_run_session_page_evidence(
                    copilot_ctx, run_session_id=run_page_source_session_id, current_url=current_url
                )
            else:
                evidence, observation_error = await _capture_composition_evidence(
                    copilot_ctx, inspected_url=entry_url, current_url=current_url
                )
        else:
            nav_result = await _discovery_navigate(
                copilot_ctx,
                entry_url,
                wait_until="domcontentloaded",
                timeout_seconds=_COMPOSITION_NAVIGATE_TIMEOUT_SECONDS,
            )
            if not nav_result.get("ok"):
                nav_error = str(nav_result.get("error") or "unknown")
                failure_evidence = await _composition_evidence_after_navigation_failure(
                    copilot_ctx,
                    inspected_url=entry_url,
                    navigation_error=nav_error,
                )
                if failure_evidence is None:
                    result = {
                        "ok": False,
                        "data": None,
                        "error": f"inspect_page_for_composition could not navigate: {nav_error}",
                    }
                    record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
                    return result
                evidence = failure_evidence
                current_url = str(evidence.get("current_url") or entry_url)
            else:
                current_url = _discovery_extract_current_url(nav_result, entry_url)
                evidence, observation_error = await _capture_composition_evidence(
                    copilot_ctx, inspected_url=entry_url, current_url=current_url
                )

    if observation_error is not None:
        result = {
            "ok": False,
            "data": None,
            "error": f"inspect_page_for_composition could not capture page evidence: {observation_error}",
        }
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    if evidence is None:
        result = {
            "ok": False,
            "data": None,
            "error": "inspect_page_for_composition could not capture page evidence.",
        }
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    if isinstance(run_id, str) and run_id:
        source_browser_session_id = (
            observed_run_session_id if run_page_source_session_id else copilot_ctx.browser_session_id
        )
        evidence, preserved_stored_evidence = store_post_run_page_evidence(
            copilot_ctx,
            evidence,
            run_id=run_id,
            current_url=current_url,
            source_browser_session_id=source_browser_session_id,
            run_browser_session_id=copilot_ctx.last_run_blocks_browser_session_id,
        )
        # A capture too hollow or too foreign to store is also too weak to move the post-run
        # observation marker; leaving the marker put keeps it describing the packet that is stored.
        if not preserved_stored_evidence:
            _mark_post_run_page_observed(
                copilot_ctx,
                source_tool="inspect_page_for_composition",
                url=current_url,
                page_evidence=evidence,
                source_browser_session_id=source_browser_session_id,
            )
    else:
        copilot_ctx.composition_page_evidence = evidence

    if not bypass_budget_for_post_run_current_page:
        copilot_ctx.page_inspection_calls_this_turn += 1
    if bypass_budget_for_post_run_current_page:
        copilot_ctx.post_run_current_page_inspection_workflow_run_id = run_id
    finalize_runtime_authoring_repair_context_from_page_observation(copilot_ctx)
    earned_interaction = False
    if use_current_page and not run_id:
        earned_interaction = _consume_pending_browser_interaction_observation(
            copilot_ctx,
            current_url=str(evidence.get("current_url") or current_url or ""),
            evidence=evidence,
        )
    reached_via = _inspection_reached_via(
        use_current_page=use_current_page,
        post_run=evidence.get("observed_after_workflow_run") is True,
        earned_interaction=earned_interaction,
    )
    observation_step = _append_flow_evidence(copilot_ctx, evidence, reached_via=reached_via)
    if observation_step is None:
        LOG.warning("copilot_flow_evidence_append_failed_no_trajectory")
    # Surface the reached page at the top level so the model registers that the
    # inspection already navigated there and does not re-issue navigate_browser.
    current_url = evidence.get("current_url") or evidence.get("inspected_url") or ""
    result = {
        "ok": True,
        "current_url": current_url,
        "reached_via": reached_via,
        "data": evidence,
    }
    await _bind_login_credential_for_observed_url(copilot_ctx, str(current_url), result)
    if observation_step is not None:
        result["observation_step"] = observation_step
    result = _model_facing_inspect_result(result)
    record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
    return result
