from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, cast
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    scout_accessible_role_name_expression as _scout_accessible_role_name_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    has_bounded_page_schema,
)
from skyvern.forge.sdk.copilot.enforcement import _RECENT_TOOL_OUTPUT_CHAR_CAP
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    PendingBrowserInteractionObservation,
    ScoutedInteraction,
)

from ._shared import (
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _append_flow_evidence,
    _composition_get_structured_evidence,
    _same_page_ignoring_fragment,
    _workflow_verification_evidence,
)

LOG = structlog.get_logger()


def _mark_page_inspected(ctx: AgentContext) -> None:
    ctx.post_budget_page_inspection_required = False
    ctx.post_budget_page_inspection_url = None
    ctx.post_budget_page_inspection_run_id = None


def _clear_pending_browser_interaction_observation(ctx: AgentContext) -> None:
    ctx.pending_browser_interaction_observation = None


def _mark_pending_browser_interaction_observation(ctx: AgentContext, *, tool_name: str, url: str) -> None:
    if not url.strip():
        _clear_pending_browser_interaction_observation(ctx)
        return
    ctx.pending_browser_interaction_observation = PendingBrowserInteractionObservation(
        tool_name=tool_name,
        url=url.strip(),
    )


def _consume_pending_browser_interaction_observation(
    ctx: AgentContext,
    *,
    current_url: str,
    evidence: dict[str, Any],
) -> bool:
    pending = ctx.pending_browser_interaction_observation
    if pending is None:
        return False
    _clear_pending_browser_interaction_observation(ctx)
    if not has_bounded_page_schema(evidence):
        return False
    if not _same_page_ignoring_fragment(pending.url, current_url):
        LOG.warning(
            "copilot_pending_browser_interaction_observation_page_mismatch",
            tool_name=pending.tool_name,
            pending_url=pending.url,
            current_url=current_url,
        )
        return False
    return True


_MAX_SCOUTED_INTERACTIONS = 60


async def _live_working_page_url(ctx: AgentContext) -> str | None:
    if not ctx.browser_session_id:
        return None
    try:
        browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        if not browser_state:
            return None
        page = await browser_state.get_or_create_page()
        return page.url if page else None
    except Exception:
        return None


async def _capture_scout_source_url(ctx: AgentContext) -> None:
    # Pre-action: a navigating click/Enter would leave only the destination URL, not the page the selector acted on.
    ctx.pending_scout_source_url = await _live_working_page_url(ctx)


def _consume_scout_source_url(ctx: AgentContext) -> str | None:
    source_url = ctx.pending_scout_source_url
    # Cleared unconditionally so a non-recording action can't bleed its source page into a later interaction.
    ctx.pending_scout_source_url = None
    return source_url


_ROLE_NAME_SELECTOR_RE = re.compile(r'^role=([a-zA-Z]+)(?:\[name="((?:[^"\\]|\\.)*)"\])?(.*)$')


def _role_name_from_selector(selector: str) -> tuple[str, str] | None:
    """Parse the ``role=<role>[name="<name>"]`` form (ref_to_selector) — TIER 1, no browser read.

    Returns (role, accessible_name) when the selector is a plain role/name locator;
    None for bare CSS/xpath or when an engine chain (`>> nth=`) trails the role/name.
    """
    selector = selector.strip()
    match = _ROLE_NAME_SELECTOR_RE.match(selector)
    if not match:
        return None
    role, raw_name, suffix = match.group(1), match.group(2), match.group(3)
    if suffix.strip():
        return None
    name = raw_name.replace('\\"', '"') if raw_name is not None else ""
    return role, name


async def _capture_accessible_role_name(ctx: AgentContext, selector: str) -> tuple[str, str] | None:
    """TIER 2: read the element's role/accessible name for a bare CSS/xpath selector.

    A failed read degrades gracefully to None so the selector-only auto-credit
    path (SKY-10712) stays intact.
    """
    selector = selector.strip()
    if not selector:
        return None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _scout_accessible_role_name_expression(selector)},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    value = (result.get("data") or {}).get("result")
    if not isinstance(value, dict):
        return None
    role = str(value.get("role") or "").strip()
    name = str(value.get("accessible_name") or "").strip()
    if not role and not name:
        return None
    return role, name


async def _resolve_scout_role_name(
    ctx: AgentContext, selector: str, *, allow_browser_read: bool = True
) -> tuple[str, str]:
    """Resolve (role, accessible_name) for a scouted selector. TIER 1 parse first;
    TIER 2 browser read only for bare CSS/xpath. Always degrades to ("", "").

    ``allow_browser_read=False`` skips TIER 2 when the action navigated: a post-action
    read against the landing page would capture the wrong element's name, so the bare
    selector is kept verbatim (the synthesizer prefers it anyway)."""
    selector = selector.strip()
    if not selector:
        return "", ""
    parsed = _role_name_from_selector(selector)
    if parsed is not None:
        return parsed
    if not allow_browser_read:
        return "", ""
    captured = await _capture_accessible_role_name(ctx, selector)
    if captured is not None:
        return captured
    return "", ""


