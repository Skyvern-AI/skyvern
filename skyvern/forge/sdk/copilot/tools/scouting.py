from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from typing import Any, Literal, cast
from urllib.parse import urlparse

import structlog
from playwright.async_api import Download, Page, Response

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.build_test_outcome import (
    _AMBIGUOUS_NON_DEMONSTRATION_RUN_REASON_CODES,
    RecordedBuildTestOutcome,
    bind_post_run_page_path_failure,
    record_build_test_outcome,
    recorded_outcome_from_scout_act_observe_hollow,
)
from skyvern.forge.sdk.copilot.challenge_evidence import (
    CHALLENGE_EVIDENCE_SOURCE_KEY,
    ChallengeEvidenceSource,
    challenge_evidence_unsettled,
    challenge_signal_regressed,
    composition_challenge_carrier,
    interactive_challenge_controls,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    enclosing_form_submit_controls_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    role_name_match_count_expression as _role_name_match_count_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    scout_accessible_role_name_expression as _scout_accessible_role_name_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    selector_candidates_expression as _selector_candidates_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    selector_match_count_expression as _selector_match_count_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    has_actionable_steer_content,
    has_bounded_page_schema,
    has_witnessed_value_content,
)
from skyvern.forge.sdk.copilot.enforcement import (
    mint_scout_observation_contract_for_ctx,
    record_reached_terminal_action_observation,
    record_scouted_output_coverage,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    PendingBrowserInteractionObservation,
    ScoutedInteraction,
    ScoutedSelectorCandidate,
    resolve_browser_state_for_context,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.workflow.models.block import (
    CodeBlockCaptchaError,
    _bounded_code_block_recaptcha_token_populated,
    _code_block_recaptcha_response_field_present,
    _code_block_solve_captcha_builtin,
)

from ._shared import (
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _append_flow_evidence,
    _composition_get_structured_evidence,
    _same_page_ignoring_fragment,
    _workflow_verification_evidence,
)

LOG = structlog.get_logger()

# Emission budget for scout/evaluate tool results: bounds how much live page content
# a result may carry into the authoring context, so it must not follow the transcript
# recent-window cap.
_SCOUT_RESULT_CHAR_CAP = 2000


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
        browser_state = await resolve_browser_state_for_context(ctx, session_id=ctx.browser_session_id)
        if not browser_state:
            return None
        page = await browser_state.get_or_create_page()
        return page.url if page else None
    except Exception:
        return None


async def _capture_scout_source_url(ctx: AgentContext) -> None:
    # Pre-action: a navigating click/Enter would leave only the destination URL, not the page the selector acted on.
    source_url = await _live_working_page_url(ctx)
    ctx.pending_scout_source_url = source_url


def _consume_scout_source_url(ctx: AgentContext) -> str | None:
    source_url = ctx.pending_scout_source_url
    # Cleared unconditionally so a non-recording action can't bleed its source page into a later interaction.
    ctx.pending_scout_source_url = None
    return source_url


_ROLE_NAME_SELECTOR_RE = re.compile(r'^role=([a-zA-Z]+)(?:\[name="((?:[^"\\]|\\.)*)"\])?(.*)$')


def _selector_text(selector: Any) -> str:
    return selector.strip() if isinstance(selector, str) else ""


def _role_name_from_selector(selector: str | None) -> tuple[str, str] | None:
    """Parse the ``role=<role>[name="<name>"]`` form (ref_to_selector) — TIER 1, no browser read.

    Returns (role, accessible_name) when the selector is a plain role/name locator;
    None for bare CSS/xpath or when an engine chain (`>> nth=`) trails the role/name.
    """
    selector = _selector_text(selector)
    match = _ROLE_NAME_SELECTOR_RE.match(selector)
    if not match:
        return None
    role, raw_name, suffix = match.group(1), match.group(2), match.group(3)
    if suffix.strip():
        return None
    name = raw_name.replace('\\"', '"') if raw_name is not None else ""
    return role, name


async def _capture_accessible_role_name(
    ctx: AgentContext, selector: str | None, *, timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
) -> tuple[str, str] | None:
    """TIER 2: read the element's role/accessible name for a bare CSS/xpath selector.

    A failed read degrades gracefully to None so the selector-only auto-credit
    path (SKY-10712) stays intact.
    """
    selector = _selector_text(selector)
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
            timeout=timeout_seconds,
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


# A click pre-hook runs inline before the click dispatch, so the read is bounded well under the
# discovery timeout to avoid delaying the action when the element resists a fast a11y read.
_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS = 2.0


async def _selector_live_match_count(
    ctx: AgentContext, selector: str | None, *, timeout_seconds: float | None = None
) -> int | None:
    """Live element count for a selector, or None when the page read is unavailable or the selector
    is invalid; lets a failed click tell an invented zero-match selector from a not-yet-actionable one."""
    selector = _selector_text(selector)
    if not selector:
        return None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return None
    timeout = _PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if timeout <= 0:
        return None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _selector_match_count_expression(selector)},
            ),
            timeout=timeout,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    value = (result.get("data") or {}).get("result")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


async def _capture_scout_selector_candidates(ctx: AgentContext, selector: str | None) -> None:
    """Capture source-page selector identities without selecting a replacement."""
    ctx.pending_scout_selector_candidates = None
    selector = _selector_text(selector)
    server = getattr(ctx, "discovery_mcp_server", None)
    if not selector or server is None:
        return
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _selector_candidates_expression(selector)},
            ),
            timeout=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS,
        )
    except Exception:
        return
    if not isinstance(result, dict) or not result.get("ok"):
        return
    raw_candidates = (result.get("data") or {}).get("result")
    if not isinstance(raw_candidates, list):
        return
    candidates: list[ScoutedSelectorCandidate] = []
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        candidate_selector = _selector_text(raw.get("selector"))
        source = _selector_text(raw.get("source"))
        if not candidate_selector or not source:
            continue
        candidate: ScoutedSelectorCandidate = {"selector": candidate_selector, "source": source}
        if candidate not in candidates:
            candidates.append(candidate)
    if candidates:
        ctx.pending_scout_selector_candidates = candidates


