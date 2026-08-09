from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Literal, cast
from urllib.parse import urlparse

import structlog
from playwright.async_api import Page, Response

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.copilot.build_test_outcome import (
    RecordedBuildTestOutcome,
    bind_post_run_page_path_failure,
    record_build_test_outcome,
    recorded_outcome_from_loaded_result_evidence,
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
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _is_positional_selector,
    dynamic_row_evidence_fingerprint,
    dynamic_row_period_matches_match_selected_row,
    normalized_scout_selector,
    validated_dynamic_row_period_matches,
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
    scout_dynamic_row_evidence_expression as _scout_dynamic_row_evidence_expression,
)
from skyvern.forge.sdk.copilot.composition_browser_expressions import (
    selector_match_count_expression as _selector_match_count_expression,
)
from skyvern.forge.sdk.copilot.composition_evidence import (
    SCOUT_INTERACTION_EVIDENCE_TOOL,
    has_actionable_steer_content,
    has_bounded_page_schema,
    has_witnessed_value_content,
    packet_describes_a_clearable_overlay,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import FillCarry
from skyvern.forge.sdk.copilot.enforcement import (
    mint_scout_observation_contract_for_ctx,
    record_reached_terminal_action_observation,
    record_scouted_output_coverage,
    register_no_progress_interaction_click,
    reset_no_progress_interaction_count,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    DOWNLOAD_KIND_OBSERVED,
    DOWNLOAD_KIND_OBSERVED_RENDER,
    ReachedDownloadTarget,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    derive_from_navigation_targets as _derive_reached_download_from_nav_targets,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    derive_from_observed_download,
    derive_from_observed_render,
)
from skyvern.forge.sdk.copilot.reached_download_target import guidance_for as _reached_download_guidance_for
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion
from skyvern.forge.sdk.copilot.result_evidence import (
    LoadedResultCompositionEvidence,
    loaded_result_composition_evidence_from_page,
    loaded_result_composition_target_summary,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    PendingBrowserInteractionObservation,
    ScoutedDynamicRowEvidence,
    ScoutedInteraction,
    resolve_browser_state_for_context,
)
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.todo_list import (
    _inapplicable_criterion_ids,
    _minted_criteria,
    _satisfied_output_paths,
    unmet_action_deliverable_criteria,
)
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
from .banned_blocks import _copilot_block_authoring_policy

LOG = structlog.get_logger()

# Emission budget for scout/evaluate tool results: bounds how much live page content
# a result may carry into the authoring context, so it must not follow the transcript
# recent-window cap.
_SCOUT_RESULT_CHAR_CAP = 2000
# Reconnaissance evaluates keep the raw page payload by design, so they get a larger
# budget than the steer results; this is the hard ceiling on that channel, not a target.
_SCOUT_RECON_RESULT_CHAR_CAP = 20_000

_FILL_CARRY_RETRYABLE_VALIDATION_FAILURES = frozenset({"page_mismatch", "selector_absent_from_page_evidence"})


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
    reset_no_progress_interaction_count(ctx)
    return True


_MAX_SCOUTED_INTERACTIONS = 60
_FILL_CARRY_SELECTOR_COUNT_TIMEOUT_SECONDS = 2.0


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
    if not source_url or ctx.fill_carry_rebound_done or not ctx.prior_fill_carry:
        return
    page_evidence = await _scout_act_observe_page_evidence(ctx, url=source_url)
    if page_evidence is None or not has_bounded_page_schema(page_evidence):
        return
    await rebind_prior_fill_carry_from_page_evidence(ctx, page_evidence=page_evidence, url=source_url)


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
    server = ctx.discovery_mcp_server
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
    server = ctx.discovery_mcp_server
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
    """Stash whether a click/select selector is ambiguous (>1 match) on its source page, read before the
    action dispatches so the count reflects the source rather than a post-navigation landing; a captured
    (role, name) re-anchor is kept only when get_by_role(role, name, exact=True) resolves uniquely, so a
    name-degenerate selector fails closed to the scout-the-step drop instead of a strict-mode failure."""
    ctx.pending_scout_ambiguous = None
    ctx.pending_scout_reanchor = None
    selector = _selector_text(selector)
    if not selector:
        return
    count = await _selector_live_match_count(ctx, selector)
    if count is None or count <= 1:
        return
    ctx.pending_scout_ambiguous = (selector, True)
    captured = await _capture_accessible_role_name(
        ctx, selector, timeout_seconds=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS
    )
    if captured is None:
        return
    role, name = captured
    if not role or not name:
        return
    if await _role_name_match_count(ctx, role, name) == 1:
        ctx.pending_scout_reanchor = (selector, role, name)


async def _capture_scout_dynamic_row(ctx: AgentContext, selector: str | None) -> None:
    ctx.pending_scout_dynamic_row = None
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return
    selector = _selector_text(selector)
    source_url = (ctx.pending_scout_source_url or "").strip()
    server = getattr(ctx, "discovery_mcp_server", None)
    if not selector or not _is_positional_selector(selector) or not source_url or server is None:
        return
    try:
        result = await asyncio.wait_for(
            server.call_internal_tool(
                "skyvern_evaluate",
                {"expression": _scout_dynamic_row_evidence_expression(selector)},
            ),
            timeout=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS,
        )
    except Exception:
        return
    if not isinstance(result, dict) or not result.get("ok"):
        return
    value = (result.get("data") or {}).get("result")
    if not isinstance(value, dict):
        return
    target_selector = value.get("target_selector")
    row_selector = value.get("row_selector")
    row_text = value.get("row_text")
    row_selector_count = value.get("row_selector_count")
    row_text_match_count = value.get("row_text_match_count")
    period_matches = value.get("period_matches")
    validated_period_matches = (
        validated_dynamic_row_period_matches(period_matches, row_selector_count)
        if isinstance(row_selector_count, int) and not isinstance(row_selector_count, bool)
        else None
    )
    selected_index = value.get("selected_index")
    if (
        target_selector != selector
        or not isinstance(row_selector, str)
        or not row_selector.strip()
        or not isinstance(row_text, str)
        or not row_text.strip()
        or isinstance(row_selector_count, bool)
        or not isinstance(row_selector_count, int)
        or row_selector_count < 2
        or row_selector_count > 100
        or isinstance(row_text_match_count, bool)
        or not isinstance(row_text_match_count, int)
        or row_text_match_count < 1
        or validated_period_matches is None
        or not dynamic_row_period_matches_match_selected_row(row_text.strip(), validated_period_matches)
        or isinstance(selected_index, bool)
        or not isinstance(selected_index, int)
        or selected_index < 0
        or selected_index >= row_selector_count
    ):
        return
    ctx.pending_scout_dynamic_row = ScoutedDynamicRowEvidence(
        source_url=source_url,
        target_selector=selector,
        row_selector=row_selector.strip(),
        row_text=row_text.strip(),
        row_selector_count=row_selector_count,
        row_text_match_count=row_text_match_count,
        period_matches=validated_period_matches,
        selected_index=selected_index,
        evidence_fingerprint=dynamic_row_evidence_fingerprint(
            source_url=source_url,
            target_selector=selector,
            row_selector=row_selector.strip(),
            row_text=row_text.strip(),
            row_selector_count=row_selector_count,
            row_text_match_count=row_text_match_count,
            period_matches=validated_period_matches,
            selected_index=selected_index,
        ),
    )


def _prenav_dynamic_row_for_selector(
    pending: ScoutedDynamicRowEvidence | None,
    selector: str,
    source_url: str | None,
) -> ScoutedDynamicRowEvidence | None:
    if pending is None:
        return None
    if pending["target_selector"] != _selector_text(selector):
        return None
    if pending["source_url"] != (source_url or "").strip():
        return None
    return pending


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


def _record_scouted_interaction(
    ctx: AgentContext,
    *,
    tool_name: str,
    selector: str | None = None,
    source_url: str | None = None,
    value: str = "",
    typed_value: str = "",
    raw_typed_value: str = "",
    key: str = "",
    typed_length: int = 0,
    role: str = "",
    accessible_name: str = "",
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
    dynamic_row_evidence: ScoutedDynamicRowEvidence | None = None,
) -> None:
    selector = _selector_text(selector)
    # press_key may be page-level, so it is recorded by key even with no selector; other tools require one.
    if tool_name != "press_key" and not selector:
        LOG.info(
            "copilot_scout_capture_loss",
            tool_name=tool_name,
            reason="unresolvable_selector",
            url=(source_url or "").strip() or None,
        )
        return
    _reset_evaluate_tracker(ctx)
    artifact: ScoutedInteraction = {"tool_name": tool_name}
    if selector:
        artifact["selector"] = selector
    if source_url and source_url.strip():
        artifact["source_url"] = source_url.strip()
    if value:
        artifact["value"] = value
    if typed_value:
        artifact["typed_value"] = typed_value
    if raw_typed_value:
        artifact["raw_typed_value"] = raw_typed_value
    if key:
        artifact["key"] = key
    if typed_length:
        artifact["typed_length"] = typed_length
    if role:
        artifact["role"] = role
    if accessible_name:
        artifact["accessible_name"] = accessible_name
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
    if dynamic_row_evidence is not None:
        artifact["dynamic_row_evidence"] = dynamic_row_evidence
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

    trajectory = list(ctx.scout_trajectory)
    trajectory_artifact = cast(ScoutedInteraction, artifact.copy())
    trajectory_artifact["trajectory_index"] = _next_trajectory_index(trajectory)
    trajectory.append(trajectory_artifact)
    ctx.scout_trajectory = _capped_with_eviction_accounting(trajectory, collection="scout_trajectory")

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
    """
    trajectory = list(ctx.scout_trajectory)
    if not trajectory:
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


def _page_evidence_with_inputs_as_fields(page_evidence: dict[str, Any]) -> dict[str, Any]:
    inputs = page_evidence.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return page_evidence
    fields: list[dict[str, Any]] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        field = dict(item)
        selector = field.get("selector")
        if isinstance(selector, str):
            field["selector"] = normalized_scout_selector(selector)
        fields.append(field)
    if not fields:
        return page_evidence
    forms = page_evidence.get("forms")
    normalized = dict(page_evidence)
    normalized["forms"] = [*(forms if isinstance(forms, list) else []), {"fields": fields}]
    return normalized


async def _fill_carry_validation_failure(
    ctx: AgentContext,
    carry: FillCarry,
    *,
    page_evidence: dict[str, Any],
    url: str,
) -> str | None:
    evidence_url = str(page_evidence.get("current_url") or page_evidence.get("inspected_url") or url).strip()
    if not evidence_url or not _same_page_ignoring_fragment(carry.source_url, evidence_url):
        return "page_mismatch"
    count = await _selector_live_match_count(
        ctx, carry.selector, timeout_seconds=_FILL_CARRY_SELECTOR_COUNT_TIMEOUT_SECONDS
    )
    if count != 1:
        return "selector_count_mismatch"
    selector_in_page_evidence = _page_evidence_has_selector(
        _page_evidence_with_inputs_as_fields(page_evidence), carry.selector
    )
    if carry.role and carry.accessible_name:
        role, accessible_name = await _resolve_scout_role_name(ctx, carry.selector)
        if role != carry.role or accessible_name != carry.accessible_name:
            return "role_name_mismatch"
    elif not selector_in_page_evidence:
        return "selector_absent_from_page_evidence"
    return None


def _fill_carry_to_interaction(carry: FillCarry, trajectory_index: int) -> ScoutedInteraction:
    interaction: ScoutedInteraction = {
        "tool_name": carry.tool_name,
        "selector": carry.selector,
        "source_url": carry.source_url,
        "trajectory_index": trajectory_index,
        "carried": True,
    }
    if carry.role:
        interaction["role"] = carry.role
    if carry.accessible_name:
        interaction["accessible_name"] = carry.accessible_name
    if carry.typed_length:
        interaction["typed_length"] = carry.typed_length
    if carry.tool_name == "type_text":
        if carry.typed_value:
            interaction["typed_value"] = carry.typed_value
        if carry.control_readonly is not None:
            interaction["control_readonly"] = carry.control_readonly
        if carry.control_disabled is not None:
            interaction["control_disabled"] = carry.control_disabled
        if carry.control_value_satisfied is not None:
            interaction["control_value_satisfied"] = carry.control_value_satisfied
    elif carry.tool_name == "select_option" and carry.value:
        interaction["value"] = carry.value
    elif carry.tool_name == "fill_credential_field":
        interaction["credential_id"] = carry.credential_id
        interaction["credential_field"] = carry.credential_field
    return interaction


async def _maybe_rebind_prior_fill_carry(
    ctx: AgentContext,
    *,
    page_evidence: dict[str, Any],
    url: str,
) -> None:
    if ctx.fill_carry_rebound_done:
        return
    prior = []
    for raw in ctx.prior_fill_carry:
        try:
            prior.append(FillCarry.model_validate(raw))
        except Exception:
            continue
    if not prior:
        ctx.fill_carry_rebound_done = True
        return
    # Inventory is credential metadata, not page state: rehydrate it even when page
    # validation below drops the carried fills themselves.
    for carry in prior:
        if carry.tool_name == "fill_credential_field" and carry.credential_id and carry.available_fields:
            ctx.scouted_credential_field_inventory_by_credential_id.setdefault(
                carry.credential_id, frozenset(carry.available_fields)
            )
    rebound: list[FillCarry] = []
    for carry in prior:
        failure = await _fill_carry_validation_failure(ctx, carry, page_evidence=page_evidence, url=url)
        if failure is not None:
            LOG.info(
                "copilot_fill_carry_rebind_degraded",
                reason=failure,
                url=url,
                source_url=carry.source_url,
            )
            if failure not in _FILL_CARRY_RETRYABLE_VALIDATION_FAILURES:
                ctx.fill_carry_rebound_done = True
            return
        rebound.append(carry)
    ctx.fill_carry_rebound_done = True
    trajectory = list(ctx.scout_trajectory)
    for carry in rebound:
        trajectory.append(_fill_carry_to_interaction(carry, _next_trajectory_index(trajectory)))
    ctx.scout_trajectory = _capped_with_eviction_accounting(trajectory, collection="scout_trajectory")
    LOG.info(
        "copilot_fill_carry_rebound",
        url=url,
        field_count=len(rebound),
    )


async def rebind_prior_fill_carry_from_current_page(ctx: AgentContext) -> bool:
    if ctx.fill_carry_rebound_done or not ctx.prior_fill_carry:
        return False
    url = await _live_working_page_url(ctx)
    if not url:
        return False
    page_evidence = await _scout_act_observe_page_evidence(ctx, url=url)
    if page_evidence is None or not has_bounded_page_schema(page_evidence):
        return False
    return await rebind_prior_fill_carry_from_page_evidence(ctx, page_evidence=page_evidence, url=url)


async def rebind_prior_fill_carry_from_page_evidence(
    ctx: AgentContext,
    *,
    page_evidence: dict[str, Any],
    url: str,
) -> bool:
    if ctx.fill_carry_rebound_done or not ctx.prior_fill_carry:
        return False
    if not has_bounded_page_schema(page_evidence):
        return False
    trajectory_len = len(ctx.scout_trajectory)
    await _maybe_rebind_prior_fill_carry(ctx, page_evidence=page_evidence, url=url)
    return len(ctx.scout_trajectory) > trajectory_len


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


def _mint_current_loaded_result_source(
    ctx: AgentContext,
    page_evidence: dict[str, Any] | None,
    *,
    url: str,
) -> LoadedResultCompositionEvidence | None:
    if page_evidence is None:
        return None
    loaded_results = loaded_result_composition_evidence_from_page(
        page_evidence,
        source_tool="evaluate",
        source_url=url,
    )
    if loaded_results is not None:
        ctx.latest_evaluate_result_composition_steer = loaded_results
        ctx.latest_evaluate_result_composition_signature = None
    return loaded_results


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
                # A card that renders asynchronously after the click is absent from the first
                # capture; settle briefly so the single recapture can witness it before crediting.
                settle_seconds = min(settings.COPILOT_CLICK_SETTLE_DELAY_SECONDS, remaining_seconds)
                if settle_seconds > 0:
                    await asyncio.sleep(settle_seconds)
                    remaining_seconds = timeout_seconds - (time.monotonic() - started)
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


def account_no_progress_interaction_click(ctx: AgentContext, result: dict[str, Any]) -> None:
    """Climb or reset the no-forward-progress counter from a click's outcome: a failed click or hollow
    observe is no progress, an attached observe is progress, a capture timeout/error is neutral."""
    if not result.get("ok"):
        register_no_progress_interaction_click(ctx, outcome="click_failed")
        return
    outcome = ctx.last_scout_act_observe_outcome
    if outcome == "attached":
        reset_no_progress_interaction_count(ctx)
    elif outcome == "hollow":
        register_no_progress_interaction_click(ctx, outcome="hollow")
    else:
        LOG.info("copilot_no_progress_interaction_neutral", outcome=outcome)


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


_EVALUATE_ACTIONABLE_MAX_TARGETS = 4

_EVALUATE_ACTIONABLE_ACT_INSTRUCTION = (
    "This page already exposes actionable targets; click the intended one rather than re-evaluating."
)
_EVALUATE_RESULT_COMPOSITION_INSTRUCTION = (
    "Loaded results are already visible on the current page; inspect this page for composition or author an "
    "extraction/validation block from the loaded results instead of re-reading it. For each requested output "
    "value you can see, pass it to inspect_page_for_composition's requested_output_reads as the value exactly "
    "as rendered plus the label above it, and the page will pin the read for you."
)


def _reset_evaluate_tracker(ctx: AgentContext) -> None:
    ctx.last_evaluate_actionable_signature = None
    ctx.last_evaluate_actionable_url = None
    ctx.latest_evaluate_result_composition_steer = None
    ctx.latest_evaluate_result_composition_signature = None


def _actionable_target_identities(evidence: dict[str, Any]) -> list[tuple[str, str]]:
    affordances: list[tuple[str, str]] = []
    fields: list[tuple[str, str]] = []

    def identity(control: Any) -> tuple[str, str] | None:
        if not isinstance(control, dict):
            return None
        selector = _summary_text(control.get("selector"))
        text = _summary_text(control.get("text") or control.get("value") or control.get("aria_label"))
        if selector or text:
            return (selector, text)
        return None

    def add_affordance(control: Any) -> None:
        ident = identity(control)
        if ident is not None:
            affordances.append(ident)

    for form in evidence.get("forms") or []:
        if not isinstance(form, dict):
            continue
        for control in form.get("submit_controls") or []:
            add_affordance(control)
        for field_entry in form.get("fields") or []:
            ident = identity(field_entry)
            if ident is not None:
                fields.append(ident)
    for target in evidence.get("navigation_targets") or []:
        add_affordance(target)
    for control in evidence.get("clickable_controls") or []:
        add_affordance(control)
    for overlay in evidence.get("modal_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        for control in overlay.get("dismiss_controls") or []:
            add_affordance(control)
    for container in evidence.get("result_containers") or []:
        add_affordance(container)
    # Click affordances precede plain input fields, and selector-bearing controls precede
    # text-only ones, so the capped payload surfaces executable selectors first.
    affordances.sort(key=lambda item: 0 if item[0] else 1)
    return affordances + fields


def _click_affordance_target_identities(evidence: dict[str, Any]) -> list[tuple[str, str]]:
    """Selector-bearing click affordances only (submit controls, navigation targets, standalone
    clickable controls, modal dismiss controls), so the re-perception attach hands back a real
    selector to copy and never a plain input field, result container, or text-only control."""
    identities: list[tuple[str, str]] = []

    def add(control: Any) -> None:
        if not isinstance(control, dict):
            return
        selector = _summary_text(control.get("selector"))
        if not selector:
            return
        text = _summary_text(control.get("text") or control.get("value") or control.get("aria_label"))
        identities.append((selector, text))

    for form in evidence.get("forms") or []:
        if not isinstance(form, dict):
            continue
        for control in form.get("submit_controls") or []:
            add(control)
    for target in evidence.get("navigation_targets") or []:
        add(target)
    for control in evidence.get("clickable_controls") or []:
        add(control)
    for overlay in evidence.get("modal_overlays") or []:
        if not isinstance(overlay, dict):
            continue
        for control in overlay.get("dismiss_controls") or []:
            add(control)
    return identities


def _actionable_target_signature(identities: list[tuple[str, str]]) -> str:
    canonical = json.dumps(sorted(identities), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _actionable_targets_for_result(identities: list[tuple[str, str]]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for selector, text in identities[:_EVALUATE_ACTIONABLE_MAX_TARGETS]:
        entry = {key: value for key, value in {"selector": selector, "text": text}.items() if value}
        if entry:
            targets.append(entry)
    return targets


# Verbs that imply an irreversible or money-moving side effect — never auto-clicked.
_AUTO_ACT_HIGH_TIER_VERBS = frozenset(
    {
        "pay",
        "payment",
        "purchase",
        "buy",
        "order",
        "place order",
        "checkout",
        "delete",
        "remove",
        "transfer",
        "send",
        "submit payment",
        "confirm payment",
        "wire",
        "withdraw",
        "cancel",
    }
)


def _auto_act_is_high_tier_label(*labels: Any) -> bool:
    for label in labels:
        if not isinstance(label, str):
            continue
        normalized = label.strip().lower()
        if not normalized:
            continue
        if any(verb in normalized for verb in _AUTO_ACT_HIGH_TIER_VERBS):
            return True
    return False


def _auto_act_href_is_navigation(href: Any) -> bool:
    if not isinstance(href, str):
        return False
    candidate = href.strip()
    if not candidate or candidate.startswith("#"):
        return False
    lowered = candidate.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:")):
        return False
    if lowered.startswith(("http://", "https://")):
        return True
    return not lowered.startswith(("data:", "blob:"))


def _auto_act_candidate(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single unambiguous, low-tier nav link to auto-click, or None.

    Eligible only from navigation_targets (`<a href>` with a real http/https/relative
    href and non-empty text); form submit_controls, form fields, result containers, and
    modal dismiss controls are excluded. Form submits are never candidates because the
    structured-evidence producer cannot reliably distinguish a writing submit from a
    bare default-submit button, so the whole form-submit class is dropped for safety.
    Money-moving / destructive verbs are dropped. Exactly one survivor ⇒ act; zero or
    more than one ⇒ None."""
    candidates: list[dict[str, Any]] = []

    for target in parsed.get("navigation_targets") or []:
        if not isinstance(target, dict):
            continue
        selector = _summary_text(target.get("selector"))
        if not selector or target.get("disabled") is True:
            continue
        if not _auto_act_href_is_navigation(target.get("href")):
            continue
        text = _summary_text(target.get("text"))
        if not text:
            continue
        if _auto_act_is_high_tier_label(text, target.get("name"), target.get("id")):
            continue
        candidates.append({"selector": selector, "text": text})

    if len(candidates) != 1:
        return None
    return candidates[0]


_EVALUATE_STEER_SHED_MARKER = "[omitted on repeat — act on the named target instead of re-reading]"

# Keys the steer must never shed: navigation/identity context plus the steer's own output.
_EVALUATE_STEER_ESSENTIAL_KEYS = frozenset(
    {
        "url",
        "title",
        "observation_step",
        "actionable_targets",
        "composition_targets",
        "next_action",
        "next_action_reason",
    }
)

# Nested bulky subfields inside an evaluate `result` dict (the raw page payload).
_EVALUATE_STEER_NESTED_BULKY_KEYS = ("html", "outerHTML", "innerHTML", "body", "bodyText", "text", "buttons")


def _serialized_len(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except Exception:
        return len(str(value))


def _largest_non_essential_data_key(data: dict[str, Any]) -> str | None:
    candidates = [
        (key, _serialized_len(value))
        for key, value in data.items()
        if key not in _EVALUATE_STEER_ESSENTIAL_KEYS and value != _EVALUATE_STEER_SHED_MARKER
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


def _fit_evaluate_steer_under_cap(
    result: dict[str, Any],
    data: dict[str, Any],
    *,
    keep_raw_page_payload: bool,
) -> None:
    """Keep the serialized result under the scout result budget without ever head-slicing it.

    Reconnaissance output needs the raw page payload available to the model; imperative steers have
    enough structured evidence to shed bulky non-essential payload while always preserving the action."""

    def over(limit: int) -> bool:
        return len(json.dumps(result, default=str)) > limit

    def over_cap() -> bool:
        return over(_SCOUT_RESULT_CHAR_CAP)

    if not over_cap():
        return
    if keep_raw_page_payload:
        data.pop("actionable_targets", None)
        nested = data.get("result")
        if isinstance(nested, dict):
            for key in _EVALUATE_STEER_NESTED_BULKY_KEYS:
                if not over(_SCOUT_RECON_RESULT_CHAR_CAP):
                    return
                if key in nested and nested[key] != _EVALUATE_STEER_SHED_MARKER:
                    nested[key] = _EVALUATE_STEER_SHED_MARKER
        while over(_SCOUT_RECON_RESULT_CHAR_CAP):
            largest_key = _largest_non_essential_data_key(data)
            if largest_key is None:
                break
            data[largest_key] = _EVALUATE_STEER_SHED_MARKER
        return
    nested = data.get("result")
    if isinstance(nested, dict):
        for key in _EVALUATE_STEER_NESTED_BULKY_KEYS:
            if key in nested and nested[key] != _EVALUATE_STEER_SHED_MARKER:
                nested[key] = _EVALUATE_STEER_SHED_MARKER
                if not over_cap():
                    return
    while over_cap():
        largest_key = _largest_non_essential_data_key(data)
        if largest_key is None:
            break
        data[largest_key] = _EVALUATE_STEER_SHED_MARKER
        if not over_cap():
            return
    targets = data.get("actionable_targets")
    while isinstance(targets, list) and len(targets) > 1 and over_cap():
        targets.pop()


def _auto_act_essential_keys() -> frozenset[str]:
    return _EVALUATE_STEER_ESSENTIAL_KEYS | {"auto_acted", "page"}


async def _auto_act_on_repeat(ctx: AgentContext, result: dict[str, Any], *, url: str, target: dict[str, Any]) -> bool:
    """Issue the in-process click for the single unambiguous target and reshape the result.

    Returns True when the click landed and the result was reshaped to report it; False
    degrades the caller to the advisory steer (next_action/actionable_targets left intact)."""
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    server = ctx.discovery_mcp_server
    if server is None:
        return False
    selector = target["selector"]
    pre_url = await _live_working_page_url(ctx) or url
    try:
        click_result = await asyncio.wait_for(
            server.call_internal_tool("skyvern_click", {"selector": selector, "selector_mode": "direct"}),
            timeout=settings.COPILOT_SCOUT_ACT_OBSERVE_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.warning("copilot_evaluate_auto_act_failed", url=pre_url, selector=selector, exc_info=True)
        return False
    if not isinstance(click_result, dict) or not click_result.get("ok"):
        LOG.warning(
            "copilot_evaluate_auto_act_failed",
            url=pre_url,
            selector=selector,
            error=(click_result or {}).get("error") if isinstance(click_result, dict) else None,
        )
        return False

    post_url = await _live_working_page_url(ctx) or url
    post_evidence = await _scout_act_observe_page_evidence(ctx, url=post_url)
    navigated = bool(pre_url) and bool(post_url) and pre_url != post_url
    role, accessible_name = await _resolve_scout_role_name(ctx, selector, allow_browser_read=not navigated)
    _record_scouted_interaction(
        ctx,
        tool_name="click",
        selector=selector,
        source_url=pre_url,
        role=role,
        accessible_name=accessible_name,
    )
    # This path records its click after the capture, so the encounter is attributed here instead.
    auto_act_carrier = composition_challenge_carrier(post_evidence) if post_evidence else None
    if auto_act_carrier is not None:
        _record_challenge_encounter(ctx, auto_act_carrier, outcome="detected")

    for key in ("next_action", "next_action_reason", "actionable_targets"):
        data.pop(key, None)
    data["auto_acted"] = {"tool": "click", "selector": selector, "text": target.get("text", "")}
    if post_evidence is None:
        data["auto_acted"]["note"] = "clicked; post-click page evidence unavailable"
    else:
        data["page"] = _build_scout_page_summary(post_evidence)
    essential = _auto_act_essential_keys()
    while len(json.dumps(result, default=str)) > _SCOUT_RESULT_CHAR_CAP:
        largest = max(
            (
                (key, _serialized_len(value))
                for key, value in data.items()
                if key not in essential and value != _EVALUATE_STEER_SHED_MARKER
            ),
            key=lambda item: item[1],
            default=None,
        )
        if largest is None:
            page = data.get("page")
            if not isinstance(page, dict) or not _shed_scout_page_summary_section(page):
                break
            continue
        data[largest[0]] = _EVALUATE_STEER_SHED_MARKER
    LOG.info("copilot_evaluate_auto_acted", url=post_url, selector=selector)
    return True


class _UnsetEvidence:
    pass


_EVIDENCE_UNSET = _UnsetEvidence()


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


def composition_steer_bypassed_for_action_goal(
    unmet_criteria: Sequence[CompletionCriterion], parsed: dict[str, Any], *, download_target_reached: bool
) -> bool:
    """Pure decision shared by the live steer and offline replay: while an action deliverable is
    unmet and its affordance is not yet click-proven, actionable targets stay visible."""
    return bool(unmet_criteria) and not download_target_reached and bool(_actionable_target_identities(parsed))


def _dump_steer_decision(ctx: AgentContext, parsed: dict[str, Any], *, url: str, outcome: str) -> None:
    """Write the evaluate-steer decision and its inputs when a local run asks for them, so a
    steering change replays offline against real captures instead of costing a live turn each.
    The evidence carries page text, so writing requires the explicit path plus a local environment."""
    directory = os.environ.get("COPILOT_DUMP_STEER_INPUTS")
    if not directory or settings.ENV != "local":
        return
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = {
            "outcome": outcome,
            "url": url,
            "page_evidence": parsed,
            "criteria": [dataclasses.asdict(criterion) for criterion in _minted_criteria(ctx)],
            "satisfied_output_paths": sorted(_satisfied_output_paths(ctx)),
            "satisfied_criterion_ids": sorted(_inapplicable_criterion_ids(ctx)),
            "download_target_reached": ctx.reached_download_target is not None,
            "reached_download_target": ctx.reached_download_target.to_dict() if ctx.reached_download_target else None,
            "last_evaluate_actionable_signature": ctx.last_evaluate_actionable_signature,
            "last_evaluate_actionable_url": ctx.last_evaluate_actionable_url,
        }
        target = os.path.join(directory, f"steer-{outcome}-{uuid.uuid4().hex[:8]}.json")
        with open(os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as handle:
            json.dump(payload, handle, default=str)
    except Exception:
        LOG.info("copilot_steer_input_dump_failed", exc_info=True)


async def _maybe_steer_evaluate_to_action(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    url: str,
    page_evidence: dict[str, Any] | None | _UnsetEvidence = _EVIDENCE_UNSET,
) -> bool:
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    try:
        parsed = (
            await _scout_act_observe_page_evidence(ctx, url=url)
            if isinstance(page_evidence, _UnsetEvidence)
            else page_evidence
        )
        if parsed is None:
            _reset_evaluate_tracker(ctx)
            return False
        contract = mint_scout_observation_contract_for_ctx(ctx, parsed, url=url)
        ctx.scout_observation_contract = contract
        if not has_actionable_steer_content(parsed):
            record_scouted_output_coverage(ctx, parsed, contract=contract, include_lexical=False)
            _reset_evaluate_tracker(ctx)
            return False
        record_scouted_output_coverage(ctx, parsed, contract=contract)
        _record_scout_page_observation(ctx, parsed)
        # An overlay the loop can clear takes precedence over reading what it obscured; the dismiss
        # control is already among the actionable targets, so the model chooses rather than being
        # vetoed, and the next observation sees the page. Checked before results are minted rather
        # than after: a packet describing only a dialog has no results to mint, so gating this on
        # having minted some left it dead in the one case it exists for.
        overlay_only = packet_describes_a_clearable_overlay(parsed)
        if overlay_only:
            LOG.info("copilot_evaluate_result_composition_deferred_to_obstruction", url=url)
        # An unmet action deliverable outranks reading the page it must be earned on: composing an
        # extraction here would hide the very affordances the goal still needs clicked, and no
        # extraction can satisfy a registered-download criterion (SKY incident: the July-invoice
        # billing table read; see reached_download_target.py for why only a click can prove these).
        unmet_action_criteria = unmet_action_deliverable_criteria(ctx)
        preserve_targets_for_goal = composition_steer_bypassed_for_action_goal(
            unmet_action_criteria, parsed, download_target_reached=ctx.reached_download_target is not None
        )
        if preserve_targets_for_goal:
            LOG.info(
                "copilot_evaluate_composition_steer_bypassed_for_unmet_action_goal",
                url=url,
                criterion_ids=[criterion.id for criterion in unmet_action_criteria],
            )
        # Recording an observation is independent of whether it steers. A page whose relations do not
        # mint a loaded result is steered as actionable instead, and appending only on the composition
        # branch discarded those packets outright: the log page carrying the requested count was
        # observed, steered as actionable, and never reached the binder at all (SKY-13226).
        if has_bounded_page_schema(parsed):
            _append_flow_evidence(ctx, parsed, reached_via="current_page")
        loaded_results = (
            None
            if (overlay_only or preserve_targets_for_goal)
            else _mint_current_loaded_result_source(ctx, parsed, url=url)
        )
        if loaded_results is not None:
            _reset_evaluate_tracker(ctx)
            ctx.latest_evaluate_result_composition_steer = loaded_results
            record_build_test_outcome(ctx, recorded_outcome_from_loaded_result_evidence(loaded_results))
            data.pop("actionable_targets", None)
            data["composition_targets"] = loaded_result_composition_target_summary(loaded_results)
            data["next_action"] = "compose_extraction"
            data["next_action_reason"] = _EVALUATE_RESULT_COMPOSITION_INSTRUCTION
            # The steer is structured enough to keep under cap without preserving raw page payload.
            _fit_evaluate_steer_under_cap(result, data, keep_raw_page_payload=False)
            LOG.info(
                "copilot_evaluate_result_composition_steer",
                url=url,
                result_container_count=loaded_results.result_container_count,
                table_result_container_count=loaded_results.table_result_container_count,
            )
            _dump_steer_decision(ctx, parsed, url=url, outcome="composition_steer")
            # The result is patched in-place; returning False keeps the normal tool-loop guard active.
            return False
        ctx.latest_evaluate_result_composition_steer = None
        ctx.latest_evaluate_result_composition_signature = None
        identities = _actionable_target_identities(parsed)
        if not identities:
            _reset_evaluate_tracker(ctx)
            return False
        signature = _actionable_target_signature(identities)
        # Strict full-URL match (fragment included): on an SPA a hash-route change
        # is a navigation, so a differing fragment must read as a different page.
        is_repeat = ctx.last_evaluate_actionable_signature == signature and ctx.last_evaluate_actionable_url == url
        ctx.last_evaluate_actionable_signature = signature
        ctx.last_evaluate_actionable_url = url
        if signature != ctx.last_auto_acted_signature and ctx.last_auto_acted_signature is not None:
            ctx.last_auto_acted_signature = None
        targets = _actionable_targets_for_result(identities)
        # Generic evaluate-loop breaker: intentionally fires for all v2 policies, not only code-first.
        if is_repeat and ctx.last_auto_acted_signature != signature:
            candidate = _auto_act_candidate(parsed)
            if candidate is not None:
                ctx.last_auto_acted_signature = signature
                if await _auto_act_on_repeat(ctx, result, url=url, target=candidate):
                    LOG.info("copilot_evaluate_actionable_target_steer", url=url, is_repeat=True, steered=True)
                    return True
        if targets:
            data["actionable_targets"] = targets
            if is_repeat:
                data["next_action"] = "click"
                data["next_action_reason"] = _EVALUATE_ACTIONABLE_ACT_INSTRUCTION
            _fit_evaluate_steer_under_cap(result, data, keep_raw_page_payload=not is_repeat)
        LOG.info(
            "copilot_evaluate_actionable_target_steer",
            url=url,
            actionable_target_count=len(identities),
            is_repeat=is_repeat,
            steered=is_repeat and bool(targets),
        )
        _dump_steer_decision(ctx, parsed, url=url, outcome="actionable_path")
    except Exception:
        data.pop("actionable_targets", None)
        data.pop("composition_targets", None)
        data.pop("next_action", None)
        data.pop("next_action_reason", None)
        _reset_evaluate_tracker(ctx)
        LOG.warning("copilot_evaluate_actionable_target_steer_failed", exc_info=True)
    return False


def _register_reached_download_scout_interaction(ctx: AgentContext, target: ReachedDownloadTarget, *, url: str) -> None:
    """Record the evaluate-resolved download affordance as a scout_interaction observation.

    The scout-act download gate is cleared by a scout_interaction this turn, but the reached-download
    target is resolved on the evaluate post-hook (source_tool="evaluate"). Registering the affordance
    here unifies the two: the same evaluate call that feeds the synthesizer also clears the gate, so
    obeying the scout-act steering is sufficient and the gate cannot loop on a scouted download.
    """
    selector = target.selector.strip()
    if not selector or not url.strip():
        return
    _append_flow_evidence(
        ctx,
        {
            "inspected_url": url,
            "current_url": url,
            "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
            "interaction_tool": "evaluate",
            "interaction_selector": selector,
            "download_kind": target.download_kind,
        },
        reached_via="interaction",
    )


def _with_trajectory_anchor(ctx: AgentContext, target: ReachedDownloadTarget) -> ReachedDownloadTarget:
    """Pin the target to the trajectory position where the affordance was observed, using the stored
    ``trajectory_index`` rather than the list position so the anchor survives trajectory eviction."""
    trajectory = list(ctx.scout_trajectory)
    if not trajectory:
        return target
    anchor = trajectory[-1].get("trajectory_index")
    if not isinstance(anchor, int):
        return target
    return replace(target, trajectory_anchor=anchor)


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

        page.once("popup", _capture)
    except Exception:
        LOG.warning("copilot_scout_popup_listener_failed", exc_info=True)


# Bounded so a page that stalls the probe cannot spend the turn budget one click at a time.
_RENDER_PROBE_TIMEOUT_MS = 5000.0


def _attach_reached_download_target(
    ctx: AgentContext, data: dict[str, Any], target: ReachedDownloadTarget, *, url: str
) -> None:
    data["reached_download_target"] = target.to_dict()
    data["reached_download_guidance"] = _reached_download_guidance_for(target)
    ctx.reached_download_target = _with_trajectory_anchor(ctx, target)
    if ctx.synthesized_block_offered and not ctx.update_workflow_called:
        ctx.synthesized_block_offered = False
        ctx.synthesized_block_offered_goal_complete = False
    _register_reached_download_scout_interaction(ctx, target, url=url)
    LOG.info(
        "copilot_reached_download_target_steer",
        url=url,
        download_kind=target.download_kind,
        already_registered=target.already_registered,
    )


async def _maybe_attach_observed_render_target(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    selector: str,
    url: str,
) -> None:
    """S3 sibling: pin the clicked affordance when the click opened a new tab that renders the
    document inline — the proof class the download-dir diff above can never see."""
    if ctx.reached_download_target is not None and _is_click_proven_download_target(ctx.reached_download_target):
        return
    data = result.get("data")
    if not isinstance(data, dict) or not selector:
        return
    popup = ctx.pending_scout_popup
    ctx.pending_scout_popup = None
    if popup is None:
        return
    try:
        await popup.wait_for_load_state("domcontentloaded", timeout=_RENDER_PROBE_TIMEOUT_MS)
        popup_url = str(popup.url)
        content_type = ctx.pending_scout_popup_content_type or ""
        ctx.pending_scout_popup_content_type = None
        if not content_type:
            # The popup's navigation response is often already dispatched before the handler
            # attaches. document.contentType is page-scriptable, but this target only drives
            # guidance now (it compiles nothing and credits no download), so a spoofed value
            # costs a missing download step rather than a false one.
            content_type = str(await popup.evaluate("document.contentType") or "")
        if not content_type.lower().startswith("image/"):
            LOG.debug("copilot_observed_render_declined", reason="not_image_render", url=url, content_type=content_type)
            return
        target = derive_from_observed_render(
            selector=selector, rendered_url=popup_url, affordance_text=_scout_click_text(result)
        )
        if target is None:
            return
        _attach_reached_download_target(ctx, data, target, url=url)
    except Exception:
        LOG.warning("copilot_observed_render_target_attach_failed", exc_info=True)


async def _maybe_attach_observed_download_target(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    selector: str,
    url: str,
) -> None:
    """S3: pin the clicked affordance when the scout's own click produced a new download.

    href shape cannot see a command-style download URL, so without this the model authors the
    download step freehand instead of receiving the synthesizer's uncaught terminal."""
    data = result.get("data")
    if not isinstance(data, dict) or not selector:
        return
    before = ctx.pending_scout_download_snapshot
    ctx.pending_scout_download_snapshot = None
    if before is None:
        return
    try:
        after = await _scout_session_download_names(ctx)
        if after is None or not (after - before):
            return
        target = derive_from_observed_download(selector=selector, affordance_text=_scout_click_text(result))
        if target is None:
            return
        _attach_reached_download_target(ctx, data, target, url=url)
    except Exception:
        LOG.warning("copilot_observed_download_target_attach_failed", exc_info=True)


def _scout_click_text(result: dict[str, Any]) -> str:
    data = result.get("data")
    if not isinstance(data, dict):
        return ""
    text = data.get("text") or data.get("accessible_name") or ""
    return text if isinstance(text, str) else ""


def _is_click_proven_download_target(target: ReachedDownloadTarget | None) -> bool:
    # Both kinds carry proof from the scout's own click, so neither may be repointed by a
    # shape prediction nothing has exercised.
    return target is not None and target.download_kind in (DOWNLOAD_KIND_OBSERVED, DOWNLOAD_KIND_OBSERVED_RENDER)


async def _maybe_attach_reached_download_target(
    ctx: AgentContext,
    result: dict[str, Any],
    *,
    url: str,
    page_evidence: dict[str, Any] | None | _UnsetEvidence = _EVIDENCE_UNSET,
) -> None:
    """Attach a typed reached-download target + guidance when the page exposes exactly one same-host
    download affordance, matched on the captured selector (never URL — a download does not change the SPA URL)."""
    data = result.get("data")
    if not isinstance(data, dict):
        return
    # Code-first only: the guidance steers toward an expect_download code block (ADR 0010), which
    # standard-mode v2 does not author.
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return
    try:
        parsed = (
            await _scout_act_observe_page_evidence(ctx, url=url)
            if isinstance(page_evidence, _UnsetEvidence)
            else page_evidence
        )
        if parsed is None:
            return
        target = _derive_reached_download_from_nav_targets(parsed.get("navigation_targets"))
        if target is None:
            return
        if _is_click_proven_download_target(ctx.reached_download_target):
            # A download or render that actually happened outranks a prediction from link shape;
            # without this the later writer would repoint the target at an unexercised affordance.
            LOG.info("copilot_reached_download_target_prediction_yielded_to_observed", url=url)
            return
        data["reached_download_target"] = target.to_dict()
        data["reached_download_guidance"] = _reached_download_guidance_for(target)
        if not target.already_registered:
            # The pure synthesizer compiles the terminal expect_download step from this typed object.
            ctx.reached_download_target = _with_trajectory_anchor(ctx, target)
            if ctx.synthesized_block_offered and not ctx.update_workflow_called:
                # The prompt-side offer latched before this download target resolved, so it rendered the
                # non-download idiom. Reopen the latch once so the post-turn fallback re-fires carrying it.
                ctx.synthesized_block_offered = False
                ctx.synthesized_block_offered_goal_complete = False
                LOG.info("copilot_synthesized_block_offer_latch_reset_for_download", url=url)
        if not target.already_registered:
            _register_reached_download_scout_interaction(ctx, target, url=url)
        LOG.info(
            "copilot_reached_download_target_steer",
            url=url,
            download_kind=target.download_kind,
            already_registered=target.already_registered,
        )
    except Exception:
        data.pop("reached_download_target", None)
        data.pop("reached_download_guidance", None)
        LOG.warning("copilot_reached_download_target_steer_failed", exc_info=True)


async def _steer_evaluate_result(ctx: AgentContext, result: dict[str, Any], *, url: str) -> None:
    # Observe the bounded page evidence once and feed both evaluate steers; re-observe for the
    # download steer only when the actionable steer auto-acted and may have changed the page.
    if not isinstance(result.get("data"), dict):
        return
    page_evidence = await _scout_act_observe_page_evidence(ctx, url=url)
    if page_evidence is not None and has_bounded_page_schema(page_evidence):
        await _maybe_rebind_prior_fill_carry(ctx, page_evidence=page_evidence, url=url)
    acted = await _maybe_steer_evaluate_to_action(ctx, result, url=url, page_evidence=page_evidence)
    await _maybe_attach_reached_download_target(
        ctx, result, url=url, page_evidence=_EVIDENCE_UNSET if acted else page_evidence
    )


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
        and latest_outcome.reason_code == "outcome_not_demonstrated"
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
