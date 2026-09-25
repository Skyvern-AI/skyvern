from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal, cast
from urllib.parse import urlparse, urlsplit

import structlog
from playwright.async_api import BrowserContext, Download, Frame, Page, Response

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
    challenge_evidence_unsettled,
    challenge_signal_regressed,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    composition_structured_evidence_expression,
    enclosing_form_submit_controls_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    scout_accessible_role_name_expression as _scout_accessible_role_name_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    scout_pre_action_expression as _scout_pre_action_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    selector_match_count_expression as _selector_match_count_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    has_actionable_steer_content,
    has_bounded_page_schema,
    has_witnessed_value_content,
    parse_composition_structured,
    post_run_evidence_source_refused,
    stamp_page_evidence_provenance,
)
from skyvern.forge.sdk.copilot.context import SIGNED_OUT_PAGE_SUMMARY_CHAR_CAP, SignedOutPageObservation
from skyvern.forge.sdk.copilot.enforcement import (
    mint_scout_observation_contract_for_ctx,
    record_reached_terminal_action_observation,
    record_scouted_output_coverage,
)
from skyvern.forge.sdk.copilot.mcp_adapter import service_worker_blocked_context
from skyvern.forge.sdk.copilot.output_utils import BLOCK_FACT_URL_MAX_CHARS, screened_recorded_url
from skyvern.forge.sdk.copilot.page_identity import page_location_fingerprint as _page_evidence_location_fingerprint
from skyvern.forge.sdk.copilot.page_identity import page_record_matches_url as _page_evidence_matches_url_identity
from skyvern.forge.sdk.copilot.page_identity import (
    safe_page_origin,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    PendingBrowserInteractionObservation,
    ScoutedInteraction,
    ScoutedSelectorCandidate,
    current_call_browser_session_override,
    effective_browser_session_id,
    resolve_browser_state_for_context,
    sensitive_origin_page_facts_withheld,
    sensitive_origin_page_is_tainted,
)
from skyvern.forge.sdk.copilot.screenshot_utils import (
    ScreenshotActionRelation,
    ScreenshotProvenance,
    ViewportFrame,
    consume_screenshot_artifact,
    enqueue_screenshot,
    screenshot_result_facts,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    registered_scrub_values,
    scrub_secrets_from_structure,
    scrub_secrets_from_text,
)
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.utils.challenge_signature import CHALLENGE_VENDOR_SIGNATURE

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

_SIGNED_OUT_SETTLE_DELAY_SECONDS = 2.5
_SIGNED_OUT_OBSERVATION_TIMEOUT_MS = 20000
_SIGNED_OUT_OBSERVATION_ATTEMPT_LIMIT = 12
_SIGNED_OUT_OBSERVATION_ORIGIN_LIMIT = 3
_SIGNED_OUT_MAX_CONTROLS = 8
_SIGNED_OUT_MAX_SELECTOR_CANDIDATES = 3
_SIGNED_OUT_TEXT_EXCERPT_CAP = 400


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
            tool_name_present=bool(pending.tool_name),
            pending_url_present=bool(pending.url),
            current_url_present=bool(current_url),
        )
        return False
    return True


_MAX_SCOUTED_INTERACTIONS = 60


async def _live_working_page_url(ctx: AgentContext) -> str | None:
    if not effective_browser_session_id(ctx):
        return None
    try:
        browser_state = await resolve_browser_state_for_context(ctx, session_id=effective_browser_session_id(ctx))
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


def _prenav_role_name_for_selector(pending: tuple[str, str, str] | None, selector: str) -> tuple[str, str]:
    """Return the pre-navigation (role, accessible_name) only when the recorded selector matches the
    stashed one, so a navigating click's anchor is never applied to a different element."""
    if pending is None:
        return "", ""
    stashed_selector, role, name = pending
    if stashed_selector != _selector_text(selector):
        return "", ""
    return role, name


def _prenav_ambiguity_for_selector(pending: tuple[str, bool] | None, selector: str) -> bool:
    """Return the stashed ambiguity verdict only when the recorded selector matches the probed one, so a
    navigating click's verdict is never applied to a different element."""
    if pending is None:
        return False
    stashed_selector, ambiguous = pending
    if stashed_selector != _selector_text(selector):
        return False
    return ambiguous


def _clear_pending_scout_selector_facts(ctx: AgentContext) -> None:
    ctx.pending_scout_role_name = None
    ctx.pending_scout_role_name_match_count = None
    ctx.pending_scout_selector_candidates = None
    ctx.pending_scout_ambiguous = None
    ctx.pending_scout_reanchor = None
    ctx.pending_scout_selector_match_count = None


def _non_negative_count(value: Any) -> int | None:
    """A live cardinality, or None for the expression's "could not evaluate" sentinel."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _parse_selector_candidates(raw_candidates: Any) -> list[ScoutedSelectorCandidate]:
    candidates: list[ScoutedSelectorCandidate] = []
    if not isinstance(raw_candidates, list):
        return candidates
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        candidate_selector = _selector_text(raw.get("selector"))
        candidate_source = _selector_text(raw.get("source"))
        if not candidate_selector or not candidate_source:
            continue
        candidate: ScoutedSelectorCandidate = {
            "selector": candidate_selector,
            "source": candidate_source,
            "match_count": _non_negative_count(raw.get("match_count")),
        }
        if not any(existing["selector"] == candidate_selector for existing in candidates):
            candidates.append(candidate)
    return candidates


def _packet_role_name(packet: dict[str, Any]) -> tuple[str, str]:
    role_name = packet.get("role_name")
    if not isinstance(role_name, dict):
        return "", ""
    return str(role_name.get("role") or "").strip(), str(role_name.get("accessible_name") or "").strip()


def _apply_scout_pre_action_packet(ctx: AgentContext, selector: str, packet: dict[str, Any], *, source: str) -> None:
    role, name = _packet_role_name(packet)
    if role and name:
        ctx.pending_scout_role_name = (selector, role, name)
    else:
        LOG.info(
            "copilot_scout_role_name_unavailable",
            reason="empty_role" if not role else "empty_name",
            source=source,
            role_present=bool(role),
        )

    role_count = _non_negative_count(packet.get("role_name_match_count"))
    if role and name and role_count is not None:
        ctx.pending_scout_role_name_match_count = (selector, role, name, role_count)

    candidates = _parse_selector_candidates(packet.get("selector_candidates"))
    if candidates:
        ctx.pending_scout_selector_candidates = candidates

    selector_count = _non_negative_count(packet.get("selector_match_count"))
    if selector_count is None:
        return
    ctx.pending_scout_selector_match_count = (selector, selector_count)
    if selector_count <= 1:
        return
    ctx.pending_scout_ambiguous = (selector, True)
    if role and name and role_count == 1:
        ctx.pending_scout_reanchor = (selector, role, name)


async def _capture_scout_pre_action(ctx: AgentContext, selector: str | None) -> None:
    """Stash every pre-action source-page fact from one bounded page read.

    The read runs inline before the action dispatches, so a failure leaves the facts it could not
    reach unset and the action proceeds; the caller's TIER 1 role/name parse is passed in because a
    role-engine selector is not valid CSS and could not be counted from the page alone."""
    _clear_pending_scout_selector_facts(ctx)
    selector = _selector_text(selector)
    if not selector:
        return
    parsed = _role_name_from_selector(selector)
    source = "selector" if parsed is not None else "page_read"
    parsed_role, parsed_name = (parsed[0].strip(), parsed[1].strip()) if parsed is not None else ("", "")
    server = ctx.discovery_mcp_server
    result = None
    if server is not None:
        try:
            result = await asyncio.wait_for(
                server.call_internal_tool(
                    "skyvern_evaluate",
                    {"expression": _scout_pre_action_expression(selector, parsed_role, parsed_name)},
                ),
                timeout=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS,
            )
        except Exception:
            result = None
    # Held raw in this turn's pending stash: the record-time scrub compares the raw identity to its
    # scrubbed form to decide whether a locator was rewritten, and a packet scrubbed here would
    # defeat that comparison.
    packet = (result.get("data") or {}).get("result") if isinstance(result, dict) and result.get("ok") else None
    if not isinstance(packet, dict):
        LOG.info("copilot_scout_role_name_unavailable", reason="page_read_failed", source=source)
        if parsed is None:
            return
        # The parse needed no page, so an unreachable page must not cost the identity it already gave.
        packet = {"role_name": {"role": parsed_role, "accessible_name": parsed_name}}
    _apply_scout_pre_action_packet(ctx, selector, packet, source=source)


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


def _clean_element_fingerprint(raw_fingerprint: Any) -> dict[str, str]:
    if not isinstance(raw_fingerprint, dict):
        return {}
    captured = {k: str(v).strip() for k, v in raw_fingerprint.items() if v}
    captured["probed"] = _FINGERPRINT_PROBED_ATTRS
    return captured


def _target_facts_expression(css_selector: str, role: str, name: str, *, fingerprint: bool) -> str:
    fingerprint_read = (
        f"(() => {{ try {{ return {_element_fingerprint_expression(css_selector)}; }} catch (e) {{ return null; }} }})()"
        if fingerprint
        else "null"
    )
    packet_read = (
        f"(() => {{ try {{ return {_scout_pre_action_expression(css_selector, role, name)}; }} "
        "catch (e) { return null; } })()"
    )
    return f"(() => ({{ packet: {packet_read}, fingerprint: {fingerprint_read} }}))()"


async def _probe_target_facts(
    ctx: AgentContext, selector: str, *, fingerprint: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read a target's pre-action packet, and its fingerprint when asked, in one page read.
    A failed read keeps only the TIER 1 role/name parse, which needs no page."""
    parsed = _role_name_from_selector(selector)
    parsed_role, parsed_name = (parsed[0].strip(), parsed[1].strip()) if parsed is not None else ("", "")
    fallback: dict[str, Any] = (
        {"role_name": {"role": parsed_role, "accessible_name": parsed_name}} if parsed is not None else {}
    )
    server = ctx.discovery_mcp_server
    if server is None:
        return fallback, {}
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _target_facts_expression(selector, parsed_role, parsed_name, fingerprint=fingerprint)},
            ),
            timeout=_DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
        )
    except Exception:
        return fallback, {}
    value = (result.get("data") or {}).get("result") if isinstance(result, dict) and result.get("ok") else None
    if not isinstance(value, dict) or not isinstance(value.get("packet"), dict):
        return fallback, {}
    return value["packet"], _clean_element_fingerprint(value.get("fingerprint"))


