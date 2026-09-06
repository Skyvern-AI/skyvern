from __future__ import annotations

import asyncio
import base64
import copy
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from skyvern.forge.sdk.copilot.challenge_evidence import ChallengeKind, challenge_evidence_unsettled
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION as _COMPOSITION_VISUAL_OBSTRUCTION_CANDIDATES_EXPRESSION,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    value_witness_read_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    _MAX_KEY_VALUE_RELATIONS,
    CONSENT_OBSTRUCTION_KIND,
    has_bounded_page_schema,
    has_satisfiable_collapsed_disclosure_path,
    interaction_evidence_is_bindable,
    merge_visual_composition_evidence,
    model_visible_composition_evidence,
    page_evidence_needs_visual_fallback,
    page_records_share_location,
    parse_composition_html,
    stamp_page_evidence_provenance,
    unresolved_requested_targets,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP, _requested_output_labels_by_path
from skyvern.forge.sdk.copilot.llm_config import resolve_fast_copilot_handler
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.output_extraction_plan import _exact_path, _value_witness_bindings
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR,
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    browser_evidence_commit_lock,
    browser_page_custody_lock,
    clear_sensitive_origin_page_taint,
    sensitive_origin_page_facts_withheld,
    sensitive_origin_page_has_active_run,
)
from skyvern.forge.sdk.copilot.runtime_authoring_repair import (
    finalize_runtime_authoring_repair_context_from_page_observation,
    post_run_inspection_cleanly_matches,
    repair_page_evidence_is_admissible,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    CapturedFrame,
    ScreenshotActionRelation,
    ScreenshotProvenance,
    enqueue_screenshot,
    screenshot_result_facts,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    register_matching_origin_run_redaction_values,
    scrub_secrets_from_structure,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import (
    _CURRENT_PAGE_INSPECTION_TARGETS,
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    AdmittedOutputRead,
    _append_flow_evidence,
    _call_internal_browser_tool,
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
from .mcp_hooks import _bind_login_credential_for_observed_url, _record_scouted_read
from .scouting import (
    _clear_pending_browser_interaction_observation,
    _consume_pending_browser_interaction_observation,
    _mark_post_run_page_observed,
    _page_evidence_matches_url_identity,
)

LOG = structlog.get_logger()


@dataclass(frozen=True)
class CompositionEvidenceCapture:
    evidence: dict[str, Any] | None
    error: str | None
    frame: CapturedFrame | None = None

    def __iter__(self) -> Iterator[dict[str, Any] | str | None]:
        """Keep legacy two-value test/caller unpacking while frame-aware funnels migrate explicitly."""
        yield self.evidence
        yield self.error


def _capture_result_parts(
    capture: CompositionEvidenceCapture | tuple[dict[str, Any] | None, str | None],
) -> tuple[dict[str, Any] | None, str | None, CapturedFrame | None]:
    if isinstance(capture, CompositionEvidenceCapture):
        return capture.evidence, capture.error, capture.frame
    evidence, error = capture
    return evidence, error, None


_POST_RUN_REPAIR_CAPTURE_TIMEOUT_SECONDS = 30.0
_COMPOSITION_VISUAL_SUMMARY_TIMEOUT_SECONDS = 10.0
_COMPOSITION_VISUAL_SUMMARY_PROMPT_NAME = "workflow-copilot-page-evidence-vision"


def _model_facing_inspect_result(result: dict[str, Any]) -> dict[str, Any]:
    """Detach stored evidence, remove locator recommendations, and fit the complete model packet."""
    if result.get("ok") is not True:
        return result
    shaped = copy.deepcopy(result)
    data = shaped.get("data")
    if isinstance(data, dict):
        shaped["data"] = model_visible_composition_evidence(data)
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


async def _composition_get_screenshot(ctx: CopilotContext, *, dispatch_session_id: str | None) -> dict[str, Any]:
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return {"ok": False, "error": "discovery MCP server not attached to context"}
    try:
        result, outcome = await asyncio.wait_for(
            _call_internal_browser_tool(
                server,
                "skyvern_screenshot",
                {"inline": True, **({"session_id": dispatch_session_id} if dispatch_session_id else {})},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
        if outcome is not None and outcome.payload_omitted:
            return {"ok": False, "error": "skyvern_screenshot payload was omitted at the MCP boundary"}
        return result
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


def _composition_visual_prompt(evidence: dict[str, Any], requested_targets: tuple[str, ...] = ()) -> str:
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
        "requested_labels": list(requested_targets),
    }
    requested_values_key = "requested_values, " if requested_targets else ""
    requested_values_rule = (
        "requested_values is a list of {label, value} objects, at most one per entry in the DOM "
        "context's requested_labels, carrying that label's value exactly as rendered on screen; omit "
        "a label whose value is not legibly visible. "
        if requested_targets
        else ""
    )
    return (
        "Summarize this screenshot for Workflow Copilot build-time page evidence. "
        f"Return JSON only with keys: summary, {requested_values_key}challenge_detected, challenge_kind, "
        "challenge_location, submit_blocked, blocked_submit_controls, empty_page_visible, "
        "loading_state_visible, page_obstruction_detected, obstruction_kind, "
        "obstruction_location, underlying_page_blocked, visible_dismiss_controls, omissions. "
        f"{requested_values_rule}"
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


def _normalize_requested_values(value: Any) -> list[dict[str, str]]:
    """Keep only label/value string pairs, in the order the vision pass returned them."""
    if not isinstance(value, list):
        return []
    pairs = [(item.get("label"), item.get("value")) for item in value if isinstance(item, dict)]
    kept: list[dict[str, str]] = []
    for label, observed in pairs:
        if not isinstance(label, str) or not isinstance(observed, str) or not label.strip() or not observed.strip():
            continue
        kept.append({"label": label.strip()[:240], "value": observed.strip()[:240]})
        if len(kept) >= _MAX_KEY_VALUE_RELATIONS:
            break
    return kept


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
        "requested_values": _normalize_requested_values(value.get("requested_values")),
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
    requested_targets: tuple[str, ...] = (),
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
                prompt=_composition_visual_prompt(evidence, requested_targets),
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


@dataclass(frozen=True)
class VisualSummaryCapture:
    summary: dict[str, Any] | None = None
    error: str | None = None
    frame: CapturedFrame | None = None
    screenshot_failure: str | None = None


async def _composition_capture_visual_summary(
    ctx: CopilotContext,
    evidence: dict[str, Any],
    requested_targets: tuple[str, ...] = (),
) -> VisualSummaryCapture:
    capture_started_at = time.monotonic()
    capture_url = evidence.get("current_url") or evidence.get("inspected_url")
    capture_session_id = getattr(ctx, "browser_session_id", None)
    screenshot_result = await _composition_get_screenshot(ctx, dispatch_session_id=capture_session_id)
    if not screenshot_result.get("ok"):
        return VisualSummaryCapture(
            screenshot_failure=f"screenshot_capture_failed: {screenshot_result.get('error', 'unknown')}"
        )
    screenshot_b64 = _composition_extract_screenshot_b64(screenshot_result)
    producer_url, producer_session_id, session_binding = screenshot_result_facts(
        screenshot_result,
        dispatch_url=str(capture_url) if capture_url else None,
        dispatch_browser_session_id=capture_session_id,
    )
    captured_frame = (
        CapturedFrame(
            b64=screenshot_b64,
            captured_at=capture_started_at,
            captured_url=producer_url,
            browser_session_id=producer_session_id,
            dispatch_url=str(capture_url) if capture_url else None,
            dispatch_browser_session_id=capture_session_id,
            producer_browser_session_id=producer_session_id,
            session_binding=session_binding,
        )
        if screenshot_b64
        else None
    )
    visual_summary, visual_error = await _composition_summarize_screenshot(
        ctx,
        evidence=evidence,
        screenshot_b64=screenshot_b64,
        requested_targets=requested_targets,
    )
    return VisualSummaryCapture(summary=visual_summary, error=visual_error, frame=captured_frame)


def _merge_visual_summary_capture(
    evidence: dict[str, Any],
    captured: VisualSummaryCapture,
) -> dict[str, Any]:
    if captured.screenshot_failure is not None:
        return _composition_add_visual_capture_omission(
            evidence, "screenshot_capture_failed", captured.screenshot_failure
        )
    return merge_visual_composition_evidence(evidence, visual_summary=captured.summary, visual_error=captured.error)


async def _augment_composition_evidence_with_visual_fallback(
    ctx: CopilotContext,
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], CapturedFrame | None]:
    captured = await _composition_capture_visual_summary(ctx, evidence)
    return _merge_visual_summary_capture(evidence, captured), captured.frame


def _composition_add_evidence_omission(evidence: dict[str, Any], message: str) -> dict[str, Any]:
    merged = dict(evidence)
    omissions = [item for item in merged.get("visual_evidence_omissions") or [] if isinstance(item, str)]
    if message:
        omissions.append(message[:160])
    merged["visual_evidence_omissions"] = list(dict.fromkeys(omissions))[:5]
    return merged


def _composition_add_visual_capture_omission(
    evidence: dict[str, Any],
    code: str,
    message: str,
) -> dict[str, Any]:
    """Record a bounded typed capture fact alongside its operator-facing detail."""
    merged = _composition_add_evidence_omission(evidence, message)
    omission_codes = [
        item for item in merged.get("visual_capture_omissions") or [] if item in {"screenshot_capture_failed"}
    ]
    if code == "screenshot_capture_failed":
        omission_codes.append(code)
    merged["visual_capture_omissions"] = list(dict.fromkeys(omission_codes))[:1]
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
    requested_reads: tuple[AdmittedOutputRead, ...] = (),
) -> tuple[dict[str, Any], CapturedFrame | None] | None:
    current_url, _ = await _fallback_page_info(ctx)
    current_url = current_url or inspected_url
    requested_targets = _seeded_capture_targets(ctx, requested_reads)
    structured, structured_error = await _composition_get_structured_evidence_result(
        ctx,
        inspected_url=inspected_url,
        current_url=current_url,
        requested_targets=requested_targets,
    )
    if structured is not None and has_bounded_page_schema(structured):
        evidence = _composition_add_inspection_warning(
            structured,
            "navigation_error_before_html_capture",
        )
        frame = None
        if page_evidence_needs_visual_fallback(evidence):
            evidence, frame = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence, frame
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
        evidence, frame = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return (evidence, frame) if evidence.get("screenshot_used") else None
    # Same size-cap survival as the success path: a heavy page that rendered before the nav
    # error still parses via the stripped-body evaluate instead of yielding hollow evidence.
    html, html_error, html_truncated, _ = await _composition_get_html(ctx, rendered_style_snapshot=True)
    if html_error is None:
        evidence = parse_composition_html(
            html,
            inspected_url=inspected_url,
            current_url=current_url,
            requested_targets=requested_targets,
        )
        evidence = _composition_add_inspection_warning(
            evidence,
            "navigation_error_before_html_capture",
        )
        if html_truncated:
            evidence = _composition_add_inspection_warning(evidence, "html_sliced_at_cap")
        evidence = await _augment_composition_evidence_with_computed_obstruction_candidates(ctx, evidence)
        frame = None
        if page_evidence_needs_visual_fallback(evidence):
            evidence, frame = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
        return evidence, frame

    evidence = parse_composition_html("", inspected_url=inspected_url, current_url=current_url)
    evidence = _composition_add_inspection_warning(
        evidence,
        "navigation_error_before_evidence_capture",
    )
    evidence = _composition_add_inspection_warning(
        evidence,
        "html_capture_failed_after_navigation_error",
    )
    evidence, frame = await _augment_composition_evidence_with_visual_fallback(ctx, evidence)
    return (evidence, frame) if evidence.get("screenshot_used") else None


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
    """Return interaction evidence for the browser's latest observed location.

    This powers a live-page protection, so it answers where the browser is now rather than whether
    the trajectory ever left that page. Historical authoring continuity is evaluated separately.
    """
    trajectory = getattr(copilot_ctx, "flow_evidence", None)
    if not isinstance(trajectory, list):
        return None
    latest_observed_evidence = next(
        (
            entry["evidence"]
            for entry in reversed(trajectory)
            if isinstance(entry, dict)
            and isinstance(entry.get("evidence"), dict)
            and _composition_evidence_page_url(entry["evidence"])
        ),
        None,
    )
    if latest_observed_evidence is None:
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
        if not interaction_evidence_is_bindable(evidence):
            continue
        if not page_records_share_location(evidence, latest_observed_evidence):
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


@dataclass(frozen=True)
class ValueWitness:
    label: str
    value: str
    output_path: str


def _seeded_capture_targets(copilot_ctx: object, requested_reads: tuple[AdmittedOutputRead, ...]) -> tuple[str, ...]:
    """The labels this turn asked capture to resolve, from declared criteria and this call's designations.

    Only a label can resolve: a target is answered by a relation whose key_text equals it, and no pass
    mints a relation keyed by a bare magnitude.
    """
    targets = list(_requested_capture_targets(copilot_ctx))
    for read in requested_reads:
        if read.label and read.label not in targets:
            targets.append(read.label)
    return tuple(targets)


def _seeded_label_owners(copilot_ctx: object, requested_reads: tuple[AdmittedOutputRead, ...]) -> dict[str, str]:
    """Each requested label exactly one output path claims, keyed to that path."""
    paths_by_label: dict[str, set[str]] = {}
    for read in requested_reads:
        if read.label:
            paths_by_label.setdefault(read.label, set()).add(read.output_path)
    owned = {label: next(iter(paths)) for label, paths in paths_by_label.items() if len(paths) == 1}
    owned.update(_singly_owned_requested_labels(copilot_ctx))
    return owned


def _singly_owned_requested_labels(copilot_ctx: object) -> dict[str, str]:
    """Requested labels exactly one output path owns, keyed to that path."""
    if not isinstance(copilot_ctx, AgentContext):
        return {}
    labels_by_path = _requested_output_labels_by_path(copilot_ctx)
    owned: dict[str, str] = {}
    for labels in labels_by_path.values():
        for label in labels:
            path = _exact_path(label, labels_by_path)
            if path is not None and label.strip():
                owned[label.strip()] = path
    return owned


def _witnesses_from_visual_summary(
    visual_summary: dict[str, Any],
    owned_labels: dict[str, str],
    unresolved: tuple[str, ...],
) -> tuple[list[ValueWitness], int]:
    """Pair each unresolved label with the value the screenshot read for it, keeping only the unambiguous.

    Aliases of one output reading one value address the same tile once, while a value two outputs
    claim, or an output whose aliases read different values, names no single element.
    """
    # The pass echoes the label as the tile renders it, while the request mints it case-folded.
    wanted = {target.strip().casefold() for target in unresolved}
    owners_by_folded_label = {label.strip().casefold(): path for label, path in owned_labels.items()}
    seen: set[tuple[str, str]] = set()
    by_value: dict[str, list[ValueWitness]] = {}
    dropped = 0
    for pair in visual_summary.get("requested_values") or []:
        label = str(pair.get("label") or "").strip()
        value = str(pair.get("value") or "").strip()
        if not label or not value:
            continue
        folded_label = label.casefold()
        if folded_label not in wanted or folded_label not in owners_by_folded_label:
            dropped += 1
            continue
        if (folded_label, value) in seen:
            continue
        seen.add((folded_label, value))
        by_value.setdefault(value, []).append(ValueWitness(label, value, owners_by_folded_label[folded_label]))
    witnesses: list[ValueWitness] = []
    for owners in by_value.values():
        if len({owner.output_path for owner in owners}) == 1:
            witnesses.append(owners[0])
            dropped += len(owners) - 1
        else:
            dropped += len(owners)
    values_by_path: dict[str, set[str]] = {}
    for witness in witnesses:
        values_by_path.setdefault(witness.output_path, set()).add(witness.value)
    kept = [witness for witness in witnesses if len(values_by_path[witness.output_path]) == 1]
    return kept, dropped + len(witnesses) - len(kept)


async def _evaluate_witness_read(copilot_ctx: object, expression: str) -> str | None:
    server = getattr(copilot_ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_evaluate", {"expression": expression}),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    value = (result.get("data") or {}).get("result")
    return value if isinstance(value, str) else None


async def _value_witness_records(
    copilot_ctx: object,
    evidence: dict[str, Any],
    witnesses: list[ValueWitness],
) -> list[dict[str, str]]:
    """The page read that returns each witnessed value, kept only when running it returns that value.

    The recorded expression is emitted verbatim into generated workflow code, so an unexecuted read
    is a claim about the page rather than an observation of it.
    """
    labels_by_path = {witness.output_path: witness.label for witness in witnesses}
    values_by_path = {witness.output_path: witness.value for witness in witnesses}
    bindings = _value_witness_bindings(
        evidence,
        values_by_path,
        channel_intact=evidence.get("key_value_relations_truncated") is not True,
    )
    records: list[dict[str, str]] = []
    for binding in bindings:
        expression = value_witness_read_expression(
            binding.selector,
            binding.selector_count,
            binding.selector_index,
            binding.child_index,
        )
        value = values_by_path[binding.output_path]
        if await _evaluate_witness_read(copilot_ctx, expression) != value:
            continue
        records.append(
            {
                "label": labels_by_path[binding.output_path],
                "value": value,
                "output_path": binding.output_path,
                "expression": expression,
            }
        )
    return records


async def _witness_unresolved_requested_targets(
    copilot_ctx: object,
    evidence: dict[str, Any],
    *,
    captured: VisualSummaryCapture,
    unresolved: tuple[str, ...],
    owned_labels: dict[str, str],
    requested_targets: tuple[str, ...],
    inspected_url: str,
    current_url: str,
) -> dict[str, Any]:
    """Re-read the page for the values this call's screenshot just read off the unresolved tiles.

    The model cannot run this itself, because a live figure ticks between calls and only the capture
    that took the frame can key a DOM pass on what the frame showed.
    """
    summary = captured.summary or {}
    witnesses, dropped = _witnesses_from_visual_summary(summary, owned_labels, unresolved)
    LOG.info(
        "copilot_composition_requested_values_seen",
        unresolved_target_count=len(unresolved),
        pair_count=len(summary.get("requested_values") or []),
        witness_count=len(witnesses),
        dropped_count=dropped,
    )
    if not witnesses:
        return evidence
    witnessed_evidence, witness_error = await _composition_get_structured_evidence_result(
        copilot_ctx,
        inspected_url=inspected_url,
        current_url=current_url,
        requested_targets=requested_targets,
        witnessed_values=tuple(witness.value for witness in witnesses),
    )
    if witnessed_evidence is None:
        LOG.info("copilot_composition_value_witness_extract_failed", error_present=bool(witness_error))
        return evidence
    merged = _merge_visual_summary_capture(witnessed_evidence, captured)
    records = await _value_witness_records(copilot_ctx, merged, witnesses)
    if not records:
        LOG.info("copilot_composition_value_witness_records_dropped", witness_count=len(witnesses))
        return evidence
    # The replacement has to clear the same gate the retry loop cleared for the packet it displaces.
    if not _composition_capture_settled(merged):
        LOG.info("copilot_composition_value_witness_replacement_unsettled", witness_count=len(witnesses))
        return evidence
    merged["value_witnesses"] = records
    return merged


def _composition_capture_settled(evidence: dict[str, Any]) -> bool:
    return (
        has_bounded_page_schema(evidence) or has_satisfiable_collapsed_disclosure_path(evidence)
    ) and not challenge_evidence_unsettled(evidence)


async def _capture_composition_evidence(
    copilot_ctx: Any,
    *,
    inspected_url: str,
    current_url: str,
    requested_reads: tuple[AdmittedOutputRead, ...] = (),
) -> CompositionEvidenceCapture:
    """Capture page evidence, using HTML only to enrich a valid but unsettled structured packet."""
    capture_session_id = copilot_ctx.browser_session_id if isinstance(copilot_ctx, AgentContext) else None
    capture_session_generation = (
        copilot_ctx.browser_session_continuity_generation if isinstance(copilot_ctx, AgentContext) else None
    )
    requested_targets = _seeded_capture_targets(copilot_ctx, requested_reads)
    owned_labels = _seeded_label_owners(copilot_ctx, requested_reads)
    evidence: dict[str, Any] | None = None
    html_truncated = False
    used_structured = False
    skip_raw = False
    for attempt in range(_COMPOSITION_HOLLOW_RECAPTURE_RETRIES + 1):
        structured, structured_error = await _composition_get_structured_evidence_result(
            copilot_ctx,
            inspected_url=inspected_url,
            current_url=current_url,
            requested_targets=requested_targets,
        )
        if structured_error is not None:
            if evidence is not None:
                break
            return CompositionEvidenceCapture(None, structured_error)
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
        html, html_error, html_truncated, used_stripped = await _composition_get_html(
            copilot_ctx,
            skip_raw=skip_raw,
            rendered_style_snapshot=True,
        )
        if html_error is not None:
            if evidence is not None:
                break
            return CompositionEvidenceCapture(None, html_error)
        # On a heavy page the raw get_html serialization is dropped over the MCP size cap and
        # falls back to the stripped read; once that happens, settle-and-recapture via the
        # stripped path only so a slow page is still retried without re-serializing the full DOM.
        if used_stripped:
            skip_raw = True
        evidence = parse_composition_html(
            html,
            inspected_url=inspected_url,
            current_url=current_url,
            requested_targets=requested_targets,
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
    frame: CapturedFrame | None = None
    unresolved = unresolved_requested_targets(evidence, requested_targets) if evidence is not None else ()
    if evidence is not None and (
        page_evidence_needs_visual_fallback(evidence)
        or (evidence.get("schema_empty_page") is True and not has_bounded_page_schema(evidence))
        or (used_structured and unresolved)
    ):
        captured = await _composition_capture_visual_summary(copilot_ctx, evidence, unresolved)
        merged = _merge_visual_summary_capture(evidence, captured)
        frame = captured.frame
        if used_structured and unresolved and captured.summary is not None:
            evidence = await _witness_unresolved_requested_targets(
                copilot_ctx,
                merged,
                captured=captured,
                unresolved=unresolved,
                owned_labels=owned_labels,
                requested_targets=requested_targets,
                inspected_url=inspected_url,
                current_url=current_url,
            )
        else:
            evidence = merged
    if (
        isinstance(copilot_ctx, AgentContext)
        and evidence is not None
        and (
            copilot_ctx.browser_session_id != capture_session_id
            or copilot_ctx.browser_session_continuity_generation != capture_session_generation
        )
    ):
        evidence = _composition_add_inspection_warning(evidence, "mixed_browser_session_provenance")
        evidence["browser_session_provenance"] = {
            "mixed": True,
            "start_browser_session_id": capture_session_id,
            "end_browser_session_id": copilot_ctx.browser_session_id,
            "start_generation": capture_session_generation,
            "end_generation": copilot_ctx.browser_session_continuity_generation,
        }
    return CompositionEvidenceCapture(evidence, None, frame)


async def _read_run_session_page_evidence(
    ctx: CopilotContext,
    *,
    run_session_id: str,
    current_url: str,
) -> tuple[dict[str, Any] | None, str | None, str | None, CapturedFrame | None]:
    """The discovery extractor reads ctx.browser_session_id per call, so the rebind targets the run
    session and is restored in a finally. The browser layer can substitute a replacement session for a
    closed one, so the id the capture actually observed is read back before the restore."""
    prior_session_id = ctx.browser_session_id
    ctx.browser_session_id = run_session_id
    observed_session_id: str | None = run_session_id
    observation_error: str | None = None
    evidence: dict[str, Any] | None = None
    frame: CapturedFrame | None = None
    try:
        capture = await asyncio.wait_for(
            _capture_composition_evidence(ctx, inspected_url=current_url, current_url=current_url),
            timeout=_POST_RUN_REPAIR_CAPTURE_TIMEOUT_SECONDS,
        )
        evidence, observation_error, frame = _capture_result_parts(capture)
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
        return None, None, observation_error or "Post-run page capture lost its browser session.", None
    return (evidence if isinstance(evidence, dict) else None), observed_session_id, observation_error, frame


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
    requested_reads: tuple[AdmittedOutputRead, ...] = (),
) -> dict[str, Any]:
    # Named navigation is the only route that may release sensitive-page custody. Keep navigation,
    # release, capture, and evidence admission atomic with sensitive-run registration so this call
    # cannot clear a newer run's taint after its navigation returns.
    async with browser_page_custody_lock(copilot_ctx):
        async with browser_evidence_commit_lock(copilot_ctx):
            return await _inspect_page_for_composition_under_custody(copilot_ctx, target_url, requested_reads)


def _record_value_witnesses(
    copilot_ctx: object,
    evidence: dict[str, Any],
    *,
    url: str,
    capture_session_id: str | None,
    run_page_source_session_id: str | None,
) -> None:
    """Keep the witnessed pairs this capture could record as scouted reads, and drop the rest.

    A witness left in the packet cancels the designation probe for its path, so a pair read through
    the run's browser, or one whose session moved mid-capture, must not survive here.
    """
    records = evidence.get("value_witnesses")
    if not isinstance(records, list) or not records:
        _drop_uncorroborated_screenshot_values(evidence)
        return
    provenance = evidence.get("browser_session_provenance")
    kept: list[dict[str, Any]] = []
    same_session = (
        isinstance(copilot_ctx, AgentContext)
        and run_page_source_session_id is None
        and copilot_ctx.browser_session_id == capture_session_id
        and not (isinstance(provenance, dict) and provenance.get("mixed") is True)
    )
    if same_session and isinstance(copilot_ctx, AgentContext):
        for record in records:
            if not isinstance(record, dict):
                continue
            output_path = str(record.get("output_path") or "")
            recorded = _record_scouted_read(
                copilot_ctx,
                expression=str(record.get("expression") or ""),
                data={"result": record.get("value")},
                url=url,
                declared_output_path=output_path or None,
            )
            # `_record_scouted_read` may bind the fact elsewhere; a witness only cancels the
            # designation probe for the path the read actually answered.
            bound_output_path = str(recorded.get("read_output_path") or "") if recorded is not None else ""
            LOG.info(
                "copilot_composition_value_witness_recorded",
                recorded=recorded is not None,
                declared_output_path_present=bool(output_path),
                bound_to_declared_path=bool(output_path) and bound_output_path == output_path,
            )
            if recorded is not None and output_path and bound_output_path == output_path:
                kept.append({key: value for key, value in record.items() if key != "expression"})
    if kept:
        evidence["value_witnesses"] = kept
    else:
        _drop_uncorroborated_screenshot_values(evidence)


def _drop_uncorroborated_screenshot_values(evidence: dict[str, Any]) -> None:
    """Take the screenshot's label/value pairs back out once no witness survived the page lookup.

    The pairs are what the frame appeared to say. Leaving them in the packet offers the model a
    magnitude nothing on the page corroborated, which it can copy into a step instead of reading.
    """
    evidence.pop("value_witnesses", None)
    evidence.pop("requested_values", None)


async def _inspect_page_for_composition_under_custody(
    copilot_ctx: Any,
    target_url: str,
    requested_reads: tuple[AdmittedOutputRead, ...] = (),
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
    if sensitive_origin_page_has_active_run(copilot_ctx):
        result = {"ok": False, "data": None, "error": SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR}
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result
    capture_session_id = copilot_ctx.browser_session_id if isinstance(copilot_ctx, AgentContext) else None
    capture_session_generation = (
        copilot_ctx.browser_session_continuity_generation if isinstance(copilot_ctx, AgentContext) else None
    )

    use_current_page = (target_url or "").strip().lower() in _CURRENT_PAGE_INSPECTION_TARGETS
    run_id = getattr(copilot_ctx, "last_run_blocks_workflow_run_id", None)
    sensitive_same_turn_run = register_matching_origin_run_redaction_values(copilot_ctx, run_id)
    if not use_current_page:
        _clear_pending_browser_interaction_observation(copilot_ctx)
    bypass_budget_for_post_run_current_page = _allows_post_run_current_page_inspection_budget_bypass(
        copilot_ctx,
        use_current_page=use_current_page,
    )

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
    elif sensitive_same_turn_run:
        # Do not inspect the sensitive run page merely to decide whether navigation can be skipped.
        # A named URL is the legitimate route: navigate first, then inspect the resulting page.
        inspect_target_url = entry_url
        on_target_page = False
    else:
        live_url, _ = await _fallback_page_info(copilot_ctx)
        on_target_page = _same_inspect_target(live_url, entry_url)
        inspect_target_url = live_url if on_target_page else entry_url

    # Structured inspection is not rationed. Capping it sent the agent to hand-rolled `evaluate`
    # probes once the budget ran out — on a heavy app that meant 22 small DOM reads in place of a
    # handful of structured looks, and the turn's wall clock spent without understanding the page.
    # The calls stay counted for telemetry.
    evidence = None
    visual_fallback_frame: CapturedFrame | None = None
    observation_error: str | None = None
    with copilot_span(
        "inspect_page_for_composition",
        data={"target_url_kind": kind},
    ):
        if on_target_page:
            # current_page, or a URL target the agent is already on — capture without navigating.
            current_url = inspect_target_url or entry_url
            if run_page_source_session_id:
                (
                    evidence,
                    observed_run_session_id,
                    observation_error,
                    visual_fallback_frame,
                ) = await _read_run_session_page_evidence(
                    copilot_ctx, run_session_id=run_page_source_session_id, current_url=current_url
                )
            else:
                capture = await _capture_composition_evidence(
                    copilot_ctx,
                    inspected_url=entry_url,
                    current_url=current_url,
                    requested_reads=requested_reads,
                )
                evidence, observation_error, visual_fallback_frame = _capture_result_parts(capture)
        else:
            nav_result = await _discovery_navigate(
                copilot_ctx,
                entry_url,
                wait_until="domcontentloaded",
                timeout_seconds=_COMPOSITION_NAVIGATE_TIMEOUT_SECONDS,
            )
            if not nav_result.get("ok"):
                nav_error = str(nav_result.get("error") or "unknown")
                if sensitive_same_turn_run:
                    # Navigation failure may leave the browser on the sensitive origin page.
                    # Do not inspect that page through the ordinary failure fallback.
                    result = {
                        "ok": False,
                        "data": None,
                        "error": f"inspect_page_for_composition could not navigate: {nav_error}",
                    }
                    record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
                    return result
                failure_capture = await _composition_evidence_after_navigation_failure(
                    copilot_ctx,
                    inspected_url=entry_url,
                    navigation_error=nav_error,
                    requested_reads=requested_reads,
                )
                if failure_capture is None:
                    result = {
                        "ok": False,
                        "data": None,
                        "error": f"inspect_page_for_composition could not navigate: {nav_error}",
                    }
                    record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
                    return result
                evidence, visual_fallback_frame = failure_capture
                current_url = str(evidence.get("current_url") or entry_url)
            else:
                current_url = _discovery_extract_current_url(nav_result, entry_url)
                clear_sensitive_origin_page_taint(copilot_ctx)
                capture = await _capture_composition_evidence(
                    copilot_ctx,
                    inspected_url=entry_url,
                    current_url=current_url,
                    requested_reads=requested_reads,
                )
                evidence, observation_error, visual_fallback_frame = _capture_result_parts(capture)

    if sensitive_origin_page_facts_withheld(copilot_ctx, run_id):
        result = {"ok": False, "data": None, "error": SENSITIVE_ORIGIN_PAGE_ERROR}
        record_tool_step_result_for_ctx(copilot_ctx, "inspect_page_for_composition", arguments, result)
        return result

    if (
        isinstance(copilot_ctx, AgentContext)
        and evidence is not None
        and (
            copilot_ctx.browser_session_id != capture_session_id
            or copilot_ctx.browser_session_continuity_generation != capture_session_generation
        )
    ):
        evidence = _composition_add_inspection_warning(evidence, "mixed_browser_session_provenance")
        evidence["browser_session_provenance"] = {
            "mixed": True,
            "start_browser_session_id": capture_session_id,
            "end_browser_session_id": copilot_ctx.browser_session_id,
            "start_generation": capture_session_generation,
            "end_generation": copilot_ctx.browser_session_continuity_generation,
        }

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

    if sensitive_same_turn_run:
        evidence = scrub_secrets_from_structure(copilot_ctx, evidence)
        # Exact-value scrubbing applies to structured evidence, not pixels.
        visual_fallback_frame = None

    if isinstance(run_id, str) and run_id:
        session_provenance = evidence.get("browser_session_provenance")
        mixed_session_provenance = isinstance(session_provenance, dict) and session_provenance.get("mixed") is True
        source_browser_session_id = (
            None
            if mixed_session_provenance
            else (observed_run_session_id if run_page_source_session_id else copilot_ctx.browser_session_id)
        )
        evidence, preserved_stored_evidence = store_post_run_page_evidence(
            copilot_ctx,
            evidence,
            run_id=run_id,
            current_url=current_url,
            source_browser_session_id=source_browser_session_id,
            run_browser_session_id=copilot_ctx.last_run_blocks_browser_session_id,
        )
        if preserved_stored_evidence:
            visual_fallback_frame = None
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
    _record_value_witnesses(
        copilot_ctx,
        evidence,
        url=str(evidence.get("current_url") or current_url or ""),
        capture_session_id=capture_session_id,
        run_page_source_session_id=run_page_source_session_id,
    )
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
    if visual_fallback_frame is not None:
        workflow_run_id = evidence.get("workflow_run_id")
        enqueue_screenshot(
            copilot_ctx,
            visual_fallback_frame.b64,
            provenance=ScreenshotProvenance(
                source_tool="inspect_page_for_composition",
                captured_url=visual_fallback_frame.captured_url,
                observation_step=observation_step,
                browser_session_id=visual_fallback_frame.browser_session_id,
                workflow_run_id=workflow_run_id if isinstance(workflow_run_id, str) else None,
                action_relation=ScreenshotActionRelation.SAME_PAGE_OBSERVATION,
                dispatch_url=visual_fallback_frame.dispatch_url,
                dispatch_browser_session_id=visual_fallback_frame.dispatch_browser_session_id,
                producer_browser_session_id=visual_fallback_frame.producer_browser_session_id,
                session_binding=visual_fallback_frame.session_binding,
            ),
            captured_at=visual_fallback_frame.captured_at,
        )
    return result