async def _role_name_match_count(
    ctx: AgentContext, role: str, name: str, *, timeout_seconds: float = _PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS
) -> int | None:
    """Live count of elements whose computed ARIA role and accessible name exactly match, or None when
    the page read is unavailable; lets the ambiguity guard tell a uniquely-resolvable re-anchor apart from
    a name-degenerate one before trusting get_by_role(role, name, exact=True)."""
    if not role or not name:
        return None
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None or timeout_seconds <= 0:
        return None
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _role_name_match_count_expression(role, name)},
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    value = (result.get("data") or {}).get("result")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


async def _capture_scout_role_name(ctx: AgentContext, selector: str | None) -> None:
    """Stash (selector, role, accessible_name) before an in-flight click that may navigate.

    A navigating click leaves only the landing page, so the post-action read returns the wrong
    element; this captures the source-page anchor so a bare-selector navigating click still carries a
    role/name into the trajectory."""
    ctx.pending_scout_role_name = None
    ctx.pending_scout_role_name_match_count = None
    selector = _selector_text(selector)
    if not selector:
        return
    parsed = _role_name_from_selector(selector)
    source = "selector"
    if parsed is not None:
        role, name = parsed
    else:
        source = "page_read"
        captured = await _capture_accessible_role_name(
            ctx, selector, timeout_seconds=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS
        )
        if captured is None:
            # A read that never answered and one that answered namelessly leave the same empty
            # trajectory, and only the first is a timeout worth widening.
            LOG.info("copilot_scout_role_name_unavailable", reason="page_read_failed", source=source)
            return
        role, name = captured
    if not role or not name:
        LOG.info(
            "copilot_scout_role_name_unavailable",
            reason="empty_role" if not role else "empty_name",
            source=source,
            role=role,
        )
        return
    ctx.pending_scout_role_name = (selector, role, name)
    count = await _role_name_match_count(ctx, role, name)
    if count is not None:
        ctx.pending_scout_role_name_match_count = (selector, role, name, count)


def _prenav_role_name_for_selector(pending: tuple[str, str, str] | None, selector: str) -> tuple[str, str]:
    """Return the pre-navigation (role, accessible_name) only when the recorded selector matches the
    stashed one, so a navigating click's anchor is never applied to a different element."""
    if pending is None:
        return "", ""
    stashed_selector, role, name = pending
    if stashed_selector != _selector_text(selector):
        return "", ""
    return role, name


async def _capture_scout_ambiguity(ctx: AgentContext, selector: str | None) -> None:
    """Stash source-page selector ambiguity and, when unique, a role/name re-anchor.

    The ambiguity fact never replaces the observed role/name packet. The re-anchor is an additional
    fact about uniqueness, not authority to discard a non-unique accessible identity.
    """
    ctx.pending_scout_ambiguous = None
    ctx.pending_scout_reanchor = None
    ctx.pending_scout_selector_match_count = None
    selector = _selector_text(selector)
    if not selector:
        return
    count = await _selector_live_match_count(ctx, selector)
    if count is None:
        return
    ctx.pending_scout_selector_match_count = (selector, count)
    if count <= 1:
        return
    ctx.pending_scout_ambiguous = (selector, True)
    pending_role_name = getattr(ctx, "pending_scout_role_name", None)
    if isinstance(pending_role_name, tuple) and len(pending_role_name) == 3 and pending_role_name[0] == selector:
        role, name = pending_role_name[1:]
    else:
        captured = await _capture_accessible_role_name(
            ctx, selector, timeout_seconds=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS
        )
        if captured is None:
            return
        role, name = captured
    if not role or not name:
        return
    pending_role_count = getattr(ctx, "pending_scout_role_name_match_count", None)
    if (
        isinstance(pending_role_count, tuple)
        and len(pending_role_count) == 4
        and pending_role_count[:3] == (selector, role, name)
    ):
        role_count = pending_role_count[3]
    else:
        role_count = await _role_name_match_count(ctx, role, name)
    if role_count == 1:
        ctx.pending_scout_reanchor = (selector, role, name)


def _prenav_ambiguity_for_selector(pending: tuple[str, bool] | None, selector: str) -> bool:
    """Return the stashed ambiguity verdict only when the recorded selector matches the probed one, so a
    navigating click's verdict is never applied to a different element."""
    if pending is None:
        return False
    stashed_selector, ambiguous = pending
    if stashed_selector != _selector_text(selector):
        return False
    return ambiguous


async def _resolve_scout_role_name(
    ctx: AgentContext, selector: str | None, *, allow_browser_read: bool = True
) -> tuple[str, str]:
    """Resolve (role, accessible_name) for a scouted selector. TIER 1 parse first;
    TIER 2 browser read only for bare CSS/xpath. Always degrades to ("", "").

    ``allow_browser_read=False`` skips TIER 2 when the action navigated: a post-action
    read against the landing page would capture the wrong element's name, so the bare
    selector is kept verbatim (the synthesizer prefers it anyway)."""
    selector = _selector_text(selector)
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


# Attributes the one-shot probe always reads off a resolved element, recorded only when that probe
# succeeded. An attribute absent from a fingerprint carrying this marker proves the element has none;
# without the marker (probe failed, element gone, older record) absence stays undecidable and fails closed.
# `label` is excluded: it is only read via `label[for=id]`, so its absence does not prove there is no label.
_FINGERPRINT_PROBED_ATTRS = "id,name,placeholder,tag,test_id,type"


def _element_fingerprint_expression(css_selector: str) -> str:
    """Capture element identity fingerprint (tag, id, name, type, placeholder, data-testid, label) for
    credential-fill resolution. Returns attributes only, never values."""
    sel = json.dumps(css_selector)
    return (
        "(() => {"
        f"  const el = document.querySelector({sel});"
        "  if (!el) return null;"
        "  const attr = (name) => el.getAttribute(name) || '';"
        "  const result = {"
        "    tag: (el.tagName || '').toLowerCase(),"
        "    id: attr('id'),"
        "    name: attr('name'),"
        "    type: attr('type'),"
        "    placeholder: attr('placeholder'),"
        "    test_id: attr('data-testid'),"
        "  };"
        "  const label = document.querySelector(`label[for=\"${attr('id')}\"]`);"
        "  if (label) result.label = (label.textContent || '').trim().slice(0, 200);"
        "  return result;"
        "})()"
    )