def _record_scouted_interaction(
    ctx: AgentContext,
    *,
    tool_name: str,
    selector: str = "",
    source_url: str | None = None,
    value: str = "",
    key: str = "",
    typed_length: int = 0,
    role: str = "",
    accessible_name: str = "",
    credential_id: str = "",
    credential_field: str = "",
    credential_name: str = "",
) -> None:
    selector = selector.strip()
    # press_key may be page-level, so it is recorded by key even with no selector; other tools require one.
    if tool_name != "press_key" and not selector:
        return
    artifact: ScoutedInteraction = {"tool_name": tool_name}
    if selector:
        artifact["selector"] = selector
    if source_url and source_url.strip():
        artifact["source_url"] = source_url.strip()
    if value:
        artifact["value"] = value
    if key:
        artifact["key"] = key
    if typed_length:
        artifact["typed_length"] = typed_length
    if role:
        artifact["role"] = role
    if accessible_name:
        artifact["accessible_name"] = accessible_name
    if credential_id:
        artifact["credential_id"] = credential_id
    if credential_field:
        artifact["credential_field"] = credential_field
    if credential_name:
        artifact["credential_name"] = credential_name
    interactions = [
        item
        for item in ctx.scouted_interactions
        if not (
            item.get("tool_name") == artifact["tool_name"]
            and item.get("selector") == artifact.get("selector")
            and item.get("source_url") == artifact.get("source_url")
            and item.get("credential_field") == artifact.get("credential_field")
        )
    ]
    interactions.append(artifact)
    ctx.scouted_interactions = interactions[-_MAX_SCOUTED_INTERACTIONS:]

    trajectory = list(ctx.scout_trajectory)
    trajectory_artifact = cast(ScoutedInteraction, artifact.copy())
    trajectory_artifact["trajectory_index"] = len(trajectory)
    trajectory.append(trajectory_artifact)
    ctx.scout_trajectory = trajectory[-_MAX_SCOUTED_INTERACTIONS:]

    LOG.info(
        "copilot_scout_interaction_captured",
        tool_name=tool_name,
        selector=selector or None,
        source_url=artifact.get("source_url"),
        role=role or None,
        total_scouted_interactions=len(ctx.scouted_interactions),
        total_scout_trajectory=len(ctx.scout_trajectory),
    )


_ACT_OBSERVE_TOOLS = frozenset({"click"})


async def _scout_act_observe_page_evidence(ctx: AgentContext, *, url: str) -> dict[str, Any] | None:
    """Run the bounded page-side extractor right after a scout interaction.

    Degrades to None on timeout, error, or a hollow parse so the interaction
    result is never blocked or failed by capture problems."""
    if getattr(ctx, "discovery_mcp_server", None) is None:
        return None
    timeout_seconds = settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS
    started = time.monotonic()
    parsed: dict[str, Any] | None = None
    try:
        parsed = await _composition_get_structured_evidence(
            ctx, inspected_url=url, current_url=url, timeout_seconds=timeout_seconds
        )
        if parsed is not None and has_bounded_page_schema(parsed):
            outcome = "attached"
        elif parsed is not None:
            outcome = "hollow"
            parsed = None
        else:
            outcome = "timeout" if time.monotonic() - started >= timeout_seconds else "error"
    except Exception:
        parsed = None
        outcome = "error"
    LOG.info(
        "copilot_scout_act_observe",
        outcome=outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        url=url,
    )
    return parsed


async def _register_scout_interaction_observation(
    ctx: AgentContext, *, tool_name: str, selector: str, source_url: str | None, url: str
) -> tuple[int | None, dict[str, Any] | None]:
    # A successful scout interaction reaches the post-action page; record it as an
    # interaction-reached observation so a click-reached block can be authored
    # against it without a separate inspect_page_for_composition.
    selector = selector.strip()
    if not selector or not url:
        return None, None
    evidence: dict[str, Any] = {
        "inspected_url": url,
        "current_url": url,
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
        "interaction_tool": tool_name,
        "interaction_selector": selector,
    }
    if source_url and source_url.strip():
        evidence["interaction_source_url"] = source_url.strip()
    page_evidence: dict[str, Any] | None = None
    if settings.COPILOT_SCOUT_ACT_OBSERVE_ENABLED and tool_name in _ACT_OBSERVE_TOOLS:
        parsed = await _scout_act_observe_page_evidence(ctx, url=url)
        if parsed is not None:
            # Identity keys overwrite the parsed packet so the entry stays a
            # scout_interaction observation, with the schema merged before append.
            evidence = {**parsed, **evidence}
            page_evidence = evidence
            # The schema is already attached; leaving the marker set would let a
            # later evaluate/inspect mint a second interaction credit for one click.
            _clear_pending_browser_interaction_observation(ctx)
    step = _append_flow_evidence(ctx, evidence, reached_via="interaction")
    return step, page_evidence


_PAGE_SUMMARY_TEXT_CAP = 80
_PAGE_SUMMARY_MAX_FIELDS = 8
_PAGE_SUMMARY_MAX_SUBMITS = 4
_PAGE_SUMMARY_MAX_NAV_TEXTS = 8
_PAGE_SUMMARY_MAX_DISMISS_TEXTS = 4


