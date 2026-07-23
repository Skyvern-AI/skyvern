from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import replace
from typing import Any, Literal, cast
from urllib.parse import urlparse

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.copilot.build_test_outcome import (
    record_build_test_outcome,
    recorded_outcome_from_loaded_result_evidence,
    recorded_outcome_from_scout_act_observe_hollow,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    _is_positional_selector,
    dynamic_row_evidence_fingerprint,
    dynamic_row_period_matches_match_selected_row,
    locator_selector_literals,
    normalized_locator_expr,
    normalized_scout_selector,
    synthesize_code_block,
    validated_dynamic_row_period_matches,
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
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import FillCarry
from skyvern.forge.sdk.copilot.enforcement import (
    _RECENT_TOOL_OUTPUT_CHAR_CAP,
    mint_scout_observation_contract_for_ctx,
    record_reached_terminal_action_observation,
    record_scouted_output_coverage,
    register_no_progress_interaction_click,
    reset_no_progress_interaction_count,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    ReachedDownloadTarget,
)
from skyvern.forge.sdk.copilot.reached_download_target import (
    derive_from_navigation_targets as _derive_reached_download_from_nav_targets,
)
from skyvern.forge.sdk.copilot.reached_download_target import guidance_for as _reached_download_guidance_for
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

from ._shared import (
    _DISCOVERY_PER_CALL_TIMEOUT_SECONDS,
    _append_flow_evidence,
    _composition_get_structured_evidence,
    _same_page_ignoring_fragment,
    _workflow_verification_evidence,
)
from .banned_blocks import _copilot_block_authoring_policy

LOG = structlog.get_logger()

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
    if parsed is not None:
        role, name = parsed
    else:
        captured = await _capture_accessible_role_name(
            ctx, selector, timeout_seconds=_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS
        )
        if captured is None:
            return
        role, name = captured
    if not role or not name:
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


def _maybe_complete_never_captured_obligation(
    ctx: AgentContext, *, interaction: ScoutedInteraction, trajectory_index: int
) -> None:
    obligation = getattr(ctx, "never_captured_obligation", None)
    if obligation is None or obligation.state != "armed":
        return
    if obligation.turn_id != str(getattr(ctx, "turn_id", "")):
        return
    tool_name = str(interaction.get("tool_name") or "")
    if tool_name != obligation.expected_tool_name or trajectory_index <= obligation.armed_after_trajectory_index:
        return
    # Bare locator obligations can reject unrelated same-tool events without paying for a full
    # trajectory synthesis. Non-bare canonical locators still fall through to the exact emitted
    # interaction comparison below.
    if obligation.normalized_receiver.startswith("page.locator("):
        expected_selectors = {
            normalized_scout_selector(candidate)
            for candidate in locator_selector_literals(obligation.normalized_receiver)
        }
        captured_selector = str(interaction.get("selector") or "").strip()
        if normalized_scout_selector(captured_selector) not in expected_selectors:
            return
    expected_argument = obligation.expected_argument_literal
    if expected_argument is not None:
        if tool_name == "press_key":
            captured_argument = str(interaction.get("key") or "")
        elif tool_name == "select_option":
            captured_argument = str(interaction.get("value") or "")
        elif tool_name == "type_text":
            captured_argument = str(interaction.get("raw_typed_value") or interaction.get("typed_value") or "")
        else:
            captured_argument = ""
        if captured_argument != expected_argument:
            return
    synthesized = synthesize_code_block(ctx.scout_trajectory, strict_selectors=True)
    if synthesized is None:
        return
    current_position = next(
        (
            position
            for position, item in enumerate(ctx.scout_trajectory)
            if item.get("trajectory_index") == trajectory_index
        ),
        None,
    )
    if current_position is None:
        return
    emitted = next(
        (
            record
            for record in synthesized.diagnostics.emitted_interactions
            if record.get("trajectory_index") == current_position
        ),
        None,
    )
    if emitted is None:
        return
    method = str(emitted.get("method") or "")
    locator = normalized_locator_expr(str(emitted.get("locator") or ""))
    if method != obligation.method or locator != obligation.normalized_receiver:
        return
    ctx.never_captured_obligation = replace(
        obligation,
        captured_trajectory_index=trajectory_index,
        state="captured",
    )
    ctx.synthesized_block_reopened_for_capture_obligation = True
    LOG.info(
        "copilot_never_captured_obligation_completed",
        identity_digest=obligation.identity_digest,
        turn_id=obligation.turn_id,
        workflow_permanent_id=ctx.workflow_permanent_id,
        draft_fingerprint=obligation.draft_fingerprint,
        block_label=obligation.block_label,
        site=obligation.site,
        trajectory_index=trajectory_index,
        method=method,
        locator=locator,
    )


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
    _maybe_complete_never_captured_obligation(
        ctx,
        interaction=trajectory_artifact,
        trajectory_index=trajectory_artifact["trajectory_index"],
    )

    LOG.info(
        "copilot_scout_interaction_captured",
        tool_name=tool_name,
        selector=selector or None,
        source_url=artifact.get("source_url"),
        role=role or None,
        total_scouted_interactions=len(ctx.scouted_interactions),
        total_scout_trajectory=len(ctx.scout_trajectory),
    )
    record_reached_terminal_action_observation(ctx)


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


async def _scout_act_observe_page_evidence(ctx: AgentContext, *, url: str) -> dict[str, Any] | None:
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
        if outcome == "hollow" and parsed is not None:
            first_hollow = parsed
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
                    parsed = first_hollow
                    outcome = "hollow"
                    ctx.last_scout_act_observe_recapture_result = (
                        "timeout" if time.monotonic() - started >= timeout_seconds else "error"
                    )
                else:
                    if recaptured is None:
                        parsed = first_hollow
                        outcome = "hollow"
                        ctx.last_scout_act_observe_recapture_result = _scout_act_observe_no_payload_result(
                            started=started, timeout_seconds=timeout_seconds
                        )
                    else:
                        recaptured_outcome = _scout_act_observe_capture_outcome(
                            recaptured, started=started, timeout_seconds=timeout_seconds
                        )
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
        parsed = await _scout_act_observe_page_evidence(ctx, url=url)
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


_EVALUATE_ACTIONABLE_MAX_TARGETS = 4

_EVALUATE_ACTIONABLE_ACT_INSTRUCTION = (
    "This page already exposes actionable targets; click the intended one rather than re-evaluating."
)
_EVALUATE_RESULT_COMPOSITION_INSTRUCTION = (
    "Loaded results are already visible on the current page; inspect this page for composition or author an "
    "extraction/validation block from the loaded results instead of re-reading it."
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
    """Keep the serialized result under the recent-output cap without ever head-slicing it.

    Reconnaissance output needs the raw page payload available to the model; imperative steers have
    enough structured evidence to shed bulky non-essential payload while always preserving the action."""

    def over_cap() -> bool:
        return len(json.dumps(result, default=str)) > _RECENT_TOOL_OUTPUT_CHAR_CAP

    if not over_cap():
        return
    if keep_raw_page_payload:
        data.pop("actionable_targets", None)
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
    server = getattr(ctx, "discovery_mcp_server", None)
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

    for key in ("next_action", "next_action_reason", "actionable_targets"):
        data.pop(key, None)
    data["auto_acted"] = {"tool": "click", "selector": selector, "text": target.get("text", "")}
    if post_evidence is None:
        data["auto_acted"]["note"] = "clicked; post-click page evidence unavailable"
    else:
        data["page"] = _build_scout_page_summary(post_evidence)
    essential = _auto_act_essential_keys()
    while len(json.dumps(result, default=str)) > _RECENT_TOOL_OUTPUT_CHAR_CAP:
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
        loaded_results = _mint_current_loaded_result_source(ctx, parsed, url=url)
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