async def _capture_element_fingerprint(
    ctx: AgentContext, selector: str | None, *, timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
) -> dict[str, str]:
    """Capture element identity fingerprint (id, name, type, placeholder, label, test-id, tag)
    for credential-fill resolution. Returns empty dict on failure, never None."""
    selector = _selector_text(selector)
    if not selector:
        return {}
    server = ctx.discovery_mcp_server
    if server is None:
        return {}
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _element_fingerprint_expression(selector)},
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return {}
    if not isinstance(result, dict) or not result.get("ok"):
        return {}
    fingerprint = (result.get("data") or {}).get("result")
    if not isinstance(fingerprint, dict):
        return {}
    captured = {k: str(v).strip() for k, v in fingerprint.items() if v}
    captured["probed"] = _FINGERPRINT_PROBED_ATTRS
    return captured


async def _capture_post_interaction_screenshot(
    ctx: AgentContext, *, timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
) -> None:
    """Attach a look at the page after a state-changing action. Reading the DOM answers "what is
    on the page" but not "did that work" -- a filled password reads as empty, and a dialog covering
    the content reads as an ordinary node. Only the most recent screenshot is retained.
    """
    # getattr mirrors screenshot_utils: this runs against contexts that predate the vision field.
    if not getattr(ctx, "supports_vision", False):
        return
    server = getattr(ctx, "discovery_mcp_server", None)
    if server is None:
        return
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_screenshot", {}),
            timeout=timeout_seconds,
        )
    except Exception:
        return
    if isinstance(result, dict) and result.get("ok"):
        enqueue_screenshot_from_result(ctx, result)


async def _capture_enclosing_form_submits(
    ctx: AgentContext, selector: str | None, *, timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
) -> list[dict[str, str]]:
    """Submit controls of the form holding a just-filled field, so submitting what was filled is not
    a guess among the page's other prominent buttons. Returns an empty list on failure."""
    selector = _selector_text(selector)
    if not selector:
        return []
    server = ctx.discovery_mcp_server
    if server is None:
        return []
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": enclosing_form_submit_controls_expression(selector)},
            ),
            timeout=timeout_seconds,
        )
    except Exception:
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    controls = (result.get("data") or {}).get("result")
    if not isinstance(controls, list):
        return []
    return [
        {"label": str(entry.get("label") or "")[:80], "selector": str(entry.get("selector") or "")[:160]}
        for entry in controls
        if isinstance(entry, dict) and (entry.get("label") or entry.get("selector"))
    ]


def _capped_with_eviction_accounting(
    items: list[ScoutedInteraction],
    *,
    collection: Literal["scout_trajectory", "scouted_interactions"],
) -> list[ScoutedInteraction]:
    if len(items) <= _MAX_SCOUTED_INTERACTIONS:
        return items
    try:
        for item in items[: len(items) - _MAX_SCOUTED_INTERACTIONS]:
            event: dict[str, Any] = {
                "collection": collection,
                "tool_name": item.get("tool_name"),
                "selector": item.get("selector"),
                "source_url": item.get("source_url"),
            }
            if collection == "scout_trajectory":
                event["trajectory_index"] = item.get("trajectory_index")
            LOG.info("copilot_scout_interaction_evicted", **event)
    except Exception:
        pass
    return items[-_MAX_SCOUTED_INTERACTIONS:]


def _next_trajectory_index(trajectory: list[ScoutedInteraction]) -> int:
    # len() regresses once eviction trims the list, so the next index continues from the highest recorded one.
    highest = -1
    for item in trajectory:
        index = item.get("trajectory_index")
        if isinstance(index, int) and index > highest:
            highest = index
    return highest + 1 if highest >= 0 else len(trajectory)


def _record_scout_trajectory_fact(ctx: AgentContext, artifact: ScoutedInteraction) -> ScoutedInteraction:
    """Append one observed fact with a monotone index and bounded retention."""
    trajectory = list(ctx.scout_trajectory)
    recorded = cast(ScoutedInteraction, artifact.copy())
    recorded["trajectory_index"] = _next_trajectory_index(trajectory)
    trajectory.append(recorded)
    ctx.scout_trajectory = _capped_with_eviction_accounting(trajectory, collection="scout_trajectory")
    return recorded


def _observed_control_readiness(ctx: AgentContext, selector: str, source_url: str) -> tuple[bool, bool]:
    """Return (hidden, disabled) when bounded same-page evidence observed this exact selector unready."""
    packets: list[dict[str, Any]] = []
    for entry in getattr(ctx, "flow_evidence", None) or []:
        if isinstance(entry, dict) and isinstance(entry.get("evidence"), dict):
            packets.append(entry["evidence"])
    current = getattr(ctx, "composition_page_evidence", None)
    if isinstance(current, dict):
        packets.append(current)

    observed_hidden = False
    observed_disabled = False
    for packet in packets:
        evidence_url = str(packet.get("current_url") or packet.get("inspected_url") or "").strip()
        if not evidence_url or not _same_page_ignoring_fragment(source_url, evidence_url):
            continue
        for form in packet.get("forms") or []:
            if not isinstance(form, dict):
                continue
            controls = [*(form.get("fields") or []), *(form.get("submit_controls") or [])]
            for control in controls:
                if not isinstance(control, dict) or _selector_text(control.get("selector")) != selector:
                    continue
                observed_hidden = observed_hidden or control.get("visible") is False
                observed_disabled = observed_disabled or control.get("disabled") is True
    return observed_hidden, observed_disabled