def _summary_text(value: Any) -> str:
    return value.strip()[:_PAGE_SUMMARY_TEXT_CAP] if isinstance(value, str) else ""


def _summary_field_name(field: dict[str, Any]) -> str:
    for key in ("label", "name", "placeholder", "id"):
        text = _summary_text(field.get(key))
        if text:
            return text
    return ""


def _build_scout_page_summary(evidence: dict[str, Any]) -> dict[str, Any]:
    forms_summary: list[dict[str, Any]] = []
    for form in evidence.get("forms") or []:
        if not isinstance(form, dict):
            continue
        fields = [field for field in form.get("fields") or [] if isinstance(field, dict)]
        submits = [control for control in form.get("submit_controls") or [] if isinstance(control, dict)]
        forms_summary.append(
            {
                "field_count": len(fields),
                "fields": [
                    name for name in (_summary_field_name(field) for field in fields[:_PAGE_SUMMARY_MAX_FIELDS]) if name
                ],
                "submit_controls": [
                    text
                    for text in (
                        _summary_text(control.get("text") or control.get("value"))
                        for control in submits[:_PAGE_SUMMARY_MAX_SUBMITS]
                    )
                    if text
                ],
            }
        )
    nav_targets = [target for target in evidence.get("navigation_targets") or [] if isinstance(target, dict)]
    dismiss_texts: list[str] = []
    for overlay in evidence.get("modal_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        for control in overlay.get("dismiss_controls") or []:
            if len(dismiss_texts) >= _PAGE_SUMMARY_MAX_DISMISS_TEXTS:
                break
            if not isinstance(control, dict):
                continue
            text = _summary_text(control.get("text") or control.get("aria_label") or control.get("title"))
            if text:
                dismiss_texts.append(text)
    challenge_state = evidence.get("challenge_state")
    challenge_detected = bool(evidence.get("challenge_controls")) or (
        isinstance(challenge_state, dict) and challenge_state.get("detected") is True
    )
    return {
        "page_title": _summary_text(evidence.get("page_title")),
        "forms": forms_summary,
        "navigation_target_count": len(nav_targets),
        "navigation_targets": [
            text
            for text in (_summary_text(target.get("text")) for target in nav_targets[:_PAGE_SUMMARY_MAX_NAV_TEXTS])
            if text
        ],
        "result_container_count": len(evidence.get("result_containers") or []),
        "challenge_detected": challenge_detected,
        "modal_dismiss_controls": dismiss_texts,
    }


def _shed_scout_page_summary_section(summary: dict[str, Any]) -> bool:
    """Drop one summary section, in fixed priority order; False when nothing is left to shed."""
    if summary.get("navigation_targets"):
        summary["navigation_targets"] = []
        return True
    forms = [form for form in summary.get("forms") or [] if isinstance(form, dict)]
    for form in forms[1:]:
        if form.get("fields"):
            form["fields"] = []
            return True
    if summary.get("modal_dismiss_controls"):
        summary["modal_dismiss_controls"] = []
        return True
    for form in forms[1:]:
        if form.get("submit_controls"):
            form["submit_controls"] = []
            return True
    if forms and forms[0].get("fields"):
        fields = forms[0]["fields"]
        forms[0]["fields"] = fields[: len(fields) // 2] if len(fields) > 2 else []
        return True
    if forms and forms[0].get("submit_controls"):
        forms[0]["submit_controls"] = []
        return True
    if len(forms) > 1:
        summary["forms"] = forms[:1]
        return True
    return False


def _attach_scout_page_summary(result: dict[str, Any], page_evidence: dict[str, Any]) -> None:
    """Attach a compact page summary at result["data"]["page"], keeping the whole
    serialized result under the recent-output pruner cap by shedding sections —
    never by slicing the serialized JSON."""
    data = result.get("data")
    if not isinstance(data, dict):
        return
    try:
        summary = _build_scout_page_summary(page_evidence)
        data["page"] = summary
        while len(json.dumps(result)) > _RECENT_TOOL_OUTPUT_CHAR_CAP:
            if not _shed_scout_page_summary_section(summary):
                data.pop("page", None)
                return
    except Exception:
        data.pop("page", None)
        LOG.warning("copilot_scout_act_observe_summary_failed", exc_info=True)


def _mark_post_run_page_observed(ctx: AgentContext, *, source_tool: str, url: str) -> None:
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return
    ctx.post_run_page_observation_tool = source_tool
    ctx.post_run_page_observation_url = url
    ctx.post_run_page_observation_workflow_run_id = run_id
    ctx.post_run_page_observation_after_failed_test = getattr(ctx, "last_test_ok", None) is False
    evidence = _workflow_verification_evidence(ctx)
    evidence.live_page_state_verified = True
    evidence.verified_from_current_browser_state = True
    evidence.workflow_run_id = run_id
    if url:
        evidence.current_url = url
        evidence.current_url_observed_after_workflow_run = True
        evidence.current_url_may_encode_runtime_state = bool(urlparse(url).query)