async def _capture_post_interaction_screenshot(
    ctx: AgentContext,
    *,
    source_tool: str,
    captured_url: str | None,
    observation_step: int | None = None,
    timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    frame: ViewportFrame | None = None,
) -> bool:
    """Attach a look at the page after a state-changing action, reporting whether a frame staged.
    Reading the DOM answers "what is on the page" but not "did that work" -- a filled password reads
    as empty, and a dialog covering the content reads as an ordinary node.
    """
    if ctx.codeblock_redaction_parameters:
        return False
    if sensitive_origin_page_is_tainted(ctx):
        return False
    if not ctx.supports_vision:
        return False
    if frame is None:
        frame = await _take_viewport_frame(ctx, timeout_seconds=timeout_seconds)
        if frame is None:
            return False
    return enqueue_screenshot(
        ctx,
        base64.b64encode(frame.png).decode("ascii"),
        provenance=ScreenshotProvenance(
            source_tool=source_tool,
            captured_url=frame.producer_url,
            observation_step=observation_step,
            browser_session_id=frame.producer_session_id,
            workflow_run_id=None,
            action_relation=ScreenshotActionRelation.AFTER_SOURCE_ACTION,
            dispatch_url=captured_url,
            dispatch_browser_session_id=frame.dispatch_session_id,
            producer_browser_session_id=frame.producer_session_id,
            session_binding=frame.session_binding,
        ),
        captured_at=frame.started_at,
    )


async def _take_viewport_frame(ctx: AgentContext, *, timeout_seconds: float) -> ViewportFrame | None:
    """Read the viewport without staging it; withheld during self-heal or on a sensitive-origin page."""
    if ctx.codeblock_redaction_parameters:
        return None
    if sensitive_origin_page_is_tainted(ctx):
        return None
    server = ctx.discovery_mcp_server
    if server is None:
        return None
    started_at = time.monotonic()
    dispatch_session_id = effective_browser_session_id(ctx)
    screenshot_arguments = {"session_id": dispatch_session_id} if dispatch_session_id else {}
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_screenshot", screenshot_arguments),
            timeout=timeout_seconds,
        )
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    png = consume_screenshot_artifact(result)
    if png is None or sensitive_origin_page_is_tainted(ctx):
        return None
    producer_url, producer_session_id, session_binding = screenshot_result_facts(
        result, dispatch_url=None, dispatch_browser_session_id=dispatch_session_id
    )
    return ViewportFrame(
        png=png,
        dispatch_session_id=dispatch_session_id,
        producer_url=producer_url,
        producer_session_id=producer_session_id,
        session_binding=session_binding,
        started_at=started_at,
    )


def _model_visible_selector_candidates(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [
        {key: item for key, item in candidate.items() if key != "match_count"}
        if isinstance(candidate, dict)
        else candidate
        for candidate in value
    ]


async def _capture_enclosing_form_submits(
    ctx: AgentContext, selector: str | None, *, timeout_seconds: float = _DISCOVERY_PER_CALL_TIMEOUT_SECONDS
) -> list[dict[str, Any]]:
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
    facts: list[dict[str, Any]] = []
    for entry in controls:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "")[:80]
        candidates = entry.get("selector_candidates")
        if not label and not candidates:
            continue
        facts.append({"label": label, "selector_candidates": _model_visible_selector_candidates(candidates)})
    return facts


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
                "tool_name_present": bool(item.get("tool_name")),
                "selector_present": bool(item.get("selector")),
                "source_url_present": bool(item.get("source_url")),
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


def _redact_codeblock_value(ctx: AgentContext, value: Any) -> Any:
    parameters = getattr(ctx, "codeblock_redaction_parameters", {})
    return app.AGENT_FUNCTION.redact_codeblock_parameter_values(value, parameters) if parameters else value


def _record_scout_trajectory_fact(ctx: AgentContext, artifact: ScoutedInteraction) -> ScoutedInteraction | None:
    """Append one observed fact with a monotone index and bounded retention."""
    redacted = _redact_codeblock_value(ctx, artifact)
    if not isinstance(redacted, dict) or "tool_name" not in redacted:
        return None
    trajectory = list(ctx.scout_trajectory)
    recorded = cast(ScoutedInteraction, redacted.copy())
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
        if not _page_evidence_matches_url_identity(packet, source_url):
            continue
        for form in packet.get("forms") or []:
            if not isinstance(form, dict):
                continue
            controls = [*(form.get("fields") or []), *(form.get("submit_controls") or [])]
            for control in controls:
                if not isinstance(control, dict):
                    continue
                candidates = control.get("selector_candidates")
                candidate_selectors = (
                    {
                        _selector_text(candidate.get("selector"))
                        for candidate in candidates
                        if isinstance(candidate, dict) and candidate.get("match_count") == 1
                    }
                    if isinstance(candidates, list)
                    else set()
                )
                if _selector_text(control.get("selector")) != selector and selector not in candidate_selectors:
                    continue
                observed_hidden = observed_hidden or control.get("visible") is False
                observed_disabled = observed_disabled or control.get("disabled") is True
    return observed_hidden, observed_disabled


# Every field the synthesizer can build a locator from: selectors, the role/name pair it falls
# back to, and the element fingerprint that identifies the element across equivalent selectors.
_RETAINED_LOCATOR_IDENTITY_FIELDS = (
    "selector",
    "executed_selector",
    "selector_candidates",
    "role",
    "accessible_name",
    "element_fingerprint_id",
    "element_fingerprint_name",
    "element_fingerprint_type",
    "element_fingerprint_placeholder",
    "element_fingerprint_label",
    "element_fingerprint_test_id",
    "element_fingerprint_tag",
    "element_fingerprint_probed",
)