def _record_scouted_interaction(
    ctx: AgentContext,
    *,
    tool_name: str,
    selector: str | None = None,
    selector_candidates: list[ScoutedSelectorCandidate] | None = None,
    selector_match_count: int | None = None,
    source_url: str | None = None,
    result_url: str | None = None,
    observed_effects: dict[str, bool] | None = None,
    observed_wait_ms: int | None = None,
    value: str = "",
    input_id: str = "",
    input_value: str = "",
    observation_step: int | None = None,
    key: str = "",
    typed_length: int = 0,
    role: str = "",
    accessible_name: str = "",
    role_name_match_count: int | None = None,
    control_readonly: bool | None = None,
    control_disabled: bool | None = None,
    control_value_satisfied: bool | None = None,
    credential_id: str = "",
    credential_field: str = "",
    credential_name: str = "",
    element_fingerprint_id: str | None = None,
    element_fingerprint_name: str | None = None,
    element_fingerprint_type: str | None = None,
    element_fingerprint_placeholder: str | None = None,
    element_fingerprint_label: str | None = None,
    element_fingerprint_test_id: str | None = None,
    element_fingerprint_tag: str | None = None,
    element_fingerprint_probed: str | None = None,
    ambiguous: bool = False,
) -> None:
    selector = _selector_text(selector)
    # Page-level key presses, waits, and navigations are factual steps without an element selector.
    if tool_name not in {"press_key", "navigate_browser", "wait_for_either_state"} and not selector:
        LOG.info(
            "copilot_scout_capture_loss",
            tool_name=tool_name,
            reason="unresolvable_selector",
            url=(source_url or "").strip() or None,
        )
        return
    artifact: ScoutedInteraction = {"tool_name": tool_name}
    if selector:
        artifact["selector"] = selector
    if selector_candidates:
        artifact["selector_candidates"] = selector_candidates.copy()
    if selector_match_count is not None:
        artifact["selector_match_count"] = selector_match_count
    if source_url and source_url.strip():
        artifact["source_url"] = source_url.strip()
    if result_url and result_url.strip():
        artifact["result_url"] = result_url.strip()
    direct_effects = dict(observed_effects or {})
    if source_url and source_url.strip() and result_url and result_url.strip():
        direct_effects["url_changed"] = source_url.strip() != result_url.strip()
    if direct_effects:
        artifact["observed_effects"] = direct_effects
    if observed_wait_ms is not None:
        artifact["observed_wait_ms"] = observed_wait_ms
    if selector and source_url and source_url.strip():
        observed_hidden, observed_disabled = _observed_control_readiness(ctx, selector, source_url.strip())
        if observed_hidden:
            artifact["observed_hidden"] = True
        if observed_disabled:
            artifact["observed_disabled"] = True
    if value:
        artifact["value"] = value
    if input_id:
        artifact["input_id"] = input_id
    if input_value:
        artifact["input_value"] = input_value
    if observation_step is not None:
        artifact["observation_step"] = observation_step
    if key:
        artifact["key"] = key
    if typed_length:
        artifact["typed_length"] = typed_length
    if role:
        artifact["role"] = role
    if accessible_name:
        artifact["accessible_name"] = accessible_name
    if role_name_match_count is not None:
        artifact["role_name_match_count"] = role_name_match_count
    if tool_name == "type_text":
        if control_readonly is not None:
            artifact["control_readonly"] = control_readonly
        if control_disabled is not None:
            artifact["control_disabled"] = control_disabled
        if control_value_satisfied is not None:
            artifact["control_value_satisfied"] = control_value_satisfied
    if credential_id:
        artifact["credential_id"] = credential_id
    if credential_field:
        artifact["credential_field"] = credential_field
    if credential_name:
        artifact["credential_name"] = credential_name
    if element_fingerprint_id:
        artifact["element_fingerprint_id"] = element_fingerprint_id
    if element_fingerprint_name:
        artifact["element_fingerprint_name"] = element_fingerprint_name
    if element_fingerprint_type:
        artifact["element_fingerprint_type"] = element_fingerprint_type
    if element_fingerprint_placeholder:
        artifact["element_fingerprint_placeholder"] = element_fingerprint_placeholder
    if element_fingerprint_label:
        artifact["element_fingerprint_label"] = element_fingerprint_label
    if element_fingerprint_test_id:
        artifact["element_fingerprint_test_id"] = element_fingerprint_test_id
    if element_fingerprint_tag:
        artifact["element_fingerprint_tag"] = element_fingerprint_tag
    if element_fingerprint_probed:
        artifact["element_fingerprint_probed"] = element_fingerprint_probed
    if ambiguous:
        artifact["ambiguous"] = True
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
    ctx.scouted_interactions = _capped_with_eviction_accounting(interactions, collection="scouted_interactions")

    _record_scout_trajectory_fact(ctx, artifact)

    LOG.info(
        "copilot_scout_interaction_captured",
        tool_name=tool_name,
        selector=selector or None,
        source_url=artifact.get("source_url"),
        role=role or None,
        credential_field=credential_field or None,
        credential_id=credential_id or None,
        total_scouted_interactions=len(ctx.scouted_interactions),
        total_scout_trajectory=len(ctx.scout_trajectory),
    )
    record_reached_terminal_action_observation(ctx)


def _attach_scout_observation_step(
    ctx: AgentContext,
    *,
    tool_name: str,
    selector: str,
    observation_step: int | None,
) -> None:
    """Attach the exact post-action page observation to the interaction it witnessed."""
    if observation_step is None:
        return
    for collection_name in ("scout_trajectory", "scouted_interactions"):
        collection = getattr(ctx, collection_name)
        for interaction in reversed(collection):
            if interaction.get("tool_name") == tool_name and interaction.get("selector", "") == selector:
                interaction["observation_step"] = observation_step
                break


_MAX_CHALLENGE_SOLVE_ATTEMPTS = 3
# Measured: a healthy turn re-enters the ladder 4-5 times for one widget, because a token
# expires long before a turn ends. Sized well clear of that so it bounds a page rotating its
# identity to outspend us, without cutting a legitimate turn short.
_MAX_CHALLENGE_SOLVE_ATTEMPTS_PER_TURN = 12


def _challenge_identity(evidence: dict[str, Any]) -> str:
    """Identify the challenge, not the page, so a later distinct one still gets attempts.

    Deliberately ignores ``src``: the common widgets carry a per-render cache-buster there, so
    keying on it would mint a fresh identity on every re-render and never reach the cap.
    """
    marks = []
    for control in interactive_challenge_controls(evidence.get("challenge_controls")):
        mark = control.get("data_sitekey") or control.get("selector") or control.get("id")
        if mark:
            marks.append(str(mark))
    return "|".join(sorted(marks)) or str(evidence.get("current_url") or "")


def _record_challenge_encounter(
    ctx: AgentContext,
    carrier: ChallengeEvidenceSource,
    *,
    outcome: str,
) -> None:
    """Record on the interaction that met the challenge, whether or not it was passed.

    Synthesis derives boundaries from ``composition_challenge_carrier`` over the recorded
    interaction itself, so the carrier has to ride on a tool_name it already emits for; a
    standalone solve_captcha interaction has no emitter and would be dropped.

    Meeting a challenge is a present-tense fact about the page this turn is on, so it may
    only land on an interaction this turn performed. Carried interactions describe an
    earlier turn on their own ``source_url``; stamping one would have synthesis solve a
    captcha at a boundary where none was seen.
    """
    trajectory = list(ctx.scout_trajectory)
    if not trajectory or trajectory[-1].get("carried") is True:
        return
    entry = trajectory[-1]
    challenge_state = dict(entry.get("challenge_state") or {})
    challenge_state[CHALLENGE_EVIDENCE_SOURCE_KEY] = carrier.value
    challenge_state["outcome"] = outcome
    stamped = dict(entry)
    stamped["challenge_state"] = challenge_state
    trajectory[-1] = cast(ScoutedInteraction, stamped)
    ctx.scout_trajectory = trajectory


async def solve_challenge_when_evidence_settles(
    ctx: AgentContext,
    evidence: dict[str, Any] | None,
    *,
    url: str | None,
    observed_after_interaction: bool,
) -> bool:
    """Try the shared solve routes on a challenge in just-settled evidence; True when one landed.

    Call from a capture exit, never a tool hook: a settled packet describes the page as it now
    is, and ``observed_after_interaction`` follows from the seam — the act-observe capture runs
    after a recorded interaction, an inspection capture has none to attribute.
    """
    if not evidence:
        return False
    carrier = composition_challenge_carrier(evidence)
    if carrier is None:
        return False

    # Record on detection rather than on success: an unsolved challenge is a fact the draft
    # carries forward, and exploration continues either way. Nothing below terminates the turn.
    if observed_after_interaction:
        _record_challenge_encounter(ctx, carrier, outcome="detected")

    # Cost gate, deliberately after the record above: a challenge that cannot be solved must stop
    # being paid for, but must never stop being reported to synthesis.
    # Counted before the attempt, so a transient failure spends one too — deliberate, because the
    # alternative lets a route that keeps erroring retry without bound.
    identity = _challenge_identity(evidence)
    attempts = ctx.challenge_solve_attempts.get(identity, 0)
    if attempts >= _MAX_CHALLENGE_SOLVE_ATTEMPTS:
        return False
    # Every input to the identity is page-controlled, so a widget that rotates its sitekey or id
    # mints a fresh one on each observation and never reaches the per-identity cap. The solver
    # costs real money on a platform-wide account, so the turn needs a bound the page cannot move.
    if sum(ctx.challenge_solve_attempts.values()) >= _MAX_CHALLENGE_SOLVE_ATTEMPTS_PER_TURN:
        LOG.info(
            "copilot_scout_captcha_turn_budget_spent",
            url=url,
            organization_id=ctx.organization_id,
        )
        return False
    ctx.challenge_solve_attempts[identity] = attempts + 1

    # Read once so the handlers below report a failure without touching the context again.
    organization_id = ctx.organization_id
    try:
        # Resolving the browser is inside the guard because it can raise, and a challenge the
        # scout merely failed to solve must never be what ends exploration.
        browser_state = await resolve_browser_state_for_context(ctx)
        if browser_state is None:
            return False
        page = await browser_state.get_working_page()
        if page is None:
            return False
        # Crediting a token that was already there would let an unrelated widget's response pass
        # this challenge off as solved, so the transition is what counts. An unreadable baseline
        # withholds that credit rather than failing the solve that has not happened yet.
        try:
            token_before = await _bounded_code_block_recaptcha_token_populated(page)
        except Exception:
            token_before = None
        # In-DOM checkbox, then the Turnstile extension, then the reCAPTCHA token route.
        arm_passed = await _code_block_solve_captcha_builtin(
            page,
            organization_id=organization_id,
            browser_session_id=ctx.browser_session_id,
        )
    except CodeBlockCaptchaError:
        LOG.info(
            "copilot_scout_captcha_solve_failed",
            url=url,
            organization_id=organization_id,
        )
        if observed_after_interaction:
            _record_challenge_encounter(ctx, carrier, outcome="unsolved")
        return False
    except Exception:
        LOG.warning(
            "copilot_scout_captcha_solve_exception",
            url=url,
            organization_id=organization_id,
            exc_info=True,
        )
        return False

    # A reCAPTCHA widget stays rendered after it is satisfied, so its continued presence says
    # nothing; the response token going from absent to present is what the platform treats as the
    # authoritative solve signal. An unreadable read withholds the credit rather than inventing it.
    try:
        token_after = await _bounded_code_block_recaptcha_token_populated(page)
        judged_by_token = await _code_block_recaptcha_response_field_present(page)
    except Exception:
        token_after = None
        judged_by_token = False
    # Turnstile and plain checkbox challenges carry no response field, so an arm passing is all
    # there is to go on; where a field exists, the transition is what separates this solve from a
    # response some other widget left behind.
    solved = arm_passed and (token_before is False and token_after is True if judged_by_token else True)

    if observed_after_interaction:
        _record_challenge_encounter(ctx, carrier, outcome="solved" if solved else "attempted")
    LOG.info(
        "copilot_scout_captcha_solve_attempted",
        url=url,
        organization_id=organization_id,
        solved=solved,
    )
    return solved


def _page_evidence_has_selector(value: Any, selector: str) -> bool:
    if isinstance(value, dict):
        if value.get("selector") == selector:
            return True
        return any(_page_evidence_has_selector(child, selector) for child in value.values())
    if isinstance(value, list):
        return any(_page_evidence_has_selector(child, selector) for child in value)
    return False


def _fill_carry_to_interaction(carry: Mapping[str, Any], trajectory_index: int) -> ScoutedInteraction:
    interaction = {key: value for key, value in carry.items() if key != "available_fields"}
    interaction["trajectory_index"] = trajectory_index
    interaction["carried"] = True
    return cast(ScoutedInteraction, interaction)