def _scrub_retained_interaction(ctx: AgentContext, artifact: Any) -> Any:
    """Scrub a retained interaction with every registered value; if the scrub rewrote any locator
    identity, drop the whole identity rather than keep it. A placeholder inside a selector, a
    role/name, or a fingerprint is a dead locator that would fail a later run silently, and the
    secret it replaced must not survive into the next turn. The identity is dropped as a set so the
    synthesizer cannot rebuild the dead locator from a sibling field."""
    scrubbed = scrub_secrets_from_structure(ctx, artifact)
    if not isinstance(artifact, dict) or not isinstance(scrubbed, dict):
        return scrubbed
    rewritten = any(
        field_name in scrubbed and scrubbed[field_name] != artifact.get(field_name)
        for field_name in _RETAINED_LOCATOR_IDENTITY_FIELDS
    )
    if rewritten:
        for field_name in _RETAINED_LOCATOR_IDENTITY_FIELDS:
            scrubbed.pop(field_name, None)
    return scrubbed


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
            url_present=bool((source_url or "").strip()),
        )
        return
    artifact: ScoutedInteraction = {"tool_name": tool_name}
    demonstrated_session_id = current_call_browser_session_override()
    if demonstrated_session_id is not None and demonstrated_session_id != ctx.browser_session_id:
        artifact["demonstrated_browser_session_id"] = demonstrated_session_id
    if selector:
        artifact["selector"] = selector
        artifact["executed_selector"] = selector
    if selector_candidates:
        normalized_candidates: list[ScoutedSelectorCandidate] = []
        for candidate in selector_candidates:
            candidate_selector = _selector_text(candidate.get("selector"))
            source = _selector_text(candidate.get("source"))
            if not candidate_selector or not source:
                continue
            raw_count = candidate.get("match_count")
            normalized: ScoutedSelectorCandidate = {
                "selector": candidate_selector,
                "source": source,
                "match_count": _non_negative_count(raw_count),
            }
            if not any(existing["selector"] == candidate_selector for existing in normalized_candidates):
                normalized_candidates.append(normalized)
        if normalized_candidates:
            artifact["selector_candidates"] = normalized_candidates
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
    redacted_artifact = _scrub_retained_interaction(ctx, _redact_codeblock_value(ctx, artifact))
    if not isinstance(redacted_artifact, dict) or "tool_name" not in redacted_artifact:
        return
    artifact = cast(ScoutedInteraction, redacted_artifact)
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

    recorded = _record_scout_trajectory_fact(ctx, artifact)
    if artifact["tool_name"] == "click":
        # Both collections hold their own object for this click, and an effect observed after the
        # fact updates these rather than re-finding them by an identity the scrub may have removed.
        click_records: list[ScoutedInteraction] = [artifact]
        if recorded is not None:
            click_records.append(recorded)
        ctx.pending_scout_click_records = click_records

    LOG.info(
        "copilot_scout_interaction_captured",
        tool_name=artifact["tool_name"],
        selector=artifact.get("selector"),
        source_url=artifact.get("source_url"),
        role=artifact.get("role"),
        credential_field=artifact.get("credential_field"),
        credential_id=artifact.get("credential_id"),
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
    executed_selector = interaction.get("executed_selector")
    if "selector" not in interaction and isinstance(executed_selector, str) and executed_selector:
        interaction["selector"] = executed_selector
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


_PAGE_EVIDENCE_STATE_KEYS = (
    "page_title",
    "size_compaction",
    "forms",
    "navigation_targets",
    "navigation_targets_truncated",
    "result_containers",
    "result_containers_truncated",
    "key_value_relations",
    "key_value_relations_truncated",
    "clickable_controls",
    "visible_text_excerpt",
    "anti_bot_indicators",
    "challenge_controls",
    "modal_overlays",
    "visual_obstruction_candidates",
    "schema_empty_page",
)


def _page_evidence_state(packet: dict[str, Any]) -> dict[str, Any]:
    """Return DOM-observed structure without visual augmentation or transport metadata."""
    state = {key: packet.get(key) for key in _PAGE_EVIDENCE_STATE_KEYS}
    page_obstructions = packet.get("page_obstructions")
    state["page_obstructions"] = (
        [
            obstruction
            for obstruction in page_obstructions
            if isinstance(obstruction, dict) and obstruction.get("source") in {"dom", "dom_html"}
        ]
        if isinstance(page_obstructions, list)
        else []
    )
    inspection_warnings = packet.get("inspection_warnings")
    state["reveal_relations_truncated"] = (
        isinstance(inspection_warnings, list) and "reveal_relations_truncated" in inspection_warnings
    )
    return state


def _page_evidence_is_unchanged(prior: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    return prior is not None and current is not None and _page_evidence_state(prior) == _page_evidence_state(current)


def _safe_page_evidence_url(value: str | None) -> str | None:
    return safe_page_origin(value)


def _latest_same_page_evidence(ctx: AgentContext, *, url: str) -> dict[str, Any] | None:
    for entry in reversed(getattr(ctx, "flow_evidence", ())):
        if not isinstance(entry, dict):
            continue
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            continue
        if not _page_evidence_matches_url_identity(evidence, url):
            continue
        if has_bounded_page_schema(evidence) or has_witnessed_value_content(evidence):
            return evidence
    return None


async def _scout_act_observe_page_evidence(
    ctx: AgentContext,
    *,
    url: str,
    observed_after_interaction: bool = False,
    prior_page_evidence: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run the bounded page-side extractor right after a scout interaction.

    Degrades to None on timeout or error so the interaction result is never
    blocked or failed by capture problems. Hollow packets still return so an
    interaction-proven hollow page can be recorded as a typed outcome. When the
    first packet is unchanged from the latest same-page evidence, one bounded
    recapture prevents pre-action state from being published as the click effect.
    """
    if getattr(ctx, "discovery_mcp_server", None) is None:
        return None
    timeout_seconds = settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS
    started = time.monotonic()
    ctx.last_scout_act_observe_recapture_attempted = False
    ctx.last_scout_act_observe_recapture_result = ""
    parsed: dict[str, Any] | None = None
    # Keep the exact URL inside turn-local evidence for structural grounding. Only
    # model-facing/cross-turn summaries reduce it to origin plus a keyed fingerprint.
    capture_url = url
    capture_location_fingerprint = _page_evidence_location_fingerprint(url)
    try:
        parsed = await _composition_get_structured_evidence(
            ctx, inspected_url=capture_url, current_url=capture_url, timeout_seconds=timeout_seconds
        )
    except Exception:
        parsed = None
        outcome = "error"
    else:
        if parsed is not None and capture_location_fingerprint is not None:
            parsed["current_url_location_fingerprint"] = capture_location_fingerprint
        outcome = _scout_act_observe_capture_outcome(parsed, started=started, timeout_seconds=timeout_seconds)
        unchanged_after_interaction = observed_after_interaction and _page_evidence_is_unchanged(
            prior_page_evidence, parsed
        )
        if parsed is not None and (
            outcome == "hollow" or challenge_evidence_unsettled(parsed) or unchanged_after_interaction
        ):
            first_packet = parsed
            first_outcome = outcome
            remaining_seconds = timeout_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                ctx.last_scout_act_observe_recapture_result = "not_attempted_no_budget"
                if unchanged_after_interaction:
                    parsed = None
                    outcome = "unchanged"
            else:
                ctx.last_scout_act_observe_recapture_attempted = True
                if unchanged_after_interaction:
                    settle_seconds = min(
                        settings.COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS,
                        remaining_seconds,
                    )
                    if settle_seconds > 0:
                        await asyncio.sleep(settle_seconds)
                    remaining_seconds = timeout_seconds - (time.monotonic() - started)
                try:
                    live_recapture_url = await _live_working_page_url(ctx)
                    recapture_url = live_recapture_url or capture_url
                    recaptured = await _composition_get_structured_evidence(
                        ctx,
                        inspected_url=recapture_url,
                        current_url=recapture_url,
                        timeout_seconds=remaining_seconds,
                    )
                except Exception:
                    ctx.last_scout_act_observe_recapture_result = (
                        "timeout" if time.monotonic() - started >= timeout_seconds else "error"
                    )
                    if unchanged_after_interaction:
                        parsed = None
                        outcome = "unchanged"
                    else:
                        parsed = first_packet
                        outcome = first_outcome
                else:
                    recapture_location_fingerprint = _page_evidence_location_fingerprint(live_recapture_url)
                    if recaptured is not None and recapture_location_fingerprint is not None:
                        recaptured["current_url_location_fingerprint"] = recapture_location_fingerprint
                    if recaptured is None:
                        ctx.last_scout_act_observe_recapture_result = _scout_act_observe_no_payload_result(
                            started=started, timeout_seconds=timeout_seconds
                        )
                        if unchanged_after_interaction:
                            parsed = None
                            outcome = "unchanged"
                        else:
                            parsed = first_packet
                            outcome = first_outcome
                    else:
                        recaptured_outcome = _scout_act_observe_capture_outcome(
                            recaptured, started=started, timeout_seconds=timeout_seconds
                        )
                        recaptured_is_attachable = has_bounded_page_schema(recaptured) or has_witnessed_value_content(
                            recaptured
                        )
                        if observed_after_interaction and _page_evidence_is_unchanged(prior_page_evidence, recaptured):
                            parsed = None
                            outcome = "unchanged"
                            ctx.last_scout_act_observe_recapture_result = "unchanged"
                        elif unchanged_after_interaction and not recaptured_is_attachable:
                            parsed = None
                            outcome = "unchanged"
                            ctx.last_scout_act_observe_recapture_result = recaptured_outcome
                        # Never trade down: a page that navigated mid-recapture would otherwise
                        # lose the form the first capture proved, or erase the challenge signal
                        # that justified re-looking while still reporting a bounded schema.
                        elif not unchanged_after_interaction and (
                            (first_outcome == "attached" and recaptured_outcome != "attached")
                            or challenge_signal_regressed(first_packet, recaptured)
                        ):
                            parsed = first_packet
                            outcome = first_outcome
                        else:
                            parsed = recaptured
                            outcome = recaptured_outcome
                        if outcome != "unchanged":
                            ctx.last_scout_act_observe_recapture_result = recaptured_outcome
    safe_parsed = parsed
    if parsed is not None:
        redacted = _redact_codeblock_value(ctx, parsed)
        safe_parsed = cast(dict[str, Any], redacted) if isinstance(redacted, dict) else None
    ctx.last_scout_act_observe_outcome = outcome
    ctx.last_scout_act_observe_packet = safe_parsed
    LOG.info(
        "copilot_scout_act_observe",
        outcome=outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        url_present=bool(url),
        result_container_count=_evidence_list_len(parsed, "result_containers"),
        key_value_relation_count=_evidence_list_len(parsed, "key_value_relations"),
        recapture_attempted=ctx.last_scout_act_observe_recapture_attempted,
        recapture_result=ctx.last_scout_act_observe_recapture_result,
    )
    return safe_parsed


async def _register_scout_interaction_observation(
    ctx: AgentContext, *, tool_name: str, selector: str | None, source_url: str | None, url: str
) -> tuple[int | None, dict[str, Any] | None]:
    # A successful scout interaction reaches the post-action page; record it as an
    # interaction-reached observation so a click-reached block can be authored
    # against it without a separate inspect_page_for_composition.
    origin_run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if sensitive_origin_page_facts_withheld(ctx, origin_run_id):
        return None, None
    selector = _selector_text(selector)
    if not selector or not url:
        return None, None
    identity = {"selector": selector, "source_url": source_url, "url": url}
    redacted_identity = scrub_secrets_from_structure(ctx, _redact_codeblock_value(ctx, identity))
    if not isinstance(redacted_identity, dict):
        return None, None
    if redacted_identity.get("selector") != selector:
        # A selector the scrub rewrote is a dead locator; nothing authored against it could run.
        return None, None
    safe_selector = _selector_text(redacted_identity.get("selector"))
    safe_source_url = _selector_text(redacted_identity.get("source_url"))
    safe_url = _selector_text(redacted_identity.get("url"))
    if not safe_selector or not safe_url:
        return None, None
    evidence: dict[str, Any] = {
        "inspected_url": safe_url,
        "current_url": safe_url,
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
        "interaction_tool": tool_name,
        "interaction_selector": safe_selector,
    }
    if safe_source_url:
        evidence["interaction_source_url"] = safe_source_url
    page_evidence: dict[str, Any] | None = None
    if tool_name in _ACT_OBSERVE_TOOLS:
        # Freshness compares the first post-click DOM to the page the click acted on.
        # Looking up only by the destination URL misses the exact stale-hydration seam:
        # navigation commits first while the old page remains rendered underneath it.
        prior_page_evidence = _latest_same_page_evidence(ctx, url=safe_source_url or safe_url)
        parsed = await _scout_act_observe_page_evidence(
            ctx,
            url=safe_url,
            observed_after_interaction=True,
            prior_page_evidence=prior_page_evidence,
        )
        if sensitive_origin_page_facts_withheld(ctx, origin_run_id):
            ctx.last_scout_act_observe_outcome = None
            ctx.last_scout_act_observe_packet = None
            return None, None
        if parsed is not None:
            scrubbed_parsed = scrub_secrets_from_structure(ctx, parsed)
            parsed = scrubbed_parsed if isinstance(scrubbed_parsed, dict) else None
            ctx.last_scout_act_observe_packet = parsed
        # Admission (credit axis) is decoupled from the hollow outcome (no-progress axis): a page
        # that rendered witnessed value content is bindable even when it exposes no actionable schema.
        if parsed is not None and (has_bounded_page_schema(parsed) or has_witnessed_value_content(parsed)):
            observed_url = str(parsed.get("current_url") or parsed.get("inspected_url") or safe_url).strip() or safe_url
            evidence["inspected_url"] = observed_url
            evidence["current_url"] = observed_url
            # Identity keys overwrite the parsed packet so the entry stays a
            # scout_interaction observation, with the schema merged before append.
            evidence = {**parsed, **evidence}
            page_evidence = evidence
            contract = mint_scout_observation_contract_for_ctx(ctx, parsed, url=safe_url)
            ctx.scout_observation_contract = contract
            record_scouted_output_coverage(
                ctx, parsed, contract=contract, include_lexical=has_actionable_steer_content(parsed)
            )
            _mark_post_run_page_observed(
                ctx,
                source_tool="evaluate",
                url=safe_url,
                page_evidence=parsed,
                source_browser_session_id=effective_browser_session_id(ctx),
            )
            # The schema is already attached; leaving the marker set would let a
            # later evaluate/inspect mint a second interaction credit for one click.
            _clear_pending_browser_interaction_observation(ctx)
        elif ctx.last_scout_act_observe_outcome == "unchanged":
            evidence["page_observation_status"] = "unchanged"
        elif parsed is not None and ctx.last_scout_act_observe_outcome == "hollow":
            record_build_test_outcome(
                ctx,
                recorded_outcome_from_scout_act_observe_hollow(
                    interaction_tool=tool_name,
                    selector=safe_selector,
                    current_url=safe_url,
                    source_url=safe_source_url,
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
_PAGE_SUMMARY_MAX_DISCLOSURE_CONTROLS = 4


def _scrubbed_to(text: str, limit: int, scrub_values: Sequence[str]) -> str:
    """`text` bounded to `limit`, with this session's registered values taken out first.

    A registered value is matched whole, so a bound that cuts one leaves a prefix no later pass can
    recognise. The values are this session's: another session's short value rewriting this page's
    labels would corrupt the very evidence the reading exists to carry.
    """
    for value in scrub_values:
        text = text.replace(value, REDACTED_SECRET_PLACEHOLDER)
    return text[:limit]


def _summary_text(value: Any, scrub_values: Sequence[str] = ()) -> str:
    return _scrubbed_to(value.strip(), _PAGE_SUMMARY_TEXT_CAP, scrub_values) if isinstance(value, str) else ""


def _summary_field_name(field: dict[str, Any], scrub_values: Sequence[str] = ()) -> str:
    for key in ("label", "name", "placeholder", "id"):
        text = _summary_text(field.get(key), scrub_values)
        if text:
            return text
    return ""


def _summary_element_facts(control: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    candidates = control.get("selector_candidates")
    if isinstance(candidates, list):
        facts["selector_candidates"] = _model_visible_selector_candidates(candidates)
    identity = control.get("identity")
    if isinstance(identity, dict):
        facts["identity"] = identity
    return facts


def _summary_disclosure_control(control: dict[str, Any], scrub_values: Sequence[str] = ()) -> dict[str, Any] | None:
    if not isinstance(control.get("expanded"), bool):
        return None
    summary: dict[str, Any] = {"expanded": control["expanded"]}
    for key in ("text", "controls"):
        value = _summary_text(control.get(key), scrub_values)
        if value:
            summary[key] = value
    summary.update(_summary_element_facts(control))
    if "controls" in summary and isinstance(control.get("controlled_region_visible"), bool):
        summary["controlled_region_visible"] = control["controlled_region_visible"]
    if control.get("disabled") is True:
        summary["disabled"] = True
    if control.get("visible") is False:
        summary["visible"] = False
    return summary


def _summary_entry(text: str, control: dict[str, Any]) -> dict[str, Any]:
    return {"text": text, **_summary_element_facts(control)}


def _build_scout_page_summary(evidence: dict[str, Any], scrub_values: Sequence[str] = ()) -> dict[str, Any]:
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
                    _summary_entry(name, field)
                    for field, name in (
                        (field, _summary_field_name(field, scrub_values)) for field in fields[:_PAGE_SUMMARY_MAX_FIELDS]
                    )
                    if name
                ],
                "submit_controls": [
                    _summary_entry(text, control)
                    for control, text in (
                        (control, _summary_text(control.get("text") or control.get("value"), scrub_values))
                        for control in submits[:_PAGE_SUMMARY_MAX_SUBMITS]
                    )
                    if text
                ],
            }
        )
    nav_targets = [target for target in evidence.get("navigation_targets") or [] if isinstance(target, dict)]
    dismiss_entries: list[dict[str, Any]] = []
    for overlay in evidence.get("modal_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        for control in overlay.get("dismiss_controls") or []:
            if len(dismiss_entries) >= _PAGE_SUMMARY_MAX_DISMISS_TEXTS:
                break
            if not isinstance(control, dict):
                continue
            text = _summary_text(control.get("text") or control.get("aria_label") or control.get("title"), scrub_values)
            if text:
                dismiss_entries.append(_summary_entry(text, control))
    interaction_blocking_layers: list[dict[str, Any]] = []
    for obstruction in evidence.get("page_obstructions") or []:
        if not isinstance(obstruction, dict) or obstruction.get("kind") != "interaction_blocking_layer":
            continue
        visible_controls = [
            _summary_entry(text, control)
            for control, text in (
                (
                    control,
                    _summary_text(
                        control.get("text") or control.get("aria_label") or control.get("title"), scrub_values
                    ),
                )
                for control in obstruction.get("visible_controls") or []
                if isinstance(control, dict)
            )
            if text
        ]
        if not visible_controls:
            continue
        layer: dict[str, Any] = {
            **_summary_element_facts(obstruction),
            "intercepts_outside_control": obstruction.get("intercepts_outside_control") is True,
            "visible_controls": visible_controls,
        }
        controls_omitted = obstruction.get("visible_controls_omitted")
        if isinstance(controls_omitted, int) and not isinstance(controls_omitted, bool) and controls_omitted > 0:
            layer["visible_controls_omitted"] = controls_omitted
        interaction_blocking_layers.append(layer)
    challenge_state = evidence.get("challenge_state")
    challenge_detected = bool(evidence.get("challenge_controls")) or (
        isinstance(challenge_state, dict) and challenge_state.get("detected") is True
    )
    disclosure_controls: list[dict[str, Any]] = []
    seen_disclosures: set[tuple[str, str]] = set()
    controls_for_summary: list[dict[str, Any]] = []
    if settings.COPILOT_CLICKABLE_CONTROLS_EVIDENCE_ENABLED:
        controls_for_summary.extend(evidence.get("clickable_controls") or [])
        controls_for_summary.extend(
            control
            for form in evidence.get("forms") or []
            if isinstance(form, dict)
            for control in form.get("submit_controls") or []
            if isinstance(control, dict) and isinstance(control.get("expanded"), bool)
        )
    for control in controls_for_summary:
        if not isinstance(control, dict) or len(disclosure_controls) >= _PAGE_SUMMARY_MAX_DISCLOSURE_CONTROLS:
            continue
        summary = _summary_disclosure_control(control, scrub_values)
        if summary is None:
            continue
        candidate_identity = json.dumps(summary.get("selector_candidates") or [], sort_keys=True)
        identity = (candidate_identity, str(summary.get("controls") or ""))
        if identity in seen_disclosures:
            continue
        seen_disclosures.add(identity)
        disclosure_controls.append(summary)
    page_summary: dict[str, Any] = {
        "page_title": _summary_text(evidence.get("page_title"), scrub_values),
        "forms": forms_summary,
        "navigation_target_count": len(nav_targets),
        "navigation_targets_truncated": evidence.get("navigation_targets_truncated") is True,
        "navigation_targets": [
            _summary_entry(text, target)
            for target, text in (
                (target, _summary_text(target.get("text"), scrub_values))
                for target in nav_targets[:_PAGE_SUMMARY_MAX_NAV_TEXTS]
            )
            if text
        ],
        "result_container_count": len(evidence.get("result_containers") or []),
        "disclosure_controls": disclosure_controls,
        "challenge_detected": challenge_detected,
        "modal_dismiss_controls": dismiss_entries,
        "interaction_blocking_layers": interaction_blocking_layers,
    }
    size_compaction = evidence.get("size_compaction")
    if isinstance(size_compaction, dict):
        page_summary["size_compaction"] = size_compaction
    return page_summary


def _drop_scout_page_summary_selectors(summary: dict[str, Any]) -> bool:
    """Collapse every control entry to the bare text it carries, and report whether anything changed."""
    dropped = False
    groups: list[Any] = [summary.get("navigation_targets"), summary.get("modal_dismiss_controls")]
    for layer in summary.get("interaction_blocking_layers") or []:
        if isinstance(layer, dict):
            for key in ("selector_candidates", "identity"):
                if key in layer:
                    layer.pop(key)
                    dropped = True
            groups.append(layer.get("visible_controls"))
    for form in summary.get("forms") or []:
        if isinstance(form, dict):
            groups.extend([form.get("fields"), form.get("submit_controls")])
    for entries in groups:
        if not isinstance(entries, list):
            continue
        collapsed = [str(entry.get("text") or "") if isinstance(entry, dict) else entry for entry in entries]
        if collapsed != entries:
            entries[:] = collapsed
            dropped = True
    return dropped


def _shed_scout_page_summary_section(summary: dict[str, Any]) -> str | None:
    """Drop one summary section, in fixed priority order; None when nothing is left to shed. Selectors
    go first so the enrichment can never cost a control the summary carried without it."""
    if _drop_scout_page_summary_selectors(summary):
        return "control_selectors"
    if summary.get("navigation_targets"):
        summary["navigation_targets"] = []
        return "navigation_targets"
    forms = [form for form in summary.get("forms") or [] if isinstance(form, dict)]
    for form in forms[1:]:
        if form.get("fields"):
            form["fields"] = []
            return "later_form_fields"
    if summary.get("modal_dismiss_controls"):
        summary["modal_dismiss_controls"] = []
        return "modal_dismiss_controls"
    if summary.get("interaction_blocking_layers"):
        summary["interaction_blocking_layers"] = []
        return "interaction_blocking_layers"
    for form in forms[1:]:
        if form.get("submit_controls"):
            form["submit_controls"] = []
            return "later_form_submit_controls"
    if forms and forms[0].get("fields"):
        fields = forms[0]["fields"]
        forms[0]["fields"] = fields[: len(fields) // 2] if len(fields) > 2 else []
        return "first_form_fields"
    if forms and forms[0].get("submit_controls"):
        forms[0]["submit_controls"] = []
        return "first_form_submit_controls"
    if len(forms) > 1:
        summary["forms"] = forms[:1]
        return "later_forms"
    if summary.get("disclosure_controls"):
        summary["disclosure_controls"] = []
        return "disclosure_controls"
    return None


def _redact_summary_node(ctx: AgentContext, node: Any) -> Any:
    """Redact the summary's string leaves rather than the whole structure.

    The scrubber rewrites dict keys and integers too, which erases the `text` key the shed ladder
    reads and turns counts into strings. A selector the scrubber rewrote is dropped rather than
    kept: a partially rewritten selector still parses and can match a different element.
    """
    if isinstance(node, dict):
        redacted_dict: dict[str, Any] = {}
        for key, value in node.items():
            redacted_value = _redact_summary_node(ctx, value)
            if key == "selector" and isinstance(value, str) and redacted_value != value:
                continue
            redacted_dict[key] = redacted_value
        return redacted_dict
    if isinstance(node, list):
        return [_redact_summary_node(ctx, item) for item in node]
    if isinstance(node, str):
        # A page echoes what was typed into it, so the session's registered values come out before
        # the reading is stored: the generic pass after it matches shapes, not arbitrary secrets.
        redacted_leaf = _redact_codeblock_value(ctx, scrub_secrets_from_text(ctx, node))
        return redacted_leaf if isinstance(redacted_leaf, str) else ""
    return node


def _attach_scout_page_summary(ctx: AgentContext, result: dict[str, Any], page_evidence: dict[str, Any]) -> None:
    """Attach a compact page summary at result["data"]["page"], keeping the whole
    serialized result under the scout result budget by shedding sections —
    never by slicing the serialized JSON.

    The summary carries selectors, which can embed a workflow parameter's value, so it takes the same
    redaction the interaction identity does. That scrubber knows workflow-parameter values only, and
    those are populated on self-heal turns -- it is not a general secret filter.
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return
    try:
        summary = _redact_summary_node(ctx, _build_scout_page_summary(page_evidence))
        if not isinstance(summary, dict):
            return
        data["page"] = summary
        shed: list[str] = []
        while len(json.dumps(result)) > _SCOUT_RESULT_CHAR_CAP:
            section = _shed_scout_page_summary_section(summary)
            if section is None:
                # A summary that vanished with no trace reads as a page with nothing on it, so the
                # names of everything dropped stay behind even when the summary itself cannot.
                data["page"] = {"shed": [*shed, "page_summary"]}
                return
            shed.append(section)
            summary["shed"] = shed
    except Exception:
        data.pop("page", None)
        LOG.warning("copilot_scout_act_observe_summary_failed")


def _signed_out_page_facts(evidence: dict[str, Any], scrub_values: Sequence[str] = ()) -> dict[str, Any]:
    """Add the page's own clickable controls and visible text to the scout summary. The summary's
    fields come from `<form>` elements only, so a sign-in card built without one reaches the reader
    through these two keys or not at all.
    """
    facts = _build_scout_page_summary(evidence, scrub_values)
    controls: list[dict[str, Any]] = []
    for control in evidence.get("clickable_controls") or []:
        if len(controls) >= _SIGNED_OUT_MAX_CONTROLS:
            break
        if not isinstance(control, dict):
            continue
        text = _summary_text(control.get("text") or control.get("aria_label") or control.get("title"), scrub_values)
        if not text:
            continue
        entry = _summary_entry(text, control)
        candidates = entry.get("selector_candidates")
        if isinstance(candidates, list):
            # A selector carrying a registered value is dropped later anyway, so it must not take one
            # of the slots from a candidate that would have survived.
            usable = [
                candidate
                for candidate in candidates
                if not (
                    isinstance(candidate, dict)
                    and isinstance(candidate.get("selector"), str)
                    and any(value in candidate["selector"] for value in scrub_values)
                )
            ]
            entry["selector_candidates"] = usable[:_SIGNED_OUT_MAX_SELECTOR_CANDIDATES]
        controls.append(entry)
    facts["controls"] = controls
    excerpt = evidence.get("visible_text_excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        facts["visible_text"] = _scrubbed_to(excerpt.strip(), _SIGNED_OUT_TEXT_EXCERPT_CAP, scrub_values)
    return facts


def _bounded_signed_out_page_facts(facts: dict[str, Any]) -> dict[str, Any]:
    while len(json.dumps(facts)) > SIGNED_OUT_PAGE_SUMMARY_CHAR_CAP:
        controls = facts.get("controls")
        if isinstance(controls, list) and len(controls) > 1:
            controls.pop()
            continue
        if facts.pop("visible_text", None) is not None:
            continue
        if isinstance(controls, list) and controls:
            controls.pop()
            continue
        if _shed_scout_page_summary_section(facts) is not None:
            continue
        return {"page_title": facts.get("page_title", ""), "shed": ["signed_out_page_facts"]}
    return facts


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _canonical_origin(url: str) -> tuple[str, str, int | None] | None:
    """The origin as a browser reports it back: scheme, lowercased host, and a port only when it is
    not the scheme's default. `https://Portal.Test:443/x` and `https://portal.test/x` are one origin."""
    origin = safe_page_origin(url)
    if not origin:
        return None
    parts = urlsplit(origin.rstrip("/"))
    if not parts.hostname:
        return None
    port = parts.port if parts.port != _DEFAULT_PORTS.get(parts.scheme) else None
    return parts.scheme, parts.hostname.lower(), port


def _exact_origin(url: str) -> str | None:
    """The origin form the deployment's egress guard names, without a path, spelt the way the guard
    will recognise it."""
    canonical = _canonical_origin(url)
    if canonical is None:
        return None
    scheme, host, port = canonical
    return f"{scheme}://{host}:{port}" if port is not None else f"{scheme}://{host}"


@asynccontextmanager
async def _signed_out_navigation_guard(
    browser_context: BrowserContext, expected_origin: str | None
) -> AsyncIterator[bool]:
    """Hold the anonymous probe to the origin it was asked to read, so a redirect meant for
    signed-out visitors cannot make it fetch somewhere else.

    Yields whether enforcement is actually active. The deployment's guard decides which origins it
    will cover, and a probe it will not cover is not worth the request.
    """
    if expected_origin is None:
        yield False
        return
    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(
                app.AGENT_FUNCTION.copilot_candidate_network_guard(browser_context, expected_origin=expected_origin)
            )
        except Exception:
            # Fail closed on any refusal to enter, not on a list of the ways it has refused so far:
            # an unenforced probe is the thing this exists to prevent.
            LOG.info("copilot_signed_out_navigation_guard_unavailable", exc_info=True)
            yield False
            return
        yield True


async def capture_signed_out_page_observation(
    browser_state: BrowserState,
    *,
    url: str,
    organization_id: str,
    scrub_values: Sequence[str] = (),
) -> SignedOutPageObservation | None:
    """Read `url` through a context that carries no cookies or storage, and summarize what it shows.

    Requesting a location is a heavier claim than reporting one, and this is the only place the
    copilot asks for a URL a page handed it. A location it may not request exactly as recorded — a
    callback carrying a one-time query, a masked or credential-bearing URL — is not requested at
    all, rather than requested in a reduced form that addresses a different page.
    """
    requestable, withheld = screened_recorded_url(url)
    if requestable is None or withheld is not None:
        LOG.info("copilot_signed_out_page_observation_skipped", reason=withheld)
        return None
    async with service_worker_blocked_context(browser_state, organization_id=organization_id) as browser_context:
        page = await browser_state.get_working_page()
        if page is None:
            return None
        async with _signed_out_navigation_guard(browser_context, _exact_origin(url)) as guarded:
            if not guarded:
                LOG.info("copilot_signed_out_page_observation_unenforceable")
                return None
            await page.goto(url, wait_until="domcontentloaded", timeout=_SIGNED_OUT_OBSERVATION_TIMEOUT_MS)
            raw = await page.evaluate(composition_structured_evidence_expression())
            reached_url = page.url
            # A sign-in card that arrives after DOMContentLoaded is not in the first read, and this
            # origin gets one attempt. One bounded re-read; the first read stands if the page has
            # moved on under it by then, which a redirect during the settle would do.
            await asyncio.sleep(_SIGNED_OUT_SETTLE_DELAY_SECONDS)
            try:
                raw = await page.evaluate(composition_structured_evidence_expression())
                reached_url = page.url
            except Exception:
                LOG.info("copilot_signed_out_page_observation_settled_read_failed", exc_info=True)
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        return None
    if _canonical_origin(reached_url) != _canonical_origin(url):
        # A reading from somewhere else is not a reading of this page.
        LOG.info("copilot_signed_out_page_observation_left_its_origin")
        return None
    evidence = parse_composition_structured(payload, inspected_url=url, current_url=reached_url)
    if evidence is None:
        return None
    screened_reached, reached_withheld = screened_recorded_url(reached_url)
    # A registered value can sit in a path, where the recorded-URL screen reads only generic shapes.
    return SignedOutPageObservation(
        requested_url=_scrubbed_to(url, BLOCK_FACT_URL_MAX_CHARS, scrub_values),
        # Where a signed-out visitor is sent may carry a query of its own, and the reading is worth
        # no less for naming only the route it reached.
        reached_url=_scrubbed_to(
            screened_reached if screened_reached is not None else f"(withheld: {reached_withheld})",
            BLOCK_FACT_URL_MAX_CHARS,
            scrub_values,
        ),
        page_summary=_bounded_signed_out_page_facts(_signed_out_page_facts(evidence, scrub_values)),
    )


async def record_signed_out_page_observation(ctx: AgentContext, url: str) -> None:
    """Keep one signed-out reading per origin the scout reached, so authoring sees what a cold run starts from."""
    origin = safe_page_origin(url)
    recorded = ctx.signed_out_page_observations
    attempted = ctx.signed_out_page_observation_attempts
    if not origin or len(recorded) >= _SIGNED_OUT_OBSERVATION_ORIGIN_LIMIT:
        return
    if origin in attempted or len(attempted) >= _SIGNED_OUT_OBSERVATION_ATTEMPT_LIMIT:
        return
    try:
        browser_state = await resolve_browser_state_for_context(ctx)
        if browser_state is None:
            # No browser to probe with, so nothing was tried: a later action in this turn may find one.
            return
        # An origin is spent once a probe can run, whether or not it yields a reading: one that times
        # out or finds no guard costs seconds, and every later action on the page would pay it again.
        attempted.append(origin)
        observation = await capture_signed_out_page_observation(
            browser_state,
            url=url,
            organization_id=ctx.organization_id,
            scrub_values=registered_scrub_values(ctx),
        )
    except Exception:
        LOG.warning("copilot_signed_out_page_observation_failed", exc_info=True)
        return
    if observation is None:
        return
    summary = _redact_summary_node(ctx, observation.page_summary)
    recorded.append(
        observation.model_copy(update={"page_summary": summary}) if isinstance(summary, dict) else observation
    )
    # The one line that says the lever engaged: every failure path above logs, and without this the
    # claim that the model saw the signed-out page cannot be checked against a run after the fact.
    LOG.info(
        "copilot_signed_out_page_observation_recorded",
        origin=origin,
        form_count=len(summary.get("forms") or []) if isinstance(summary, dict) else None,
        control_count=len(summary.get("controls") or []) if isinstance(summary, dict) else None,
        recorded_origins=len(recorded),
    )


def _page_evidence_names_obstruction(page_evidence: dict[str, Any] | None) -> bool:
    """True only when the evidence positively names a blocker a frame would have shown.

    A computed-style obstruction candidate does not count: it fires on ordinary textless overlays
    and would suppress the frame on exactly the pages a frame is needed for.
    """
    if not isinstance(page_evidence, dict):
        return False
    overlays = [overlay for overlay in page_evidence.get("modal_overlays") or [] if isinstance(overlay, dict)]
    if any(overlay.get("dismiss_controls") for overlay in overlays):
        return True
    if page_evidence.get("challenge_controls"):
        return True
    challenge_state = page_evidence.get("challenge_state")
    return isinstance(challenge_state, dict) and challenge_state.get("detected") is True


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
    browser_session_id = effective_browser_session_id(ctx)
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


_CHALLENGE_VENDOR_FRAME_URL = re.compile(CHALLENGE_VENDOR_SIGNATURE, re.IGNORECASE)


def _release_scout_challenge_listeners(ctx: AgentContext) -> None:
    for detach in ctx.pending_scout_challenge_detachers:
        try:
            detach()
        except Exception:
            LOG.debug("copilot_scout_challenge_listener_detach_failed", exc_info=True)
    ctx.pending_scout_challenge_detachers = []


# A managed widget preloads small and grows when it actually challenges, so "rendered" cannot mean
# "has a box": a 1x1 iframe measures 25 once default borders are counted. Anything at or under this
# much on-screen area is a placeholder rather than a challenge a person could answer.
# ponytail: one sentinel for every vendor — revisit if a vendor's real widget ships smaller than 16x16.
_CHALLENGE_FRAME_PLACEHOLDER_AREA = 256.0

# Layout area is not screen area: a widget preloaded at full size can sit off-viewport, inside a
# zero-size overflow:hidden ancestor, under a clip-path, behind visibility:hidden, or under an
# opacity:0 ancestor, and still report its whole box. checkVisibility answers the style half;
# IntersectionObserver answers the geometry half, ancestor clip rects included, so neither the
# viewport nor a clipping container has to be walked by hand.
_CHALLENGE_FRAME_ONSCREEN_AREA_JS = """
el => {
  if (el.checkVisibility && !el.checkVisibility({
    opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true,
  })) return 0;
  return new Promise(resolve => {
    let observer = null;
    let timer = null;
    const done = area => {
      if (observer) observer.disconnect();
      if (timer) clearTimeout(timer);
      resolve(area);
    };
    observer = new IntersectionObserver(entries => {
      const rect = entries[entries.length - 1].intersectionRect;
      done(rect.width * rect.height);
    });
    timer = setTimeout(() => done(null), 1000);
    observer.observe(el);
  });
}
"""


_ELEMENT_STYLE_VISIBLE_JS = """
el => !el.checkVisibility || el.checkVisibility({
  opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true,
})
"""


async def _frame_element_style_visible(frame: Frame) -> bool:
    try:
        element = await frame.frame_element()
        return bool(await element.evaluate(_ELEMENT_STYLE_VISIBLE_JS))
    except Exception:
        return False


async def _embedding_frames_style_visible(frame: Frame) -> bool:
    """Whether every iframe embedding this one, up to the top page, is visible by style.

    The observer's implicit root already clips geometry through each ancestor frame, but style is judged
    per document: an opacity:0 or visibility:hidden iframe further up still lets this frame report its
    full box. An ancestor that cannot be read counts as hidden.
    """
    ancestors: list[Frame] = []
    current = frame.parent_frame
    while current is not None and current.parent_frame is not None:
        ancestors.append(current)
        current = current.parent_frame
    return all(await asyncio.gather(*(_frame_element_style_visible(ancestor) for ancestor in ancestors)))


# The script's own timer cannot fire in a renderer that never yields, and the click pre-hook runs outside
# the MCP call timeout, so the deadline is held here.
_CHALLENGE_FRAME_PROBE_TIMEOUT_SECONDS = 2.0


async def _challenge_frame_rendered_area(frame: Frame) -> float | None:
    """On-screen area of the frame's own element, or None when it cannot be measured in time."""
    try:
        return await asyncio.wait_for(
            _measure_challenge_frame_area(frame), timeout=_CHALLENGE_FRAME_PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        return None


async def _measure_challenge_frame_area(frame: Frame) -> float | None:
    element = await frame.frame_element()
    area = await element.evaluate(_CHALLENGE_FRAME_ONSCREEN_AREA_JS)
    if area is None:
        return None
    if not await _embedding_frames_style_visible(frame):
        return 0.0
    return float(area)


async def _arm_scout_challenge_listener(ctx: AgentContext) -> None:
    """Arm a frame-navigation listener for the click about to dispatch.

    A widget already mounted before the click is not this click's effect, so only navigations
    inside the click's window count; main-frame navigations are left to ``url_changed`` and the
    page summary. A preloaded widget the click merely reveals never navigates, so its rendered area
    is measured here and compared after the settle."""
    _release_scout_challenge_listeners(ctx)
    ctx.pending_scout_challenge_frames = []
    ctx.pending_scout_challenge_prior_frames = []
    ctx.pending_scout_challenge_armed_at = None
    try:
        browser_state = await resolve_browser_state_for_context(ctx)
        if browser_state is None:
            return
        page = await browser_state.get_or_create_page()
        # A widget already showing a vendor URL re-navigates to refresh its token, and that fires
        # framenavigated on the same frame — which is the widget's own upkeep, not this click.
        already_challenged = [
            frame
            for frame in page.frames
            if frame.parent_frame is not None and _CHALLENGE_VENDOR_FRAME_URL.search(frame.url or "")
        ]

        def _capture(frame: Frame) -> None:
            if frame.parent_frame is None:
                return
            baseline = (seen for seen, _area in ctx.pending_scout_challenge_prior_frames)
            if any(frame is seen for seen in (*already_challenged, *baseline, *ctx.pending_scout_challenge_frames)):
                return
            if _CHALLENGE_VENDOR_FRAME_URL.search(frame.url or ""):
                ctx.pending_scout_challenge_frames.append(frame)

        # Listening before the measurement below yields, so a frame that mounts while it runs is still seen.
        page.on("framenavigated", _capture)
        ctx.pending_scout_challenge_detachers.append(lambda: page.remove_listener("framenavigated", _capture))
        ctx.pending_scout_challenge_armed_at = time.monotonic()
        # Measured together: each reading stops itself after its observer timeout, so a page with several
        # vendor frames costs one bound before the click dispatches rather than one per frame.
        prior_areas = await asyncio.gather(*(_challenge_frame_rendered_area(frame) for frame in already_challenged))
        ctx.pending_scout_challenge_prior_frames = list(zip(already_challenged, prior_areas, strict=True))
    except Exception:
        LOG.warning("copilot_scout_challenge_listener_failed", exc_info=True)


# Bounded so a page that stalls the probe cannot spend the turn budget one click at a time.
_RENDER_PROBE_TIMEOUT_MS = 5000.0


def _attach_observed_click_effect(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    effect: str,
    challenge_vendor: str | None = None,
) -> None:
    """Attach a browser-observed click effect without choosing a future action or locator.

    The records this click just wrote are updated directly. Searching for them by selector would miss
    a click whose locator the scrub removed, and could land the effect on an older click that used
    the same selector.
    """
    data = result.get("data")
    if not isinstance(data, dict):
        return
    result_effects = data.setdefault("observed_effects", {})
    if isinstance(result_effects, dict):
        result_effects[effect] = True
    if challenge_vendor is not None:
        data["challenge_vendor"] = challenge_vendor
    for interaction in ctx.pending_scout_click_records:
        effects = dict(interaction.get("observed_effects") or {})
        effects[effect] = True
        interaction["observed_effects"] = effects
        if challenge_vendor is not None:
            interaction["challenge_vendor"] = challenge_vendor


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
        _attach_observed_click_effect(ctx, result, effect="rendered_document_opened")
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
        _attach_observed_click_effect(ctx, result, effect="download_started")
    except Exception:
        LOG.warning("copilot_observed_download_target_attach_failed", exc_info=True)


async def _close_scout_challenge_baseline(ctx: AgentContext) -> None:
    """Close the pre-click window at the dispatch boundary.

    Frames that navigated since the listener went on arrived before the click, so they join the baseline:
    one the click later reveals is still credited through the reveal check, a visible one is not.
    """
    if ctx.pending_scout_challenge_armed_at is None:
        return
    arrivals = list(ctx.pending_scout_challenge_frames)
    ctx.pending_scout_challenge_frames.clear()
    areas = await asyncio.gather(*(_challenge_frame_rendered_area(frame) for frame in arrivals))
    ctx.pending_scout_challenge_prior_frames.extend(zip(arrivals, areas, strict=True))
    # Frames that navigated during that measurement are baselined unmeasured, with no await before dispatch:
    # an unmeasured baseline frame is never credited, as new or as revealed.
    ctx.pending_scout_challenge_prior_frames.extend((frame, None) for frame in ctx.pending_scout_challenge_frames)
    ctx.pending_scout_challenge_frames.clear()


def _start_scout_challenge_settle(ctx: AgentContext) -> None:
    """Start the settle window when the click tool returns.

    The browser clicks somewhere inside the tool call, after resolving and waiting for its target, which
    can outlast the window; the return is the first moment known to follow the click.
    """
    if ctx.pending_scout_challenge_armed_at is not None:
        ctx.pending_scout_challenge_armed_at = time.monotonic()


async def _on_screen_challenge_vendor(ctx: AgentContext) -> str | None:
    """Vendor of the first challenge frame the click put on screen, whether it mounted one or revealed a placeholder.

    A widget often arrives with hidden helper frames beside it, so every candidate is measured rather
    than the first to navigate. A hidden vendor frame is still a marker the solve ladder acts on, and
    crediting one would send a solve through the whole ladder against nothing a person could answer.
    """
    # A preload is only revealable if it measured as a placeholder before the click: without that
    # reading there is no growth to observe, and an already-challenging widget would be credited to
    # whatever click happened to follow it.
    revealable = [
        frame
        for frame, prior_area in ctx.pending_scout_challenge_prior_frames
        if prior_area is not None and prior_area <= _CHALLENGE_FRAME_PLACEHOLDER_AREA
    ]
    return await rendered_challenge_vendor([*ctx.pending_scout_challenge_frames, *revealable])


async def rendered_challenge_vendor(frames: list[Frame]) -> str | None:
    """Vendor of the first of these frames that is a challenge frame rendered on screen above placeholder size."""
    # A captured frame can navigate away before this runs, so each is matched on its current URL.
    candidates = [
        (frame, match) for frame in frames if (match := _CHALLENGE_VENDOR_FRAME_URL.search(frame.url or "")) is not None
    ]
    areas = await asyncio.gather(*(_challenge_frame_rendered_area(frame) for frame, _match in candidates))
    for (_frame, match), area in zip(candidates, areas, strict=True):
        if area is not None and area > _CHALLENGE_FRAME_PLACEHOLDER_AREA:
            # The matched text is one of the signature's own literals, so the vendor is named from a closed
            # vocabulary. A hostname would carry whatever the page put in it — a tenant slug or a secret, in
            # any case or encoding — and no scrub can enumerate every spelling of that.
            return match.group(0).casefold()
    return None


async def _maybe_attach_observed_challenge(ctx: AgentContext, result: dict[str, Any], *, url: str) -> None:
    """Report when the scout's click raised an anti-bot challenge.

    Needs no locator: a coordinate click has none, and still raises challenges. The effect goes on the
    tool result either way, and onto whatever records this click wrote, which for a click with no
    locator is none. A failed click may carry no data at all, and gets some only when a challenge is found.
    """
    if result.get("data") is not None and not isinstance(result.get("data"), dict):
        return
    armed_at = ctx.pending_scout_challenge_armed_at
    if armed_at is None:
        return
    vendor = await _on_screen_challenge_vendor(ctx)
    if vendor is None:
        # The window started when the click returned, and the observation between then and now usually
        # outlasts it by itself. Wait only for what is left of it, so a widget
        # that mounts or grows a beat late still lands without adding a delay to every click.
        owed = settings.COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS - (time.monotonic() - armed_at)
        if owed > 0:
            await asyncio.sleep(owed)
        vendor = await _on_screen_challenge_vendor(ctx)
    if vendor is None:
        return
    try:
        LOG.info("copilot_observed_challenge_signal", vendor=vendor, page_origin=safe_page_origin(url))
        result.setdefault("data", {})
        _attach_observed_click_effect(ctx, result, effect="challenge_raised", challenge_vendor=vendor)
    except Exception:
        LOG.warning("copilot_observed_challenge_attach_failed", exc_info=True)


async def attach_navigation_challenge_vendor(ctx: AgentContext, result: dict[str, Any]) -> None:
    """Name the vendor of a challenge frame the navigated page shows on screen."""
    try:
        browser_state = await resolve_browser_state_for_context(ctx)
        if browser_state is None:
            return
        page = await browser_state.get_or_create_page()
        vendor = await rendered_challenge_vendor([frame for frame in page.frames if frame.parent_frame is not None])
        vendor_frame_present = any(
            frame.parent_frame is not None and _CHALLENGE_VENDOR_FRAME_URL.search(frame.url or "")
            for frame in page.frames
        )
        if vendor is None and (ctx.pending_scout_challenge_frames or vendor_frame_present):
            # Only a page that already has a vendor frame waits for it to mount or grow, so a page without one
            # pays no delay.
            settle_started = ctx.pending_scout_challenge_armed_at or time.monotonic()
            owed = settings.COPILOT_SCOUT_ACT_OBSERVE_RECAPTURE_DELAY_SECONDS - (time.monotonic() - settle_started)
            if owed > 0:
                await asyncio.sleep(owed)
            vendor = await rendered_challenge_vendor([frame for frame in page.frames if frame.parent_frame is not None])
    except Exception:
        LOG.warning("copilot_navigation_challenge_vendor_failed", exc_info=True)
        return
    if vendor is not None:
        result["challenge_vendor"] = vendor


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
    _attach_scout_page_summary(ctx, result, page_evidence)


def _mark_post_run_page_observed(
    ctx: AgentContext,
    *,
    source_tool: str,
    url: str,
    page_evidence: dict[str, Any] | None = None,
    source_browser_session_id: str | None,
) -> None:
    run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    if not isinstance(run_id, str) or not run_id:
        return
    if post_run_evidence_source_refused(
        run_id=run_id,
        source_browser_session_id=source_browser_session_id,
        run_browser_session_id=ctx.last_run_blocks_browser_session_id,
    ):
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
        bound_evidence = stamp_page_evidence_provenance(
            {**page_evidence, "current_url": url},
            source_browser_session_id=source_browser_session_id,
            run_id=run_id,
            run_browser_session_id=ctx.last_run_blocks_browser_session_id,
        )
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