def hydrate_prior_carried_trajectory(ctx: AgentContext) -> bool:
    """Put the retained record into this turn's trajectory, marked ``carried``.

    Unconditional: what the previous turn did is not contingent on where this turn's
    browser happens to be standing. Entries keep ``source_url``, so a consumer that
    needs current-page truth reads the page rather than being handed a shorter record.
    """
    if ctx.carried_trajectory_rebound_done:
        return False
    ctx.carried_trajectory_rebound_done = True
    prior = [raw for raw in ctx.prior_carried_trajectory if isinstance(raw, Mapping)]
    if not prior:
        return False
    for carry in prior:
        credential_id = carry.get("credential_id")
        available_fields = carry.get("available_fields")
        if carry.get("tool_name") == "fill_credential_field" and credential_id and isinstance(available_fields, list):
            ctx.scouted_credential_field_inventory_by_credential_id.setdefault(
                str(credential_id), frozenset(str(field_name) for field_name in available_fields)
            )
    trajectory = list(ctx.scout_trajectory)
    for carry in prior:
        trajectory.append(_fill_carry_to_interaction(carry, _next_trajectory_index(trajectory)))
    ctx.scout_trajectory = _capped_with_eviction_accounting(trajectory, collection="scout_trajectory")
    LOG.info("copilot_carried_trajectory_hydrated", interaction_count=len(prior))
    return True


_ACT_OBSERVE_TOOLS = frozenset({"click"})


def _scout_act_observe_capture_outcome(parsed: dict[str, Any] | None, *, started: float, timeout_seconds: float) -> str:
    if parsed is None:
        return "timeout" if time.monotonic() - started >= timeout_seconds else "error"
    if has_bounded_page_schema(parsed):
        return "attached"
    return "hollow"


def _scout_act_observe_no_payload_result(*, started: float, timeout_seconds: float) -> str:
    return "timeout" if time.monotonic() - started >= timeout_seconds else "no_payload"


def _evidence_list_len(packet: dict[str, Any] | None, key: str) -> int:
    if not isinstance(packet, dict):
        return 0
    value = packet.get(key)
    return len(value) if isinstance(value, list) else 0


async def _scout_act_observe_page_evidence(
    ctx: AgentContext, *, url: str, observed_after_interaction: bool = False
) -> dict[str, Any] | None:
    """Run the bounded page-side extractor right after a scout interaction.

    Degrades to None on timeout or error so the interaction result is never
    blocked or failed by capture problems. Hollow packets still return so an
    interaction-proven hollow page can be recorded as a typed outcome."""
    if getattr(ctx, "discovery_mcp_server", None) is None:
        return None
    timeout_seconds = settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS
    started = time.monotonic()
    ctx.last_scout_act_observe_recapture_attempted = False
    ctx.last_scout_act_observe_recapture_result = ""
    parsed: dict[str, Any] | None = None
    try:
        parsed = await _composition_get_structured_evidence(
            ctx, inspected_url=url, current_url=url, timeout_seconds=timeout_seconds
        )
    except Exception:
        parsed = None
        outcome = "error"
    else:
        outcome = _scout_act_observe_capture_outcome(parsed, started=started, timeout_seconds=timeout_seconds)
        if parsed is not None and (outcome == "hollow" or challenge_evidence_unsettled(parsed)):
            first_packet = parsed
            first_outcome = outcome
            remaining_seconds = timeout_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                ctx.last_scout_act_observe_recapture_result = "not_attempted_no_budget"
            else:
                ctx.last_scout_act_observe_recapture_attempted = True
                try:
                    recaptured = await _composition_get_structured_evidence(
                        ctx, inspected_url=url, current_url=url, timeout_seconds=remaining_seconds
                    )
                except Exception:
                    parsed = first_packet
                    outcome = first_outcome
                    ctx.last_scout_act_observe_recapture_result = (
                        "timeout" if time.monotonic() - started >= timeout_seconds else "error"
                    )
                else:
                    if recaptured is None:
                        parsed = first_packet
                        outcome = first_outcome
                        ctx.last_scout_act_observe_recapture_result = _scout_act_observe_no_payload_result(
                            started=started, timeout_seconds=timeout_seconds
                        )
                    else:
                        recaptured_outcome = _scout_act_observe_capture_outcome(
                            recaptured, started=started, timeout_seconds=timeout_seconds
                        )
                        # Never trade down: a page that navigated mid-recapture would otherwise
                        # lose the form the first capture proved, or erase the challenge signal
                        # that justified re-looking while still reporting a bounded schema.
                        if (
                            first_outcome == "attached" and recaptured_outcome != "attached"
                        ) or challenge_signal_regressed(first_packet, recaptured):
                            parsed = first_packet
                            outcome = first_outcome
                        else:
                            parsed = recaptured
                            outcome = recaptured_outcome
                        ctx.last_scout_act_observe_recapture_result = recaptured_outcome
    ctx.last_scout_act_observe_outcome = outcome
    ctx.last_scout_act_observe_packet = parsed
    LOG.info(
        "copilot_scout_act_observe",
        outcome=outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        url=url,
        result_container_count=_evidence_list_len(parsed, "result_containers"),
        key_value_relation_count=_evidence_list_len(parsed, "key_value_relations"),
        recapture_attempted=ctx.last_scout_act_observe_recapture_attempted,
        recapture_result=ctx.last_scout_act_observe_recapture_result,
    )
    await solve_challenge_when_evidence_settles(
        ctx, parsed, url=url, observed_after_interaction=observed_after_interaction
    )
    return parsed


async def _register_scout_interaction_observation(
    ctx: AgentContext, *, tool_name: str, selector: str | None, source_url: str | None, url: str
) -> tuple[int | None, dict[str, Any] | None]:
    # A successful scout interaction reaches the post-action page; record it as an
    # interaction-reached observation so a click-reached block can be authored
    # against it without a separate inspect_page_for_composition.
    selector = _selector_text(selector)
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
    if tool_name in _ACT_OBSERVE_TOOLS:
        parsed = await _scout_act_observe_page_evidence(ctx, url=url, observed_after_interaction=True)
        # Admission (credit axis) is decoupled from the hollow outcome (no-progress axis): a page
        # that rendered witnessed value content is bindable even when it exposes no actionable schema.
        if parsed is not None and (has_bounded_page_schema(parsed) or has_witnessed_value_content(parsed)):
            # Identity keys overwrite the parsed packet so the entry stays a
            # scout_interaction observation, with the schema merged before append.
            evidence = {**parsed, **evidence}
            page_evidence = evidence
            contract = mint_scout_observation_contract_for_ctx(ctx, parsed, url=url)
            ctx.scout_observation_contract = contract
            record_scouted_output_coverage(
                ctx, parsed, contract=contract, include_lexical=has_actionable_steer_content(parsed)
            )
            _mark_post_run_page_observed(ctx, source_tool="evaluate", url=url, page_evidence=parsed)
            # The schema is already attached; leaving the marker set would let a
            # later evaluate/inspect mint a second interaction credit for one click.
            _clear_pending_browser_interaction_observation(ctx)
        elif parsed is not None and ctx.last_scout_act_observe_outcome == "hollow":
            record_build_test_outcome(
                ctx,
                recorded_outcome_from_scout_act_observe_hollow(
                    interaction_tool=tool_name,
                    selector=selector,
                    current_url=url,
                    source_url=source_url,
                    page_evidence=parsed,
                    recapture_attempted=ctx.last_scout_act_observe_recapture_attempted,
                    recapture_result=ctx.last_scout_act_observe_recapture_result,
                ),
            )
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
    serialized result under the scout result budget by shedding sections —
    never by slicing the serialized JSON."""
    data = result.get("data")
    if not isinstance(data, dict):
        return
    try:
        summary = _build_scout_page_summary(page_evidence)
        data["page"] = summary
        while len(json.dumps(result)) > _SCOUT_RESULT_CHAR_CAP:
            if not _shed_scout_page_summary_section(summary):
                data.pop("page", None)
                return
    except Exception:
        data.pop("page", None)
        LOG.warning("copilot_scout_act_observe_summary_failed", exc_info=True)


def _page_evidence_has_password_control(page_evidence: dict[str, Any]) -> bool:
    forms = page_evidence.get("forms")
    if not isinstance(forms, list):
        return False
    for form in forms:
        if not isinstance(form, dict):
            continue
        fields = form.get("fields")
        if not isinstance(fields, list):
            continue
        for form_field in fields:
            if isinstance(form_field, dict) and str(form_field.get("type") or "").strip().lower() == "password":
                return True
    return False


def _record_scout_page_observation(ctx: AgentContext, page_evidence: dict[str, Any]) -> None:
    observed_index: int | None = None
    for item in ctx.scout_trajectory:
        if not isinstance(item, dict):
            continue
        index = item.get("trajectory_index")
        if isinstance(index, int) and (observed_index is None or index > observed_index):
            observed_index = index
    ctx.last_scout_observation_trajectory_index = observed_index
    ctx.last_scout_observation_has_password_control = _page_evidence_has_password_control(page_evidence)


async def _scout_session_download_names(ctx: AgentContext) -> frozenset[str] | None:
    """Filenames currently registered in the scout's browser session, or empty when unavailable.

    Read-only and failure-tolerant: this only sharpens download detection, so a storage hiccup must
    never break a scout click."""
    browser_session_id = ctx.browser_session_id
    organization_id = ctx.organization_id
    if not browser_session_id or not organization_id:
        return None
    try:
        files = await app.STORAGE.list_downloaded_files_in_browser_session(
            organization_id=organization_id, browser_session_id=browser_session_id
        )
    except Exception:
        LOG.warning("copilot_scout_download_snapshot_failed", exc_info=True)
        return None
    return frozenset(str(name) for name in files or ())


def _record_popup_navigation_headers(ctx: AgentContext, response: Response) -> None:
    """Record the popup's own document content type as the browser received it.

    Re-requesting the URL to read headers would send the context's cookies to an address the page
    chose, replaying one-click confirm links and sign-in redirects, and many document endpoints
    answer HEAD differently or refuse it. The navigation response the browser already made costs
    nothing and, unlike `document.contentType`, cannot be shadowed by page script.
    """
    try:
        if ctx.pending_scout_popup_content_type is None and response.request.is_navigation_request():
            ctx.pending_scout_popup_content_type = str(response.headers.get("content-type", ""))
    except Exception:
        LOG.debug("copilot_popup_navigation_headers_unreadable", exc_info=True)


def _record_scout_download(ctx: AgentContext) -> None:
    ctx.pending_scout_download = True


def _watch_downloads_for_click(ctx: AgentContext, page: Page) -> None:
    """Record downloads from ``page`` for the in-flight click, and register the remover.

    ``once`` would be wrong here: it detaches only when it fires, so a click that downloads nothing
    leaves a live writer behind that a later download would deliver into another click's window.
    """

    def _capture(_: Download) -> None:
        _record_scout_download(ctx)

    page.on("download", _capture)
    ctx.pending_scout_download_detachers.append(lambda: page.remove_listener("download", _capture))


def _release_scout_download_listeners(ctx: AgentContext) -> None:
    for detach in ctx.pending_scout_download_detachers:
        try:
            detach()
        except Exception:
            LOG.debug("copilot_scout_download_listener_detach_failed", exc_info=True)
    ctx.pending_scout_download_detachers = []


async def _arm_scout_download_listener(ctx: AgentContext) -> None:
    """Arm a download listener for the click about to dispatch.

    The browser reports the download the click fired as an event, so detection does not depend on
    when the session store registers the file — the store lags the event by seconds (watcher
    upload) or never sees it (vendor sessions), and a diff read in that window is blind."""
    _release_scout_download_listeners(ctx)
    ctx.pending_scout_download = False
    try:
        browser_state = await resolve_browser_state_for_context(ctx)
        if browser_state is None:
            return
        page = await browser_state.get_or_create_page()
        _watch_downloads_for_click(ctx, page)
    except Exception:
        LOG.warning("copilot_scout_download_listener_failed", exc_info=True)


async def _arm_scout_popup_listener(ctx: AgentContext) -> None:
    """Arm a one-shot popup listener before a click dispatches.

    Playwright reports the popup the click opened by identity, so nothing polls and an ordinary
    click costs nothing; a URL-set diff would both stall every click and mistake a same-tab
    navigation for a new tab."""
    ctx.pending_scout_popup = None
    ctx.pending_scout_popup_content_type = None
    try:
        browser_state = await resolve_browser_state_for_context(ctx)
        if browser_state is None:
            return
        page = await browser_state.get_or_create_page()

        def _capture(popup: Page) -> None:
            ctx.pending_scout_popup = popup
            popup.on("response", lambda response: _record_popup_navigation_headers(ctx, response))
            # An <a download target=_blank> click fires its download event on the transient popup
            # page, never on the clicked page.
            _watch_downloads_for_click(ctx, popup)

        page.once("popup", _capture)
    except Exception:
        LOG.warning("copilot_scout_popup_listener_failed", exc_info=True)


# Bounded so a page that stalls the probe cannot spend the turn budget one click at a time.
_RENDER_PROBE_TIMEOUT_MS = 5000.0


def _attach_observed_click_effect(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    selector: str,
    effect: str,
) -> None:
    """Attach a browser-observed click effect without choosing a future action or locator."""
    data = result.get("data")
    if not isinstance(data, dict):
        return
    result_effects = data.setdefault("observed_effects", {})
    if isinstance(result_effects, dict):
        result_effects[effect] = True
    for collection_name in ("scout_trajectory", "scouted_interactions"):
        collection = getattr(ctx, collection_name, None)
        if not isinstance(collection, list):
            continue
        for interaction in reversed(collection):
            if interaction.get("tool_name") != "click" or interaction.get("selector") != selector:
                continue
            effects = dict(interaction.get("observed_effects") or {})
            effects[effect] = True
            interaction["observed_effects"] = effects
            break


async def _maybe_attach_observed_render_target(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    selector: str,
    url: str,
) -> None:
    """Report when the click opened an image document in a new tab."""
    data = result.get("data")
    if not isinstance(data, dict) or not selector:
        return
    popup = ctx.pending_scout_popup
    ctx.pending_scout_popup = None
    if popup is None:
        return
    try:
        await popup.wait_for_load_state("domcontentloaded", timeout=_RENDER_PROBE_TIMEOUT_MS)
        content_type = ctx.pending_scout_popup_content_type or ""
        ctx.pending_scout_popup_content_type = None
        if not content_type:
            content_type = str(await popup.evaluate("document.contentType") or "")
        if not content_type.lower().startswith("image/"):
            LOG.debug("copilot_observed_render_declined", reason="not_image_render", url=url, content_type=content_type)
            return
        _attach_observed_click_effect(ctx, result, selector=selector, effect="rendered_document_opened")
    except Exception:
        LOG.warning("copilot_observed_render_target_attach_failed", exc_info=True)


async def _maybe_attach_observed_download_target(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    selector: str,
    url: str,
) -> None:
    """Report when the scout's click directly produced a download."""
    data = result.get("data")
    if not isinstance(data, dict) or not selector:
        return
    observed_event = ctx.pending_scout_download
    ctx.pending_scout_download = False
    _release_scout_download_listeners(ctx)
    before = ctx.pending_scout_download_snapshot
    ctx.pending_scout_download_snapshot = None
    try:
        download_signal = "event" if observed_event else None
        if download_signal is None:
            if before is None:
                return
            after = await _scout_session_download_names(ctx)
            if after is None or not (after - before):
                return
            download_signal = "store_diff"
        LOG.info("copilot_observed_download_signal", signal=download_signal, url=url)
        _attach_observed_click_effect(ctx, result, selector=selector, effect="download_started")
    except Exception:
        LOG.warning("copilot_observed_download_target_attach_failed", exc_info=True)


async def _attach_evaluate_page_facts(ctx: AgentContext, result: dict[str, Any], *, url: str) -> None:
    """Attach bounded page facts without selecting or suggesting the model's next action."""
    if not isinstance(result.get("data"), dict):
        return
    page_evidence = await _scout_act_observe_page_evidence(ctx, url=url)
    if page_evidence is None:
        return
    contract = mint_scout_observation_contract_for_ctx(ctx, page_evidence, url=url)
    ctx.scout_observation_contract = contract
    record_scouted_output_coverage(ctx, page_evidence, contract=contract, include_lexical=False)
    _record_scout_page_observation(ctx, page_evidence)
    if has_bounded_page_schema(page_evidence):
        _append_flow_evidence(ctx, page_evidence, reached_via="current_page")
    _attach_scout_page_summary(result, page_evidence)


def _mark_post_run_page_observed(
    ctx: AgentContext,
    *,
    source_tool: str,
    url: str,
    page_evidence: dict[str, Any] | None = None,
) -> None:
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return
    ctx.post_run_page_observation_tool = source_tool
    ctx.post_run_page_observation_url = url
    ctx.post_run_page_observation_workflow_run_id = run_id
    latest_outcome = getattr(ctx, "latest_recorded_build_test_outcome", None)
    authoritative_unsatisfied = (
        isinstance(latest_outcome, RecordedBuildTestOutcome)
        and latest_outcome.is_authoritative
        and latest_outcome.phase == "persisted_block_run"
        and latest_outcome.reason_code in _AMBIGUOUS_NON_DEMONSTRATION_RUN_REASON_CODES
        and latest_outcome.workflow_run_id == run_id
    )
    ctx.post_run_page_observation_after_failed_test = (
        getattr(ctx, "last_test_ok", None) is False or authoritative_unsatisfied
    )
    if page_evidence is not None and ctx.post_run_page_observation_after_failed_test:
        bound_evidence = {
            **page_evidence,
            "workflow_run_id": run_id,
            "observed_after_workflow_run": True,
            "current_url": url,
        }
        if bind_post_run_page_path_failure(ctx, bound_evidence):
            ctx.post_run_page_observation_generation = (
                getattr(ctx, "post_run_page_observation_generation", 0) or 0
            ) + 1
    evidence = _workflow_verification_evidence(ctx)
    evidence.live_page_state_verified = True
    evidence.verified_from_current_browser_state = True
    evidence.workflow_run_id = run_id
    if url:
        evidence.current_url = url
        evidence.current_url_observed_after_workflow_run = True
        evidence.current_url_may_encode_runtime_state = bool(urlparse(url).query)
