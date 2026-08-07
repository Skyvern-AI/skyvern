"""Enforcement wrapper — nudge agent when it skips required steps."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog
from agents import ModelSettings, RunConfig
from agents.run import Runner

from skyvern.config import settings
from skyvern.forge.sdk.copilot import config as copilot_config_defaults
from skyvern.forge.sdk.copilot import streaming_adapter
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    clear_tool_blocker_signals_for_reason_codes,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_phase import (
    DISCOVERY_FAILURE_STREAK_ESCAPE_THRESHOLD,
    DISCOVERY_PERMITTED_PHASES,
    BuildPhase,
)
from skyvern.forge.sdk.copilot.build_test_outcome import (
    latest_recorded_build_test_outcome_repeated,
)
from skyvern.forge.sdk.copilot.challenge_evidence import composition_challenge_carrier
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    CREDENTIAL_FILL_TOOL_NAME,
    LIVE_SCOUT_CREDENTIAL_FIELDS,
    ONE_TIME_CODE_CREDENTIAL_FIELD,
    SYNTHESIZED_OFFER_SENTINEL,
    ObligationFinding,
    credential_scout_gap,
    credential_submit_boundary_index,
    first_matched_post_fill_submit_index,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    first_stable_login_submit_index as _first_stable_login_submit_index,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    freeze_requested_output_extraction_candidate,
    is_durable_fallback_entry_target,
    is_generic_entry_opener_click,
    is_optional_dismissal_only_trajectory,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    last_scout_credential_fill_index as _last_scout_credential_fill_index,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    missing_rung_text,
    render_obligation_findings,
    render_synthesized_offer_text,
    spine_partition_findings,
    synthesize_code_block,
    synthesize_code_block_with_extraction,
    trajectory_has_browser_fill_interaction,
    uncovered_rung_records,
)
from skyvern.forge.sdk.copilot.completion_criteria_store import requested_output_paths
from skyvern.forge.sdk.copilot.completion_verification import only_structural_requested_output_abstentions
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema, interactive_challenge_controls
from skyvern.forge.sdk.copilot.config import (
    DEFAULT_ENFORCEMENT_NUDGES,
    DEFAULT_TOKEN_BUDGET,
    SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD,
    BlockAuthoringPolicy,
    CopilotConfig,
    normalize_block_authoring_policy,
)
from skyvern.forge.sdk.copilot.context import (
    AskSubject,
    coerce_ask_subject,
    parsed_ask_refs,
)
from skyvern.forge.sdk.copilot.credential_pause import credential_pause_would_fire, maybe_credential_pause
from skyvern.forge.sdk.copilot.credential_resolution import url_parts
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    RepairLoopState,
    RepairNextAction,
)
from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY, normalize_failure_reason
from skyvern.forge.sdk.copilot.narration import TransitionKind
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    RequestedOutputExtractionPlan,
    bindable_candidate_headings,
    derivation_bail_reason,
    derive_requested_output_extraction_plan,
    plan_from_designations,
    resolve_shape_expectations_by_path,
    unbound_candidate_relations,
    value_shown_in_selectable_evidence,
)
from skyvern.forge.sdk.copilot.output_policy import (
    normalize_response_scaffolding,
)
from skyvern.forge.sdk.copilot.output_utils import (
    extract_final_text,
    looks_like_workflow_delivery_claim,
    parse_final_response,
)
from skyvern.forge.sdk.copilot.request_policy import (
    REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS,
    CompletionCriterion,
    RequestPolicy,
    floor_rekeyed_requested_output_paths,
    request_policy_has_present_completion_contract,
    requested_output_path_for_field,
    schema_output_path_aliases_from_criteria,
)
from skyvern.forge.sdk.copilot.request_slots import is_canonical_request_slot_path
from skyvern.forge.sdk.copilot.result_evidence import (
    COVERAGE_TOKEN_RE,
    ScoutObservationContract,
    covered_output_paths_in_result_containers,
    mint_scout_observation_contract,
    scout_observation_bound_paths,
)
from skyvern.forge.sdk.copilot.run_outcome import (
    TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
    TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
    TERMINAL_CHALLENGE_USER_FACING_REASON,
    RecordedRunOutcome,
    run_outcome_display_reason,
)
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
    diagnosis_repair_obligation_open,
)
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.terminal_predicates import (
    artifact_health_blocked,
    outcome_criteria_evaluated,
    outcome_fully_verified,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import (
    blocker_signal_is_genuinely_terminal,
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
)
from skyvern.forge.sdk.copilot.turn_intent import TurnIntent, TurnIntentMode
from skyvern.forge.sdk.copilot.turn_ownership import (
    TurnClaimant,
    claim_turn,
)
from skyvern.forge.sdk.copilot.unrecoverable_tool_error import (
    CopilotUnrecoverableToolError as CopilotUnrecoverableToolError,
)
from skyvern.forge.sdk.copilot.unrecoverable_tool_error import (
    _maybe_raise_unrecoverable_tool_error as _maybe_raise_unrecoverable_tool_error,
)
from skyvern.utils.token_counter import count_tokens

if TYPE_CHECKING:
    from agents.agent import Agent
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.copilot.runtime import AgentContext
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

POST_FORMAT_NUDGE = copilot_config_defaults.POST_FORMAT_NUDGE
POST_INTERMEDIATE_SUCCESS_NUDGE = copilot_config_defaults.POST_INTERMEDIATE_SUCCESS_NUDGE

MAX_POST_UPDATE_NUDGES = 2
MAX_INTERMEDIATE_NUDGES = 8
MAX_FAILED_TEST_NUDGES = 2
# Repair rounds the typed obligation may force past the ordinary nudge budget before a turn is
# allowed to report an unrepairable failure.
MAX_REPAIR_OBLIGATION_NUDGES = 6
MAX_FORMAT_NUDGES = 2
MAX_NO_WORKFLOW_NUDGES = 2
MAX_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGES = 2
MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES = 2
MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES = 2
# Stops the suspicious-success nudge from re-firing forever when the agent has
# correctly diagnosed an unrecoverable block (anti-bot, paywall) and is no
# longer willing to re-run extraction.
MAX_SUSPICIOUS_SUCCESS_NUDGES = 2
# Streak levels for repeated-failure (same frontier + same failure signature).
REPEATED_FRONTIER_STREAK_ESCALATE_AT = 2
REPEATED_FRONTIER_STREAK_STOP_AT = 3
# Stop after this many consecutive runs where navigation succeeded but the
# scraper could not read the page. Aligned with MAX_FAILED_TEST_NUDGES so the
# copilot gets one generic retry nudge, then stops on the second occurrence.
# Caps how many times the per-tool-budget split nudge can fire. After two
# trips the agent should already be at single-block granularity; further
# trips fall through to the repeated-frontier escalation path.
MAX_PER_TOOL_BUDGET_NUDGES = 2
_NO_PROGRESS_INTERACTION_REASON_CODES = frozenset({"loop_detected_no_forward_progress_interaction"})
MIN_BLOCKS_FOR_AUTO_COMPLETE = 10
TOTAL_TIMEOUT_SECONDS = settings.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS or 900
# Floor for the per-iteration ``wait_for`` deadline so an already-spent budget
# never yields ``wait_for(timeout=0)`` (which raises immediately). Kept as a
# constant so tests can shrink it instead of paying a full second per deadline.
MIN_DEADLINE_REMAINING_SECONDS = 1.0
SCREENSHOT_SENTINEL = "[copilot:screenshot] "
NUDGE_SENTINEL = "[copilot:nudge] "
SCREENSHOT_PLACEHOLDER = SCREENSHOT_SENTINEL + "[prior screenshot removed to save context]"
TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET
SYNTHESIZED_BLOCK_PERSISTENCE_TOOL = "update_and_run_blocks"
# Both tools re-author the workflow draft and clear the coverage-reopen flag; the steer must fire
# for either or an update_workflow re-author silently spends the one-shot rescout.
_SYNTHESIZED_BLOCK_REAUTHORING_TOOLS = frozenset({SYNTHESIZED_BLOCK_PERSISTENCE_TOOL, "update_workflow"})
_SYNTHESIZED_BLOCK_COMMIT_TOOLS = frozenset({"click", "press_key"})
_POST_RUN_PAGE_PATH_INTERACTION_BUDGET = 4
# Evidence sources confirmable only after a run — excluded from the pre-run scout-coverage gate.
_PRE_RUN_UNGATED_EVIDENCE_SOURCES = frozenset(
    {"independent_run_evidence", "registered_output_parameter", "registered_artifact_content"}
)
# OpenAI detail=high cost per resized image. If we support other providers,
# pull from model config — this value will silently over/undercount otherwise.
# See screenshot_utils.resize_screenshot_b64 for the dimension contract this
# token count assumes.
TOKENS_PER_RESIZED_IMAGE = 765

# Keep the last N function_call_output items at full (head-truncated) size.
# Older outputs collapse to a compact synopsis so context doesn't grow linearly.
KEEP_RECENT_TOOL_OUTPUTS = 3
# A tripwire against pathological payloads, not a routine ration: code-bearing tool
# results (synthesized blocks, edit re-anchor echoes) run 3-30KB plus JSON escaping
# and must reach the model whole, so the cap sits above that class with headroom.
_RECENT_TOOL_OUTPUT_CHAR_CAP = 50_000
# Older tool-call arguments that fail to summarize keep only this much; that channel
# exists to shrink replayed context and must not follow the recent-window cap.
_SUMMARIZED_TOOL_ARGUMENT_CHAR_CAP = 2000
_TOOL_OUTPUT_SUMMARIZE_THRESHOLD = 300
_TOOL_OUTPUT_TRUNCATION_SUFFIX = "\n... [older tool output truncated]"
# Head-truncation marker for the recent tool-output window. Kept on a
# module-level constant so session_factory can import the same string and
# the two paths stay in sync if the wording ever changes.
_TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX = "\n... [truncated]"

# A REPLY matching any of these is almost certainly the agent leaking internal
# iteration state instead of finalizing or asking a specific question.
_PROGRESS_NARRATION_PATTERNS = [
    re.compile(r"\b(next|then)\s+i\s+will\b", re.IGNORECASE),
    re.compile(r"\bi\s+did\s+not\s+attempt\b", re.IGNORECASE),
    re.compile(r"\bunless\s+you\s+want\b", re.IGNORECASE),
    re.compile(r"\bi\s+will\s+(?:now\s+)?proceed\b", re.IGNORECASE),
    re.compile(r"\bi\s+have\s+not\s+yet\b", re.IGNORECASE),
]

PRESENT_COMPLETION_CONTRACT_ASK_RETRY = (
    "The final ASK_QUESTION is not an allowed terminal response for this turn: the request already has a typed "
    "completion contract / completion criteria and no separate required clarification is active. Continue authoring "
    "the workflow from the existing contract, then run/test it before responding. Only ask the user if a separate "
    "required input is missing under RequestPolicy or TurnIntent."
)


def _is_progress_narration(user_response: Any) -> bool:
    if not isinstance(user_response, str) or not user_response:
        return False
    return any(pattern.search(user_response) for pattern in _PROGRESS_NARRATION_PATTERNS)


def _normalized_proxy_label(proxy_location: Any) -> str | None:
    if proxy_location is None:
        return None
    raw_value = getattr(proxy_location, "value", proxy_location)
    if isinstance(raw_value, dict):
        country = raw_value.get("country")
        subdivision = raw_value.get("subdivision")
        city = raw_value.get("city")
        parts = [str(part).strip() for part in (country, subdivision, city) if part]
        return "-".join(parts) if parts else None
    value = str(raw_value).strip()
    if not value or value.upper() in {"NONE", "NULL", "NO_PROXY"}:
        return None
    return value


def _effective_proxy_label(ctx: Any) -> str | None:
    effective_raw = getattr(ctx, "effective_workflow_proxy_location", None)
    if effective_raw is not None:
        return _normalized_proxy_label(effective_raw)
    workflow = getattr(ctx, "last_workflow", None)
    if workflow is None:
        return None
    return _normalized_proxy_label(getattr(workflow, "proxy_location", None))


def _repair_loop_state(ctx: Any) -> RepairLoopState | None:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    state = getattr(contract, "repair_loop_state", None)
    return state if isinstance(state, RepairLoopState) else None


_CHURN_REASON_CODES = frozenset({"code_authoring_guardrail_churn", "credential_priority_authoring_churn"})


def _scouted_spine_open_obligation(ctx: AgentContext) -> list[ObligationFinding]:
    """Partition-exhaustiveness findings the latest persisted draft leaves open — uncovered required
    rungs, dropped interactions the allowlist does not forgive, retained indices in no lane, and
    truncation; empty when no in-turn persist exists or the full manifest is accounted for."""
    persisted_calls = ctx.persisted_draft_browser_calls
    if persisted_calls is None:
        return []
    if not ctx.impose_synthesized_code_block:
        return []
    if normalize_block_authoring_policy(ctx.block_authoring_policy) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return []
    trajectory = ctx.scout_trajectory
    if not trajectory:
        return []
    if not str(trajectory[0].get("source_url") or "").strip():
        return []
    synthesized = synthesize_code_block(
        trajectory,
        strict_selectors=True,
        reached_download_target=ctx.reached_download_target,
    )
    if synthesized is None:
        return []
    return spine_partition_findings(synthesized.diagnostics, persisted_calls, trajectory)


def _scouted_spine_missing_text(findings: list[ObligationFinding]) -> str:
    uncovered = uncovered_rung_records(findings)
    return missing_rung_text(uncovered) if uncovered else render_obligation_findings(findings)


def _log_scouted_spine_unresolved(findings: list[ObligationFinding], *, site: str) -> None:
    LOG.info(
        "copilot_scouted_spine_under_build_unresolved",
        site=site,
        missing_rung_count=len(uncovered_rung_records(findings)),
        missing_rungs=_scouted_spine_missing_text(findings),
    )


def _get_scouted_spine_missing_steps_for_halt(ctx: AgentContext) -> str | None:
    """Missing-steps text for any give-up offer, covering every open obligation family
    (uncovered rungs, unforgiven drops, unrecorded indices, truncation), not uncovered rungs alone."""
    try:
        findings = _scouted_spine_open_obligation(ctx)
    except Exception:
        LOG.warning("copilot_scouted_spine_halt_missing_steps_failed", exc_info=True)
        return None
    if not findings:
        return None
    return _scouted_spine_missing_text(findings)


def log_scouted_spine_unresolved_at_turn_halt(ctx: AgentContext) -> bool:
    """Log-only and never raises: a failed obligation read must not block rendering the halt reply."""
    try:
        findings = _scouted_spine_open_obligation(ctx)
    except Exception:
        LOG.warning("copilot_scouted_spine_turn_halt_check_failed", exc_info=True)
        return False
    if not findings:
        return False
    _log_scouted_spine_unresolved(findings, site="turn_halt")
    return True


def _code_authoring_reject_count_resets(repeated_outcome: bool | None, frontier_unchanged: bool) -> bool:
    # A frontier-unchanged reject is churn even when sibling edits move the whole-signature key each
    # turn (which reads as a non-repeat); it must accumulate toward the churn stop, not reset.
    return repeated_outcome is False and not frontier_unchanged


def _record_code_authoring_guardrail_reject(
    ctx: AgentContext,
    *,
    defer_churn_stop: bool = False,
    frontier_unchanged: bool = False,
    output_policy_reason_codes: frozenset[str] | None = None,
) -> None:
    # Callers record the current build-test outcome first so repeat detection compares that key to history.
    repeated_outcome = latest_recorded_build_test_outcome_repeated(ctx)
    if _code_authoring_reject_count_resets(repeated_outcome, frontier_unchanged):
        ctx.code_authoring_guardrail_reject_count = 0
    ctx.code_authoring_guardrail_reject_count += 1
    ctx.last_code_authoring_reject_was_credential_priority = defer_churn_stop
    # Any non-output-policy reject clears the cause, so the credential-priority terminal never
    # attributes a scout-gate stop to a stale raw-secret-leak streak.
    ctx.last_output_policy_reject_reason_codes = output_policy_reason_codes
    LOG.info(
        "copilot code-authoring guardrail reject recorded",
        reject_count=ctx.code_authoring_guardrail_reject_count,
        credential_priority=defer_churn_stop,
    )


def reset_no_progress_interaction_count(ctx: Any) -> None:
    if _get_int(ctx, "consecutive_no_progress_interaction_count") == 0:
        return
    ctx.consecutive_no_progress_interaction_count = 0
    clear_tool_blocker_signals_for_reason_codes(ctx, _NO_PROGRESS_INTERACTION_REASON_CODES)
    LOG.info("copilot_no_progress_interaction_reset")


def register_no_progress_interaction_click(ctx: Any, *, outcome: str) -> None:
    count = _get_int(ctx, "consecutive_no_progress_interaction_count") + 1
    ctx.consecutive_no_progress_interaction_count = count
    LOG.info("copilot_no_progress_interaction_click", outcome=outcome, count=count)


def _typed_terminal_challenge_outcome(ctx: Any) -> RecordedRunOutcome | None:
    outcome = getattr(ctx, "last_run_outcome", None)
    if not isinstance(outcome, RecordedRunOutcome):
        return None
    if outcome.reason_code != TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE:
        return None
    return outcome


def _structured_page_challenge_reason(ctx: Any, evidence: dict[str, Any] | None = None) -> str | None:
    if evidence is None:
        evidence = getattr(ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return None
    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict) and challenge_state.get("detected") is True:
        # This raw page kind is folded into an internal reason here; halt
        # metadata sanitizes it through run_outcome_display_reason below.
        kind = str(challenge_state.get("kind") or "site challenge").replace("_", " ")
        if challenge_state.get("requires_human_verification") is True:
            if "verification" in kind.lower() or "captcha" in kind.lower():
                return f"{kind} requires manual completion"
            return f"{kind} requires human verification"
        if challenge_state.get("gates_submit_controls") is True:
            return f"{kind} gates the submit/search controls"
    controls = evidence.get("challenge_controls")
    if isinstance(controls, list) and interactive_challenge_controls(controls):
        return "interactive challenge controls are visible on the page"
    return None


def _terminal_challenge_halt_signal(
    ctx: Any,
    *,
    evidence_source: str,
    evidence_reason: str,
    blocked_tool: str = "update_and_run_blocks",
    challenge_evidence_source: str | None = None,
) -> CopilotToolBlockerSignal:
    workflow_run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    safe_evidence_reason = (
        run_outcome_display_reason(evidence_reason) or "Structured challenge evidence reported a terminal blocker."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "Structured challenge evidence confirms this path is blocked: "
            f"{safe_evidence_reason}. Do NOT retry block-running tools, do NOT try a proxy/location switch "
            "in this turn, and do NOT claim the workflow is verified end-to-end. Reply with the blocker."
        ),
        user_facing_reason=TERMINAL_CHALLENGE_USER_FACING_REASON,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code=TERMINAL_CHALLENGE_BLOCKER_REASON_CODE,
        blocked_tool=blocked_tool,
        extra={
            "run_outcome_reason_code": TERMINAL_CHALLENGE_RUN_OUTCOME_REASON_CODE,
            "evidence_source": evidence_source,
            "challenge_evidence_source": challenge_evidence_source,
            "evidence_reason": safe_evidence_reason,
            "workflow_run_id": workflow_run_id if isinstance(workflow_run_id, str) else None,
        },
    )


def terminal_challenge_blocker_signal_from_page_evidence(
    ctx: Any,
    *,
    blocked_tool: str,
    evidence_source: str = "page_evidence",
    evidence: dict[str, Any] | None = None,
) -> CopilotToolBlockerSignal | None:
    packet = evidence if evidence is not None else getattr(ctx, "composition_page_evidence", None)
    page_reason = _structured_page_challenge_reason(ctx, packet)
    if page_reason is None:
        return None
    if isinstance(packet, Mapping) and one_time_code_fill_supersedes_challenge(ctx, packet):
        LOG.info(
            "copilot_terminal_challenge_declined_credential_served",
            blocked_tool=blocked_tool,
            evidence_source=evidence_source,
        )
        return None
    carrier = composition_challenge_carrier(packet)
    return _terminal_challenge_halt_signal(
        ctx,
        evidence_source=evidence_source,
        evidence_reason=page_reason,
        blocked_tool=blocked_tool,
        challenge_evidence_source=carrier.value if carrier else None,
    )


def _current_page_challenge_requires_stop(evidence: dict[str, Any]) -> bool:
    challenge_state = evidence.get("challenge_state")
    if isinstance(challenge_state, dict) and (
        challenge_state.get("requires_human_verification") is True
        or challenge_state.get("gates_submit_controls") is True
    ):
        return True
    controls = evidence.get("challenge_controls")
    return isinstance(controls, list) and bool(interactive_challenge_controls(controls))


def _current_page_evidence_candidates(ctx: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for entry in reversed(getattr(ctx, "flow_evidence", None) or []):
        if not isinstance(entry, dict):
            continue
        packet = entry.get("evidence")
        if isinstance(packet, dict):
            candidates.append(packet)
    single = getattr(ctx, "composition_page_evidence", None)
    # Both writers alias the packet they appended, so identity keeps one candidate out of two.
    if isinstance(single, dict) and not any(single is packet for packet in candidates):
        candidates.append(single)
    return candidates


# Challenge kinds a saved one-time code cannot answer, whoever else is on the page. `unknown` is the
# DOM detector's verdict for every anti-bot vendor it has no name for, so it belongs here too.
_CODE_UNSATISFIABLE_CHALLENGE_KIND_TERMS = (
    "captcha",
    "robot",
    "turnstile",
    "cloudflare",
    "access",
    "human",
    "unknown",
)


def _observed_page_key(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    parts = url_parts(url.strip())
    return parts[1] if parts else None


def _one_time_code_fill_targets(ctx: Any) -> set[tuple[str, str]]:
    """(page key, selector) for every saved one-time code this turn filled.

    Keyed by page as well as selector because an observation packet records only the selector, and
    the same selector text recurs across sites.
    """
    targets: set[tuple[str, str]] = set()
    for item in getattr(ctx, "scout_trajectory", None) or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        if str(item.get("credential_field") or "").strip() != ONE_TIME_CODE_CREDENTIAL_FIELD:
            continue
        page = _observed_page_key(item.get("source_url"))
        selector = str(item.get("selector") or "").strip()
        if page and selector:
            targets.add((page, selector))
    return targets


def _challenge_a_code_cannot_answer(evidence: Mapping[str, Any]) -> bool:
    """A deny-list on purpose: an unrecognized kind stays answerable rather than halting.

    `challenge_state.kind` is free-form vision output, so an allow-list would fail closed on the
    misread this ticket exists to fix — the witnessed failure was labelled `other`.
    """
    controls = evidence.get("challenge_controls")
    if isinstance(controls, list) and interactive_challenge_controls(controls):
        return True
    challenge_state = evidence.get("challenge_state")
    kind = str(challenge_state.get("kind") or "").lower() if isinstance(challenge_state, Mapping) else ""
    return any(term in kind for term in _CODE_UNSATISFIABLE_CHALLENGE_KIND_TERMS)


def one_time_code_fill_supersedes_challenge(ctx: Any, evidence: Mapping[str, Any]) -> bool:
    """Whether this turn filled a saved one-time code into the observed page after this challenge
    was captured.

    Scoped to challenges older than the code so it can never outlive one: a submit reaches the page
    by routes that mint no observation of their own (an Enter keypress, a block run), so anything
    observed after the fill is left to halt.
    """
    # Nothing is superseded by a packet that reports no challenge, and a caller may hold one whose
    # stop was decided by the run rather than by this page.
    if not isinstance(evidence, dict) or _structured_page_challenge_reason(ctx, evidence) is None:
        return False
    page = _observed_page_key(evidence.get("current_url") or evidence.get("inspected_url"))
    if page is None or _challenge_a_code_cannot_answer(evidence):
        return False
    targets = _one_time_code_fill_targets(ctx)
    if not targets:
        return False
    challenge_seen = False
    for entry in getattr(ctx, "flow_evidence", None) or []:
        if not isinstance(entry, dict):
            continue
        packet = entry.get("evidence")
        if not isinstance(packet, dict):
            continue
        # Both writers alias the appended packet into `composition_page_evidence`; a shallow copy
        # there would silently make this never match, which halts rather than misfires.
        if packet is evidence:
            challenge_seen = True
            continue
        if not challenge_seen or str(packet.get("interaction_tool") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        selector = str(packet.get("interaction_selector") or "").strip()
        # An interaction packet carries the post-interaction URL, so it is attributable to the page
        # it acted on only by the source it recorded.
        if (page, selector) in targets and _observed_page_key(packet.get("interaction_source_url")) == page:
            return True
    return False


def terminal_challenge_blocker_signal_from_current_page_evidence(
    ctx: Any,
    *,
    blocked_tool: str,
    evidence_source: str = "page_evidence",
) -> CopilotToolBlockerSignal | None:
    if getattr(ctx, "last_failure_category_top", None) == PER_TOOL_BUDGET_FAILURE_CATEGORY:
        return None
    for evidence in _current_page_evidence_candidates(ctx):
        if evidence.get("observed_after_workflow_run") is not True:
            continue
        if not _current_page_challenge_requires_stop(evidence):
            continue
        signal = terminal_challenge_blocker_signal_from_page_evidence(
            ctx,
            blocked_tool=blocked_tool,
            evidence_source=evidence_source,
            evidence=evidence,
        )
        if signal is not None:
            return signal
    return None


CURRENT_PAGE_CHALLENGE_ADVISORY_REASON_CODE = "tool_error_current_page_challenge_advisory"


def current_page_challenge_advisory_signal(
    ctx: Any, *, blocked_tool: str, evidence_source: str = "page_evidence"
) -> CopilotToolBlockerSignal | None:
    """A fire-once advisory in place of the current-page pre-veto: the model sees the challenge
    verdict and decides, holding facts this plane cannot see — what it filled and what it holds.

    Terminal stopping stays with the producers that key on outcomes: a run that failed with
    anti-bot evidence, the loop plane, budget. A page that merely looks like a wall has not
    stopped anything yet.
    """
    signal = terminal_challenge_blocker_signal_from_current_page_evidence(
        ctx, blocked_tool=blocked_tool, evidence_source=evidence_source
    )
    if signal is None:
        return None
    reason = str(signal.extra.get("evidence_reason") or "a verification challenge")
    fired = getattr(ctx, "challenge_advisory_fired_reasons", None)
    if not isinstance(fired, set) or reason in fired:
        return None
    fired.add(reason)
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            f"Structured page evidence flags the current page as a verification challenge: {reason}. "
            "This is advisory, not a wall. If the page is asking for something this turn already holds — "
            "for example a one-time code from the resolved credential — fill it, submit it, and continue. "
            "If it is a genuine anti-bot wall you cannot satisfy, do NOT retry the same path; reply to the "
            "user naming the blocker."
        ),
        user_facing_reason="The page may be showing a verification step; I'm checking whether I can complete it.",
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=frozenset({blocked_tool}),
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code=CURRENT_PAGE_CHALLENGE_ADVISORY_REASON_CODE,
        blocked_tool=blocked_tool,
        extra={key: value for key, value in signal.extra.items() if key != "run_outcome_reason_code"},
    )


def _maybe_stash_terminal_challenge_halt(ctx: Any) -> None:
    if getattr(ctx, "turn_halt", None) is not None:
        return
    outcome = _typed_terminal_challenge_outcome(ctx)
    if outcome is not None:
        reason = outcome.display_reason or "Structured evidence reported a terminal site challenge."
        carrier = composition_challenge_carrier(getattr(ctx, "composition_page_evidence", None))
        signal = _terminal_challenge_halt_signal(
            ctx,
            evidence_source="run_outcome",
            evidence_reason=reason,
            challenge_evidence_source=carrier.value if carrier else None,
        )
        stash_blocker_signal(ctx, signal)
        stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement")
        return
    # `last_test_ok is False` is the failed-run sentinel for this backstop.
    # Free-standing visible challenge hints remain diagnostic until a run/test
    # also records anti-bot evidence.
    if getattr(ctx, "last_test_ok", None) is not False:
        return
    if not getattr(ctx, "last_test_anti_bot", None):
        return
    page_signal = terminal_challenge_blocker_signal_from_page_evidence(ctx, blocked_tool="update_and_run_blocks")
    if page_signal is None:
        return
    stash_blocker_signal(ctx, page_signal)
    stash_turn_halt_from_blocker_signal(ctx, page_signal, source="enforcement")


class CopilotTotalTimeoutError(Exception):
    """Raised when the copilot agent exceeds the total allowed runtime."""


def latest_diagnosis_contract_satisfies_goal(ctx: CopilotContext) -> bool:
    contract = ctx.latest_diagnosis_repair_contract
    if contract is None:
        return False
    verification = contract.verification_result
    repair_decision = contract.repair_decision
    return (
        verification.user_goal_satisfied is True
        and verification.completion_contract_satisfied is True
        and repair_decision.next_action is RepairNextAction.NO_CHANGE
    )


def _latest_diagnosis_contract_selects_no_repair(ctx: CopilotContext) -> bool:
    contract = ctx.latest_diagnosis_repair_contract
    return contract is not None and contract.repair_decision.next_action is RepairNextAction.NO_CHANGE


def _outcome_criteria_evaluated(ctx: CopilotContext) -> bool:
    return outcome_criteria_evaluated(ctx)


def _completion_verification_only_structural_abstentions(ctx: CopilotContext) -> bool:
    result = ctx.completion_verification_result
    return result is not None and only_structural_requested_output_abstentions(result)


def verified_goal_satisfied_context(ctx: CopilotContext) -> bool:
    return outcome_fully_verified(ctx)


def built_complete_without_evaluated_outcome(ctx: CopilotContext) -> bool:
    """A run that looks built and repair-inert but carries no evaluated verdict.
    It ends the turn like ``built_unverified_repair_inert_context`` does, but must
    never authorize a verified-satisfaction claim."""
    if _outcome_criteria_evaluated(ctx):
        return False
    if not (
        ctx.last_test_ok is True
        and ctx.last_full_workflow_test_ok is True
        and latest_diagnosis_contract_satisfies_goal(ctx)
    ):
        return False
    return not _verified_goal_likely_needs_more_work(ctx)


def built_unverified_repair_inert_context(ctx: CopilotContext) -> bool:
    return (
        ctx.last_test_ok is True
        and ctx.last_full_workflow_test_ok is True
        and _outcome_criteria_evaluated(ctx)
        and _latest_diagnosis_contract_selects_no_repair(ctx)
        and _completion_verification_only_structural_abstentions(ctx)
        and not _verified_goal_likely_needs_more_work(ctx)
    )


def verified_goal_claim_authorized(ctx: CopilotContext) -> bool:
    """Whether the terminal may CLAIM a tested success. Turn completion keeps
    flowing through ``verified_goal_satisfied_context``; the claim tier additionally
    requires judge-confirmed outcome evidence — criteria-less or judge-less terminals
    end the turn but render built-but-unverified."""
    return outcome_fully_verified(ctx)


def gate_decision_trace_fields(ctx: CopilotContext) -> dict[str, bool]:
    """The terminal-gate decision plus the conjuncts that explain it.

    Captured wherever the gate is evaluated (including when it returns False, the
    signal that explains why the turn continued) so a single trace shows whether
    the gate failed on the test, the full-workflow run, the diagnosis contract,
    the absence of outcome verification, or the block-count heuristic.
    """
    return {
        "gate_satisfied": verified_goal_satisfied_context(ctx),
        "gate_built_unverified_repair_inert": built_unverified_repair_inert_context(ctx),
        "gate_built_complete_without_evaluated_outcome": built_complete_without_evaluated_outcome(ctx),
        "gate_claim_authorized": verified_goal_claim_authorized(ctx),
        "gate_last_test_ok": ctx.last_test_ok is True,
        "gate_last_full_workflow_test_ok": ctx.last_full_workflow_test_ok is True,
        "gate_diagnosis_contract_satisfies_goal": latest_diagnosis_contract_satisfies_goal(ctx),
        "gate_outcome_criteria_evaluated": _outcome_criteria_evaluated(ctx),
        "gate_artifact_health_blocked": artifact_health_blocked(ctx),
        "gate_likely_needs_more_work": _verified_goal_likely_needs_more_work(ctx),
        "gate_evaluated_this_turn": True,
    }


def _verified_goal_likely_needs_more_work(ctx: CopilotContext) -> bool:
    block_count = ctx.last_update_block_count
    if not isinstance(block_count, int):
        return False
    user_message = ctx.user_message
    completion_contract = _request_completion_contract(ctx)
    return _goal_likely_needs_more_blocks(user_message, block_count, completion_contract)


def _mark_copilot_total_timeout(ctx: Any, *, elapsed_seconds: float, iteration: int) -> None:
    already_marked = ctx.copilot_total_timeout_exceeded is True
    ctx.copilot_total_timeout_exceeded = True
    if already_marked:
        return
    # Only CopilotContext carries build_phase; the self-heal path passes a bare AgentContext,
    # and an AttributeError here would replace the timeout with a crash.
    build_phase = getattr(ctx, "build_phase", None)
    LOG.warning(
        "copilot_turn_deadline_expired",
        elapsed_seconds=round(elapsed_seconds, 3),
        iteration=iteration,
        build_phase=build_phase.value if isinstance(build_phase, BuildPhase) else None,
    )


def _elapsed_run_seconds(ctx: Any, start_time: float) -> float:
    """Wall-clock elapsed since ``start_time``, minus time spent in a credential pause.

    Keeps TOTAL_TIMEOUT_SECONDS a budget over actual agent work, not real
    time, so a paused-and-resumed turn isn't penalized for pause time.

    ``pause_seconds`` is coerced defensively: tests commonly pass a bare
    ``MagicMock()`` as ``ctx``, whose ``getattr(..., default)`` returns a
    fresh Mock instead of the default (Mock never raises AttributeError).
    """
    pause_seconds = getattr(ctx, "copilot_credential_pause_seconds", 0.0)
    if not isinstance(pause_seconds, (int, float)):
        pause_seconds = 0.0
    return time.monotonic() - start_time - pause_seconds


def _mark_copilot_total_timeout_if_elapsed(ctx: Any, start_time: float, iteration: int) -> None:
    elapsed = _elapsed_run_seconds(ctx, start_time)
    if elapsed >= TOTAL_TIMEOUT_SECONDS:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=elapsed, iteration=iteration)


class CopilotNonRetriableNavError(Exception):
    """Raised from run_with_enforcement when the copilot's most recent run
    hit a permanent navigation error (DNS / cert / SSL / invalid URL) and
    the loop is about to exit without a successful test. Caught at the
    agent entrypoint and translated to a deterministic user-facing failure,
    mirroring the CopilotTotalTimeoutError handling pattern."""

    def __init__(self, url: str | None, error_message: str) -> None:
        self.url = url
        self.error_message = error_message
        super().__init__(f"Non-retriable navigation error: {error_message}")


_FAILED_TO_NAVIGATE_URL_PATTERN = re.compile(r"Failed to navigate to url (\S+)\. Error message:")


def _extract_url_from_nav_error(message: str) -> str | None:
    """Pull the URL out of a FailedToNavigateToUrl string. None on no match."""
    match = _FAILED_TO_NAVIGATE_URL_PATTERN.search(message)
    return match.group(1) if match else None


def _maybe_raise_non_retriable_nav(ctx: Any) -> None:
    """Raise CopilotNonRetriableNavError if the most recent run was a
    permanent navigation failure and nothing else has succeeded. Called
    before both `return result` sites in run_with_enforcement so the loop
    cannot hand a failed run back to the caller as if it completed."""
    err = getattr(ctx, "last_test_non_retriable_nav_error", None)
    if not isinstance(err, str) or not err:
        return
    if getattr(ctx, "last_test_ok", None) is True:
        return
    raise CopilotNonRetriableNavError(url=_extract_url_from_nav_error(err), error_message=err)


_POST_RUN_PAGE_OBSERVATION_TOOLS = frozenset({"evaluate", "get_browser_screenshot", "inspect_page_for_composition"})


def _raise_if_unrecoverable_contract_stop(ctx: Any) -> None:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    diagnosis = getattr(contract, "diagnosis_result", None)
    repair_decision = getattr(contract, "repair_decision", None)
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
    if failure_type != "unrecoverable_tool_error" or next_action != "stop":
        return
    verification = getattr(contract, "verification_result", None)
    reason = getattr(verification, "remaining_blocker", None) or getattr(diagnosis, "root_cause_summary", None)
    if not isinstance(reason, str) or not reason.strip():
        reason = "Browser session was no longer reachable."
    source_tool = getattr(getattr(contract, "diagnosis_input", None), "source_tool", None)
    tool_name = source_tool if isinstance(source_tool, str) and source_tool else "unknown"
    raise CopilotUnrecoverableToolError(tool_name, reason)


_ACTION_CATEGORIES: list[list[str]] = [
    ["navigate", "go to", "open", "visit"],
    ["download", "save", "export"],
    ["extract", "scrape", "collect", "gather", "get all", "grab", "capture", "retrieve", "pull"],
    ["login", "log in", "sign in", "authenticate"],
    ["search", "find", "look for", "look up", "check", "verify"],
    ["fill", "enter", "type", "submit", "complete the form", "input"],
    ["click", "select", "choose", "pick"],
    ["upload", "attach"],
]

_SEQUENTIAL_CONNECTORS = [" and then ", " then ", " after that ", " next ", " followed by ", " afterward "]


def _request_completion_contract(ctx: Any) -> str | None:
    request_policy = getattr(ctx, "request_policy", None)
    completion_contract = getattr(request_policy, "completion_contract", None)
    if isinstance(completion_contract, str) and completion_contract.strip():
        return completion_contract.strip()
    return None


def _request_completion_contract_status(ctx: Any) -> str:
    request_policy = getattr(ctx, "request_policy", None)
    status = getattr(request_policy, "completion_contract_status", None)
    if status in ("present", "absent", "unknown"):
        return status
    return "present" if _request_completion_contract(ctx) else "absent"


def _completion_contract_unknown_due_to_policy_fallback(ctx: Any) -> bool:
    return _request_completion_contract_status(ctx) == "unknown"


_AUTHORING_TURN_INTENT_MODES = frozenset({TurnIntentMode.BUILD, TurnIntentMode.EDIT, TurnIntentMode.DRAFT_ONLY})


def _turn_intent_can_author_without_user_input(turn_intent: Any) -> bool:
    if not isinstance(turn_intent, TurnIntent):
        return False
    if turn_intent.mode not in _AUTHORING_TURN_INTENT_MODES:
        return False
    if turn_intent.authority.requires_user_input:
        return False
    return turn_intent.authority.may_update_workflow


def _turn_intent_can_update_and_run_without_user_input(turn_intent: Any) -> bool:
    if not _turn_intent_can_author_without_user_input(turn_intent):
        return False
    return bool(turn_intent.authority.may_run_blocks)


def _present_completion_contract_ask_admission_base(ctx: CopilotContext) -> bool:
    request_policy = ctx.request_policy
    if not isinstance(request_policy, RequestPolicy):
        return False
    if not request_policy_has_present_completion_contract(request_policy):
        return False
    if request_policy.user_response_policy == "ask_clarification":
        return False
    if request_policy.clarification_reason not in (None, "none"):
        return False
    return _turn_intent_can_author_without_user_input(ctx.turn_intent)


def recycle_admits_present_completion_contract_ask(ctx: CopilotContext) -> bool:
    if not _present_completion_contract_ask_admission_base(ctx):
        return False
    return not ctx.has_genuine_workflow_attempt()


def _present_completion_contract_ask_retry(ctx: CopilotContext, parsed: dict[str, Any]) -> EnforcementDecision | None:
    if parsed.get("type") != "ASK_QUESTION":
        return None
    ask_subject = coerce_ask_subject(parsed.get("ask_subject"))
    if ask_subject is not None:
        # A schema ask the contract already answers is redundant whether or not the turn has
        # built anything yet, so it resolves on the base admission without the attempt check.
        if _present_completion_contract_ask_admission_base(ctx):
            auto_answer = _typed_ask_subject_auto_answer(ctx, ask_subject, parsed)
            if auto_answer is not None:
                return EnforcementDecision(rule="typed_ask_subject_auto_answer", message=auto_answer)
    retry_admitted = recycle_admits_present_completion_contract_ask(ctx)
    if ask_subject is not None:
        LOG.info(
            "copilot_ask_subject_passed_through",
            subject=ask_subject,
            outcome="build_first_retry" if retry_admitted else "reached_user",
            **ctx.genuine_attempt_parity_fields(),
        )
    if not retry_admitted:
        return None
    LOG.info(
        "copilot.present_completion_contract_ask_retry",
        reason_code="present_completion_contract_ask_internal_retry",
        turn_intent_mode=ctx.turn_intent.mode if ctx.turn_intent else None,
        **ctx.genuine_attempt_parity_fields(),
    )
    return EnforcementDecision(
        rule="present_completion_contract_ask_retry",
        message=PRESENT_COMPLETION_CONTRACT_ASK_RETRY,
    )


def _typed_ask_subject_auto_answer(ctx: CopilotContext, ask_subject: AskSubject, parsed: dict[str, Any]) -> str | None:
    if ask_subject != "output_schema":
        return None
    # Reads the raw policy criteria rather than the turn-active set the other requested-output
    # consumers use: floor-rekey annotations are baked in at request-policy build time, and what
    # the model may cite as refs is rendered from this same set in `prompt_summary`.
    policy = ctx.request_policy
    if not isinstance(policy, RequestPolicy):
        return None
    criteria = policy.graded_completion_criteria()
    requested = requested_output_paths(criteria) | floor_rekeyed_requested_output_paths(criteria)
    if not requested:
        return None
    refs = parsed_ask_refs(parsed.get("refs"))
    if refs and set(refs) <= requested:
        resolved = sorted(set(refs))
        LOG.info(
            "copilot_ask_subject_auto_answered",
            subject=ask_subject,
            resolved_refs=resolved,
        )
        # An output_schema ask is usually "which page value is it?", and the asker has already named a
        # candidate; telling it to choose a representation licensed freelance parsing while the
        # designation route sat unused (SKY-13485). Point the ask at the read that binds instead.
        return (
            f"The output path is settled: {', '.join(resolved)}. Which page value it is, is yours to "
            "decide from what you can see — do not ask the user. If the requested value is visible "
            "now, read it off the live page: call evaluate with an expression whose result is just "
            "that value exactly as rendered, and set output_path to the requested path on that call; "
            "the extraction will bind to the value you observed. If it is not visible yet, author and "
            "test the workflow to produce it."
        )
    # A request that named no output key is pinned to anonymous slots, so no name the model could
    # propose is citable and the coverage check above can never be satisfied. The contract still
    # settles that an output is owed; only what to call it is open, and that belongs to the author.
    if any(not is_canonical_request_slot_path(path) for path in requested):
        return None
    LOG.info(
        "copilot_ask_subject_auto_answered",
        subject=ask_subject,
        resolved_refs=[],
        proposed_refs=sorted(set(refs)),
        anonymous_slot_count=len(requested),
    )
    owed = len(requested)
    subject_phrase = "one output is owed" if owed == 1 else f"{owed} outputs are owed"
    naming_phrase = "what to call it" if owed == 1 else "what to call them"
    return (
        f"This request's completion contract already settles that {subject_phrase}; the only open "
        f"question is {naming_phrase}, and that is yours to choose. Author and test the workflow to "
        "produce every value the user asked for, under reasonable names and representations, instead "
        "of asking the user to confirm field names."
    )


def _nudge(config: CopilotConfig | None, key: str) -> str:
    if config is None:
        return DEFAULT_ENFORCEMENT_NUDGES[key]
    return config.nudge(key)


@dataclass(frozen=True)
class EnforcementDecision:
    """Which enforcement rule fired, and the text it renders to.

    ``rule`` is the stable identity because nudge prose is operator-configurable;
    most rules name a key in ``DEFAULT_ENFORCEMENT_NUDGES``, but the ask-retry
    rules carry a hardcoded message and are not resolvable through ``_nudge``.
    """

    rule: str
    message: str


def _decision(config: CopilotConfig | None, key: str) -> EnforcementDecision:
    return EnforcementDecision(rule=key, message=_nudge(config, key))


def _goal_likely_needs_more_blocks(user_message: Any, block_count: int, completion_contract: str | None = None) -> bool:
    """Return True when the goal likely requires more blocks than currently exist."""
    if block_count >= MIN_BLOCKS_FOR_AUTO_COMPLETE:
        return False
    if not isinstance(user_message, str):
        return False
    text = user_message.lower()
    has_sequential = any(conn in text for conn in _SEQUENTIAL_CONNECTORS)
    if block_count >= 1 and completion_contract:
        return has_sequential and block_count < 2

    matched_categories = sum(1 for category in _ACTION_CATEGORIES if any(keyword in text for keyword in category))

    estimated_min_blocks = max(matched_categories, 2) if has_sequential else matched_categories
    return block_count < estimated_min_blocks


def _same_page(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except Exception:
        return False
    if not left_parsed.netloc or not right_parsed.netloc:
        return False
    if left_parsed.netloc.lower() != right_parsed.netloc.lower():
        return False
    left_path = (left_parsed.path or "/").rstrip("/") or "/"
    right_path = (right_parsed.path or "/").rstrip("/") or "/"
    return left_path == right_path


def _has_candidate_bound_page_evidence(ctx: Any, candidate_url: str) -> bool:
    inspection_count = int(getattr(ctx, "page_inspection_calls_this_turn", 0) or 0)
    inspection_baseline = int(getattr(ctx, "resolved_discovery_entrypoint_inspection_baseline", 0) or 0)
    if inspection_count <= inspection_baseline:
        return False
    evidence = getattr(ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return False
    if evidence.get("source_tool") != "inspect_page_for_composition":
        return False
    for key in ("inspected_url", "current_url"):
        value = evidence.get(key)
        if isinstance(value, str) and _same_page(candidate_url, value):
            return True
    return False


def _pre_discovery_url_question_nudge(
    ctx: Any,
    parsed: dict[str, Any],
    config: CopilotConfig | None = None,
) -> EnforcementDecision | None:
    """Steer the model to discovery when it asks before discovery has run.

    INITIAL/DISCOVERING phase with zero discovery calls means the model went
    straight to asking instead of resolving the entrypoint itself. Credential,
    loop, and conditional clarifications carry a non-default
    request_policy.clarification_reason and are let through; the structural
    triple (phase + zero discovery calls + default clarification_reason) already
    excludes them. The post-discovery could-not-resolve ask happens after
    discovery ran (discovery_calls_this_turn > 0) and so never reaches this gate.
    Steering any remaining pre-discovery ASK to discovery is correct: discovery
    is cheap, and if the site cannot resolve the model re-asks afterward.
    """
    if parsed.get("type") != "ASK_QUESTION":
        return None
    if getattr(ctx, "build_phase", None) not in DISCOVERY_PERMITTED_PHASES:
        return None
    if _get_int(ctx, "discovery_calls_this_turn") != 0:
        return None
    if (
        getattr(ctx, "turn_halt", None) is not None
        or _get_int(ctx, "discovery_failure_streak_this_turn") >= DISCOVERY_FAILURE_STREAK_ESCAPE_THRESHOLD
    ):
        return None
    request_policy = getattr(ctx, "request_policy", None)
    clarification_reason = getattr(request_policy, "clarification_reason", "none")
    if clarification_reason not in (None, "none"):
        return None
    nudge_count = _get_int(ctx, "pre_discovery_url_question_nudge_count")
    if nudge_count >= MAX_PRE_DISCOVERY_URL_QUESTION_NUDGES:
        return None
    ctx.pre_discovery_url_question_nudge_count = nudge_count + 1
    LOG.info(
        "copilot.pre_discovery_url_question_nudge",
        reason_code="pre_discovery_url_question_steer_to_discovery",
        build_phase=getattr(getattr(ctx, "build_phase", None), "value", None),
        nudge_count=ctx.pre_discovery_url_question_nudge_count,
    )
    return _decision(config, "pre_discovery_url_question")


def _post_discovery_entrypoint_url_question_nudge(
    ctx: Any,
    parsed: dict[str, Any],
    config: CopilotConfig | None = None,
) -> EnforcementDecision | None:
    if parsed.get("type") != "ASK_QUESTION":
        return None
    candidate_url = getattr(ctx, "resolved_discovery_entrypoint_url", None)
    failure_reason = getattr(ctx, "resolved_discovery_failure_reason", None)
    if not isinstance(candidate_url, str) or not candidate_url or failure_reason:
        return None
    inspected_after_discovery = _has_candidate_bound_page_evidence(ctx, candidate_url)
    mutated_after_discovery = bool(getattr(ctx, "update_workflow_called", False))
    if inspected_after_discovery or mutated_after_discovery:
        return None
    nudge_count = getattr(ctx, "discovery_entrypoint_url_question_nudge_count", 0)
    if nudge_count >= MAX_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGES:
        return None
    ctx.discovery_entrypoint_url_question_nudge_count = nudge_count + 1
    return EnforcementDecision(
        rule="post_discovery_entrypoint_url_question",
        message=f"{_nudge(config, 'post_discovery_entrypoint_url_question')} Resolved candidate_url: {candidate_url}",
    )


def _response_coverage_nudge(
    ctx: Any, parsed: dict[str, Any], config: CopilotConfig | None = None
) -> EnforcementDecision | None:
    """Peek at the model's final output and return a decision for coverage gaps
    or progress-narration format. ASK_QUESTION is let through so the agent
    can request missing credentials or disambiguation, except when discovery
    resolved a candidate and the agent has not yet inspected or composed from
    that candidate.

    Returns the decision to inject, or None to let the response through.
    """
    response_type = parsed.get("type")
    pre_discovery_nudge = _pre_discovery_url_question_nudge(ctx, parsed, config)
    if pre_discovery_nudge is not None:
        return pre_discovery_nudge

    discovery_entrypoint_nudge = _post_discovery_entrypoint_url_question_nudge(ctx, parsed, config)
    if discovery_entrypoint_nudge is not None:
        return discovery_entrypoint_nudge

    present_contract_retry = _present_completion_contract_ask_retry(ctx, parsed)
    if present_contract_retry is not None:
        return present_contract_retry

    if response_type not in ("REPLY", "REPLACE_WORKFLOW"):
        return None

    if (
        response_type == "REPLY"
        and not getattr(ctx, "update_workflow_called", False)
        and looks_like_workflow_delivery_claim(parsed.get("user_response"))
    ):
        nudge_count = getattr(ctx, "no_workflow_nudge_count", 0)
        if nudge_count < MAX_NO_WORKFLOW_NUDGES:
            ctx.no_workflow_nudge_count = nudge_count + 1
            return _decision(config, "post_no_workflow_delivery")

    workflow_tested_ok = (
        getattr(ctx, "last_test_ok", None) is True
        and getattr(ctx, "update_workflow_called", False)
        and getattr(ctx, "test_after_update_done", False)
    )
    if workflow_tested_ok:
        block_count = getattr(ctx, "last_update_block_count", None)
        # ctx.user_message is set by the agent orchestrator in a later stack PR
        # (06c). The getattr default keeps this gate working on partial stacks.
        user_message = getattr(ctx, "user_message", "")
        completion_contract = _request_completion_contract(ctx)
        if (
            isinstance(block_count, int)
            and not _completion_contract_unknown_due_to_policy_fallback(ctx)
            and _goal_likely_needs_more_blocks(user_message, block_count, completion_contract)
        ):
            nudge_count = getattr(ctx, "coverage_nudge_count", 0)
            if nudge_count < MAX_INTERMEDIATE_NUDGES:
                ctx.coverage_nudge_count = nudge_count + 1
                return _decision(config, "post_intermediate_success")

    if _is_progress_narration(parsed.get("user_response")):
        nudge_count = getattr(ctx, "format_nudge_count", 0)
        if nudge_count < MAX_FORMAT_NUDGES:
            ctx.format_nudge_count = nudge_count + 1
            return _decision(config, "post_format")

    return None


def _consume_pending_screenshots(ctx: Any) -> dict[str, Any] | None:
    """Drain pending_screenshots into a synthetic user message with images.

    Tool results stay text-only because OpenAI rejects images in tool
    messages, so screenshots are delivered as a follow-up user message.
    """
    pending = getattr(ctx, "pending_screenshots", None)
    if not isinstance(pending, list) or not pending:
        return None
    screenshots: list[ScreenshotEntry] = list(pending)
    pending.clear()
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                SCREENSHOT_SENTINEL + "Here is the screenshot from the tool result. "
                "Analyze it to understand the current browser state."
            ),
        },
    ]
    for entry in screenshots:
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{entry.mime};base64,{entry.b64}",
                "detail": "high",
            }
        )
    return {"role": "user", "content": content}


def _needs_explore_without_workflow_nudge(ctx: Any) -> bool:
    """Return True when the agent navigated and observed but never engaged the workflow path."""
    if not getattr(ctx, "navigate_called", False):
        return False
    if not getattr(ctx, "observation_after_navigate", False):
        return False
    if getattr(ctx, "update_workflow_called", False):
        return False
    if getattr(ctx, "test_after_update_done", False):
        return False
    nudge_count = getattr(ctx, "explore_without_workflow_nudge_count", 0)
    return nudge_count < MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES


def _repair_obligation_live(ctx: AgentContext) -> bool:
    """The typed obligation, bounded. It is discharged by evidence, but must not outlive the
    evidence that produced it: a failure that looks repairable and is not would otherwise be
    re-nudged until the turn budget dies, burying the blocker the model is trying to report."""
    if not diagnosis_repair_obligation_open(ctx):
        return False
    return _get_int(ctx, "repair_obligation_nudge_count") < MAX_REPAIR_OBLIGATION_NUDGES


def _repair_obligation_blocks_finalize(ctx: AgentContext, result: RunResultStreaming | None) -> bool:
    """True when the turn is about to end while the typed contract still says the failure is repairable.

    The nudge counters bound *repetition of a nudge*; they were never evidence that the failure had been
    addressed, so once they ran out a turn could finalize a draft its own build test disproved. Ending is
    still bounded — by the total-turn timeout and max-turns, both of which exit as WIP rather than as an
    accept-ready proposal."""
    if not _repair_obligation_live(ctx):
        return False
    parsed = _parse_normalized_final_response(result)
    # ASK_QUESTION is a legitimate exit: it needs the user, not another repair round.
    return parsed is not None and parsed.get("type") == "REPLY"


def _needs_failed_test_nudge(ctx: Any) -> bool:
    """Return True when the last test failed and the agent hasn't iterated yet."""
    # A permanent nav error cannot be 'fix the workflow and retry' material —
    # the dedicated non-retriable branch in enforcement_decision owns this case.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return False
    if getattr(ctx, "pending_reconciliation_requires_user_input", False) is True:
        return False
    if getattr(ctx, "last_test_ok", None) is not False:
        return False
    if not getattr(ctx, "test_after_update_done", False):
        return False
    nudge_count = getattr(ctx, "failed_test_nudge_count", 0)
    return nudge_count < MAX_FAILED_TEST_NUDGES


def _needs_inspect_before_repair_nudge(ctx: Any) -> bool:
    """True when a failed run is repairable and the reached page is not yet observed.

    Routes the first post-failure move to observing the reached page before
    re-authoring, instead of guessing a new block goal and re-running blind.
    """
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    if contract is None:
        return False
    if contract.repair_decision.next_action is not RepairNextAction.REPAIR:
        return False
    if not contract.diagnosis_input.browser_page_state.get("has_current_url"):
        return False
    return not _has_post_failed_run_page_observation(ctx)


def _has_post_failed_run_page_observation(ctx: AgentContext) -> bool:
    if getattr(ctx, "post_run_page_observation_after_failed_test", False) is not True:
        return False
    tool = getattr(ctx, "post_run_page_observation_tool", None)
    if tool not in _POST_RUN_PAGE_OBSERVATION_TOOLS:
        return False
    observed_run_id = getattr(ctx, "post_run_page_observation_workflow_run_id", None)
    current_run_id = getattr(ctx, "last_run_blocks_workflow_run_id", None)
    return bool(isinstance(observed_run_id, str) and observed_run_id and observed_run_id == current_run_id)


def _parse_normalized_final_response(result: RunResultStreaming | None) -> dict[str, Any] | None:
    if result is None:
        return None
    parsed = parse_final_response(extract_final_text(result))
    normalized_scaffolding = normalize_response_scaffolding(
        str(parsed.get("type") or "REPLY"),
        str(parsed.get("user_response") or ""),
    )
    if normalized_scaffolding.changed:
        parsed = {
            **parsed,
            "type": normalized_scaffolding.response_type,
            "user_response": normalized_scaffolding.user_response or "Done.",
        }
    return parsed


def _post_run_observed_reply_can_finalize(ctx: AgentContext, result: RunResultStreaming | None) -> bool:
    if not _has_post_failed_run_page_observation(ctx):
        return False
    parsed = _parse_normalized_final_response(result)
    if parsed is None or parsed.get("type") != "REPLY":
        return False
    user_response = parsed.get("user_response")
    return isinstance(user_response, str) and bool(user_response.strip()) and not _is_progress_narration(user_response)


def _needs_suspicious_success_nudge(ctx: Any) -> bool:
    """Return True when the last test 'completed' but data blocks had no output."""
    if _typed_terminal_challenge_outcome(ctx) is not None:
        return False
    # A non-retriable nav failure cannot be "suspiciously successful" — defer
    # to the dedicated stop path rather than competing for the nudge slot.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return False
    if not getattr(ctx, "last_test_suspicious_success", False):
        return False
    nudge_count = getattr(ctx, "suspicious_success_nudge_count", 0)
    return nudge_count < MAX_SUSPICIOUS_SUCCESS_NUDGES


def _needs_per_tool_budget_nudge(ctx: Any) -> bool:
    if getattr(ctx, "last_failure_category_top", None) != PER_TOOL_BUDGET_FAILURE_CATEGORY:
        return False
    return _get_int(ctx, "per_tool_budget_nudge_count") < MAX_PER_TOOL_BUDGET_NUDGES


def _get_int(ctx: Any, name: str, default: int = 0) -> int:
    value = getattr(ctx, name, default)
    return value if isinstance(value, int) else default


def _repeated_frontier_failure_nudge(ctx: Any) -> str | None:
    """Return the nudge key for each escalation level, at most once per streak.
    The streak itself keeps climbing on further identical failures (incremented
    elsewhere by update_repeated_failure_state), so the stop nudge fires
    naturally on the next repeat after a warn."""
    # Non-retriable nav errors get their own dedicated stop path; don't let a
    # repeated-frontier nudge smuggle different retry advice past the gate.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return None
    streak = _get_int(ctx, "repeated_failure_streak_count")
    emitted = _get_int(ctx, "repeated_failure_nudge_emitted_at_streak")
    top_category = getattr(ctx, "last_failure_category_top", None)
    is_param_binding = top_category == "PARAMETER_BINDING_ERROR"

    if streak >= REPEATED_FRONTIER_STREAK_STOP_AT and emitted < REPEATED_FRONTIER_STREAK_STOP_AT:
        return "post_parameter_binding_stop" if is_param_binding else "post_repeated_frontier_failure_stop"
    if streak >= REPEATED_FRONTIER_STREAK_ESCALATE_AT and emitted < REPEATED_FRONTIER_STREAK_ESCALATE_AT:
        return "post_parameter_binding_warn" if is_param_binding else "post_repeated_frontier_failure_warn"
    return None


STOP_LEVEL_FRONTIER_RULES = frozenset({"post_repeated_frontier_failure_stop", "post_parameter_binding_stop"})


def _non_retriable_nav_error_nudge(ctx: Any) -> tuple[str, str] | None:
    """Fire the non-retriable nav-error stop at most once per distinct signature.
    Returns ``(rule, signature)`` when it should fire, ``None`` otherwise.
    Signature normalization is shared with
    `failure_tracking.compute_failure_signature`, so a cert error after a DNS
    error (or vice versa) counts as a distinct signature and re-fires."""
    raw = getattr(ctx, "last_test_non_retriable_nav_error", None)
    if not isinstance(raw, str) or not raw:
        return None
    signature = normalize_failure_reason(raw)
    last_emitted = getattr(ctx, "non_retriable_nav_error_last_emitted_signature", None)
    if signature == last_emitted:
        return None
    return "post_non_retriable_nav_error_stop", signature


def enforcement_decision(
    ctx: Any,
    result: RunResultStreaming | None = None,
    config: CopilotConfig | None = None,
) -> EnforcementDecision | None:
    """Resolve which enforcement rule fires for this iteration, if any.

    The ladder below is ordered: the first rule whose condition holds wins, and
    that precedence is the contract callers depend on.
    """
    verified = outcome_fully_verified(ctx)
    # Terminal failure-mode signals must pre-empt tool-call hygiene nudges.
    terminal_signal = getattr(ctx, "latest_tool_blocker_signal", None) or getattr(ctx, "blocker_signal", None)
    if terminal_signal is not None:
        stash_turn_halt_from_blocker_signal(ctx, terminal_signal, source="enforcement_backstop")
    raise_if_turn_halt(ctx, verified=verified)
    _raise_if_unrecoverable_contract_stop(ctx)

    # A pending credential pause pre-empts every hygiene nudge below, not just
    # the failed-test one: a credential-blocked update_and_run_blocks call
    # satisfies post_update (test not run) and, when the diagnosis contract
    # is the source, the generic failed-test nudge too. None of those nudges
    # can be acted on without the credential the pause is about to ask for.
    if credential_pause_would_fire(ctx, config):
        return None

    # A permanent navigation error (DNS / cert / SSL / invalid URL) cannot be
    # resolved by observing a prior navigate or by testing an updated
    # workflow against the same bad URL, so let it speak first.
    non_retriable = _non_retriable_nav_error_nudge(ctx)
    if non_retriable is not None:
        rule, signature = non_retriable
        ctx.non_retriable_nav_error_last_emitted_signature = signature
        return _decision(config, rule)

    if ctx.navigate_called and not ctx.observation_after_navigate and not ctx.navigate_enforcement_done:
        ctx.navigate_enforcement_done = True
        return _decision(config, "post_navigate")

    if _needs_explore_without_workflow_nudge(ctx):
        ctx.explore_without_workflow_nudge_count += 1
        return _decision(config, "post_explore_without_workflow")

    if (
        ctx.update_workflow_called
        and not ctx.test_after_update_done
        and getattr(ctx, "allow_untested_workflow_draft", False) is not True
    ):
        return _decision(config, "post_update")

    # Observing the reached page is the first repair move, not the last: while the typed contract
    # still says REPAIR, a reply that reports the failure instead of acting on it re-enters the loop.
    if not _repair_obligation_live(ctx) and _post_run_observed_reply_can_finalize(ctx, result):
        return None

    _maybe_stash_terminal_challenge_halt(ctx)
    raise_if_turn_halt(ctx, verified=verified)

    # If the last run had confirmed challenge evidence, do not misdiagnose a
    # challenge-solving loop as a long-chain budgeting problem.
    if _needs_failed_test_nudge(ctx) and getattr(ctx, "last_test_anti_bot", None):
        ctx.failed_test_nudge_count += 1
        return _decision(config, "post_anti_bot_failed_test")

    # A budget-trip without challenge evidence is a structural problem (chain
    # too long), not a workflow-shape problem — emit the targeted "split the
    # chain" advice before the generic repeated-frontier and failed-test paths
    # can fire.
    if _needs_per_tool_budget_nudge(ctx):
        prior = _get_int(ctx, "per_tool_budget_nudge_count")
        ctx.per_tool_budget_nudge_count = prior + 1
        # First budget trip earns one smaller-frontier retry. A second consecutive trip
        # (the shrunk frontier ALSO blew the budget) is a doomed shrinking-budget spiral on a
        # too-heavy page — finalize the verified prefix instead of re-running into less time.
        if prior >= 1:
            return _decision(config, "post_per_tool_budget_stop")
        return _decision(config, "post_per_tool_budget")

    frontier_rule = _repeated_frontier_failure_nudge(ctx)
    if frontier_rule is not None:
        # Latch the emitted level so each escalation fires at most once per streak.
        ctx.repeated_failure_nudge_emitted_at_streak = (
            REPEATED_FRONTIER_STREAK_STOP_AT
            if frontier_rule in STOP_LEVEL_FRONTIER_RULES
            else REPEATED_FRONTIER_STREAK_ESCALATE_AT
        )
        return _decision(config, frontier_rule)

    # Do NOT clear last_test_suspicious_success here. tools._record_run_blocks_result
    # resets it on every new run; if the agent ignores the nudge and answers
    # without rerunning, we want enforcement_decision to re-emit the nudge.
    if _needs_suspicious_success_nudge(ctx):
        ctx.suspicious_success_nudge_count = getattr(ctx, "suspicious_success_nudge_count", 0) + 1
        return _decision(config, "post_suspicious_success")

    # Checked before the generic failed-test nudge so a scrape-wall streak
    # emits the specific STOP text and does not also consume a
    # failed_test_nudge_count slot.
    if _needs_failed_test_nudge(ctx):
        ctx.failed_test_nudge_count += 1
        if _needs_inspect_before_repair_nudge(ctx):
            return _decision(config, "post_failed_test_inspect_first")
        return _decision(config, "post_failed_test")

    # Counters exhausted but the contract still says REPAIR: keep steering rather than finalize a
    # draft the build test disproved.
    if _repair_obligation_blocks_finalize(ctx, result):
        ctx.repair_obligation_nudge_count = _get_int(ctx, "repair_obligation_nudge_count") + 1
        return _decision(config, "post_failed_test")

    # Response-time gate: peek at the model's final output to tell ASK_QUESTION
    # (always allowed) from a REPLY with a coverage gap or progress-narration.
    # Only runs when no state-based nudge fired.
    if result is not None:
        parsed = _parse_normalized_final_response(result)
        if parsed is None:
            return None
        return _response_coverage_nudge(ctx, parsed, config)

    return None


def _item_field(item: Any, name: str) -> Any:
    """Read *name* from an item that can be either a dict or an attr-style object."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def is_screenshot_message(item: Any) -> bool:
    """Return True if the item is a synthetic screenshot user message."""
    if _item_field(item, "role") != "user":
        return False
    content = _item_field(item, "content")
    if isinstance(content, str):
        return content.startswith(SCREENSHOT_SENTINEL)
    if not isinstance(content, list):
        return False
    for block in content:
        text = _item_field(block, "text")
        if isinstance(text, str) and text.startswith(SCREENSHOT_SENTINEL):
            return True
    return False


def _is_nudge_message(item: Any) -> bool:
    """Return True if the item is a synthetic enforcement nudge."""
    if _item_field(item, "role") != "user":
        return False
    content = _item_field(item, "content")
    return isinstance(content, str) and content.startswith(NUDGE_SENTINEL)


def _is_synthesized_offer_message(item: Any) -> bool:
    if _item_field(item, "role") != "user":
        return False
    content = _item_field(item, "content")
    return isinstance(content, str) and content.startswith(SYNTHESIZED_OFFER_SENTINEL)


def is_synthetic_user_message(item: Any) -> bool:
    """Return True if item is a screenshot, nudge, or synthesized-block offer
    (not a real user turn)."""
    return is_screenshot_message(item) or _is_nudge_message(item) or _is_synthesized_offer_message(item)


def collapse_superseded_synthesized_offers(items: list[Any]) -> list[Any]:
    """Drop every synthesized-block offer except the newest: a refreshed offer supersedes its
    predecessors, and offers ride as user messages no other compaction rung touches. Applied on
    every model-input assembly path before token estimation; the opening item is never dropped.
    """
    offer_indices = [i for i, item in enumerate(items) if i > 0 and _is_synthesized_offer_message(item)]
    if len(offer_indices) <= 1:
        return items
    stale = set(offer_indices[:-1])
    dropped_chars = sum(len(_item_field(items[i], "content") or "") for i in stale)
    LOG.info("copilot_superseded_offers_dropped", dropped=len(stale), dropped_chars=dropped_chars)
    return [item for i, item in enumerate(items) if i not in stale]


def _truncated_output_fallback(output: str) -> str:
    return output[:_TOOL_OUTPUT_SUMMARIZE_THRESHOLD] + _TOOL_OUTPUT_TRUNCATION_SUFFIX


def _summarize_tool_output(output: str) -> str:
    """Compress an old function_call_output to a compact JSON synopsis that
    preserves only signal fields (ok/error/status/failure_reason/block labels).
    Falls back to a head-truncation when the output isn't a JSON dict."""
    if not isinstance(output, str) or len(output) <= _TOOL_OUTPUT_SUMMARIZE_THRESHOLD:
        return output

    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return _truncated_output_fallback(output)

    if not isinstance(parsed, dict):
        return _truncated_output_fallback(output)

    synopsis: dict[str, Any] = {}
    if "ok" in parsed:
        synopsis["ok"] = parsed["ok"]
    if parsed.get("error"):
        synopsis["error"] = str(parsed["error"])[:200]

    data = parsed.get("data")
    if isinstance(data, dict):
        code = data.get("code")
        if isinstance(code, str) and code:
            synopsis["code_chars_elided"] = len(code)
        for key in ("overall_status", "workflow_run_id", "failure_reason", "url", "message"):
            val = data.get(key)
            if val is None or val == "":
                continue
            synopsis[key] = val if isinstance(val, (bool, int, float)) else str(val)[:200]

        # Preserve failure_categories — tools._record_run_blocks_result injects
        # these specifically for downstream reasoning about why a test failed.
        categories = data.get("failure_categories")
        if isinstance(categories, list) and categories:
            synopsis["failure_categories"] = categories

        blocks = data.get("blocks")
        if isinstance(blocks, list):
            block_summary: list[dict[str, Any]] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                entry: dict[str, Any] = {"label": block.get("label"), "status": block.get("status")}
                if block.get("failure_reason"):
                    entry["failure_reason"] = str(block["failure_reason"])[:120]
                block_summary.append(entry)
            if block_summary:
                synopsis["blocks"] = block_summary

    synopsis["_summarized"] = "older tool output — only key fields retained"
    try:
        return json.dumps(synopsis, separators=(",", ":"))
    except (TypeError, ValueError):
        return _truncated_output_fallback(output)


def _replace_item_field(item: Any, name: str, new_value: Any) -> Any:
    """Return a copy of *item* with its *name* field replaced.

    For dicts and attr-style objects, always returns a new object — never
    mutates *item* in place. `_prune_input_list` runs over input lists that
    may share references with SDK-owned state (e.g. `result.to_input_list()`
    and `model_data.input`); in-place mutation there would corrupt shared
    state.
    """
    if isinstance(item, dict):
        return {**item, name: new_value}
    try:
        dup = copy.copy(item)
        setattr(dup, name, new_value)
        return dup
    except (AttributeError, TypeError) as exc:
        LOG.debug(
            "Could not rewrite input-list item field; leaving untouched",
            field=name,
            item_type=type(item).__name__,
            error=str(exc),
        )
        return item


def _replace_item_output(item: Any, new_output: str) -> Any:
    return _replace_item_field(item, "output", new_output)


def _summarize_tool_arguments(args_json: str) -> str:
    """Compact the arguments payload of an older tool call so that massive
    inputs (e.g. the full workflow YAML passed to `update_workflow`) don't keep
    bloating replayed context. Short payloads pass through unchanged."""
    if len(args_json) <= _TOOL_OUTPUT_SUMMARIZE_THRESHOLD:
        return args_json
    try:
        parsed = json.loads(args_json)
    except (TypeError, ValueError):
        return args_json[:_SUMMARIZED_TOOL_ARGUMENT_CHAR_CAP] + _TOOL_OUTPUT_TRUNCATION_SUFFIX
    if not isinstance(parsed, dict):
        return args_json[:_SUMMARIZED_TOOL_ARGUMENT_CHAR_CAP] + _TOOL_OUTPUT_TRUNCATION_SUFFIX
    compact: dict[str, Any] = {}
    for key, val in parsed.items():
        if isinstance(val, str) and len(val) > 500:
            compact[key] = f"<{key} truncated: {len(val)} chars>"
        elif isinstance(val, (list, dict)):
            serialized = json.dumps(val, separators=(",", ":"), default=str)
            compact[key] = f"<{key} truncated: {len(serialized)} chars>" if len(serialized) > 500 else val
        else:
            compact[key] = val
    compact["_summarized"] = "older tool call — large fields replaced with size markers"
    try:
        return json.dumps(compact, separators=(",", ":"))
    except (TypeError, ValueError):
        return args_json[:_SUMMARIZED_TOOL_ARGUMENT_CHAR_CAP] + _TOOL_OUTPUT_TRUNCATION_SUFFIX


def log_recent_tool_output_truncation(truncated_count: int, largest_original_chars: int) -> None:
    LOG.warning(
        "copilot_recent_tool_output_truncated",
        truncated_count=truncated_count,
        cap=_RECENT_TOOL_OUTPUT_CHAR_CAP,
        largest_original_chars=largest_original_chars,
    )


def _prune_input_list(items: list[Any]) -> list[Any]:
    """Drop all but the most recent screenshot, compress older tool outputs,
    and summarize the arguments of older tool CALLS so bulky payloads (like
    the full workflow YAML) don't accumulate in replayed context.

    Screenshots collapse to a short text placeholder. function_call_output and
    function_call items keep the last KEEP_RECENT_TOOL_OUTPUTS at full size
    (head-truncated); older ones collapse to JSON synopses.
    """
    items = collapse_superseded_synthesized_offers(items)
    screenshot_indices = [i for i, item in enumerate(items) if is_screenshot_message(item)]
    drop_indices = set(screenshot_indices[:-1])

    fco_indices = [i for i, item in enumerate(items) if _item_field(item, "type") == "function_call_output"]
    recent_fco_set = set(fco_indices[-KEEP_RECENT_TOOL_OUTPUTS:])

    fc_indices = [i for i, item in enumerate(items) if _item_field(item, "type") == "function_call"]
    recent_fc_set = set(fc_indices[-KEEP_RECENT_TOOL_OUTPUTS:])

    result: list[Any] = []
    recent_truncated_count = 0
    recent_truncated_largest = 0
    for i, item in enumerate(items):
        if i in drop_indices:
            result.append({"role": "user", "content": SCREENSHOT_PLACEHOLDER})
            continue

        item_type = _item_field(item, "type")
        if item_type == "function_call_output":
            output = _item_field(item, "output")
            if isinstance(output, str):
                if i in recent_fco_set:
                    if len(output) > _RECENT_TOOL_OUTPUT_CHAR_CAP:
                        new_output = output[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX
                        if new_output != output:
                            recent_truncated_count += 1
                            recent_truncated_largest = max(recent_truncated_largest, len(output))
                    else:
                        new_output = output
                else:
                    new_output = _summarize_tool_output(output)
                if new_output != output:
                    item = _replace_item_output(item, new_output)
        elif item_type == "function_call" and i not in recent_fc_set:
            args = _item_field(item, "arguments")
            if isinstance(args, str):
                new_args = _summarize_tool_arguments(args)
                if new_args != args:
                    item = _replace_item_field(item, "arguments", new_args)

        result.append(item)
    if recent_truncated_count:
        log_recent_tool_output_truncation(recent_truncated_count, recent_truncated_largest)
    return result


def _sanitize_for_token_estimation(value: Any) -> tuple[Any, int]:
    """Build a sanitized copy of *value*, replacing base64 image data with
    a short placeholder so blobs don't inflate the token count.

    Returns ``(sanitized_value, image_count)``.
    """
    if isinstance(value, dict):
        is_image = value.get("type") == "input_image"
        sanitized: dict[str, Any] = {}
        image_count = 1 if is_image else 0
        for key, child in value.items():
            if is_image and key == "image_url":
                sanitized[key] = "[image]"
                continue
            sanitized_child, child_images = _sanitize_for_token_estimation(child)
            sanitized[key] = sanitized_child
            image_count += child_images
        return sanitized, image_count
    if isinstance(value, list):
        sanitized_list: list[Any] = []
        image_count = 0
        for item in value:
            sanitized_item, item_images = _sanitize_for_token_estimation(item)
            sanitized_list.append(sanitized_item)
            image_count += item_images
        return sanitized_list, image_count
    return value, 0


def estimate_tokens(items: list[Any]) -> int:
    """Token estimate for an input list using tiktoken."""
    if not items:
        return 0
    sanitized, image_count = _sanitize_for_token_estimation(items)
    text = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False, default=str)
    return count_tokens(text) + image_count * TOKENS_PER_RESIZED_IMAGE


_AGGRESSIVE_PRUNE_TAIL = 7


def aggressive_prune(items: list[Any]) -> list[Any]:
    """Emergency prune: drop ALL screenshots, keep original message + last ~3
    tool call/output pairs + latest nudge, prioritizing pair-valid history."""
    if not items:
        return items

    tail: list[Any] = []
    for item in reversed(items[1:]):
        if is_screenshot_message(item):
            continue
        tail.append(item)
        if len(tail) >= _AGGRESSIVE_PRUNE_TAIL:
            break
    tail.reverse()
    opening = items[0]
    opening_call_id = _item_field(opening, "call_id")
    seen_call_ids: set[str] = (
        {opening_call_id}
        if _item_field(opening, "type") == "function_call" and isinstance(opening_call_id, str)
        else set()
    )
    retained_tail: list[Any] = []
    orphaned_output_dropped = False
    for item in tail:
        item_type = _item_field(item, "type")
        call_id = _item_field(item, "call_id")
        if item_type == "function_call_output" and call_id not in seen_call_ids:
            orphaned_output_dropped = True
            continue
        retained_tail.append(item)
        if item_type == "function_call" and isinstance(call_id, str):
            seen_call_ids.add(call_id)

    LOG.info(
        "copilot_aggressive_prune_pair_validity",
        retained_tail=[_item_field(item, "type") for item in retained_tail],
        orphaned_output_dropped=orphaned_output_dropped,
    )
    return [opening, *retained_tail]


def _is_context_window_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    # Match OpenAI's explicit code/phrase variants. Avoid loose substrings like
    # "max_tokens" which also appear in max_tokens_per_request quota errors.
    return (
        "context_length_exceeded" in msg
        or "context window" in msg
        or "maximum context length" in msg
        or "reduce the length of the messages" in msg
    )


_NUDGE_TYPE_BY_KEY: dict[str, str] = {
    "post_update": "post_update",
    "post_navigate": "post_navigate",
    "post_explore_without_workflow": "explore_without_workflow",
    "post_suspicious_success": "suspicious_success",
    "post_repeated_frontier_failure_warn": "repeated_frontier_failure_warn",
    "post_repeated_frontier_failure_stop": "repeated_frontier_failure_stop",
    "post_non_retriable_nav_error_stop": "non_retriable_nav_error_stop",
    "post_parameter_binding_warn": "parameter_binding_warn",
    "post_parameter_binding_stop": "parameter_binding_stop",
    "post_anti_bot_failed_test": "anti_bot_block",
    "post_per_tool_budget": "per_tool_budget_split",
    "post_per_tool_budget_stop": "per_tool_budget_stop",
    "post_no_workflow_delivery": "no_workflow_delivery",
    "post_discovery_entrypoint_url_question": "discovery_entrypoint_url_question",
    "pre_discovery_url_question": "pre_discovery_url_question",
    "post_failed_test": "post_failed_test",
    "post_failed_test_inspect_first": "post_failed_test_inspect_first",
    "screenshot_dropped": "screenshot_dropped_on_recovery",
    "post_intermediate_success": "intermediate_success",
    "post_format": "format",
    # Self-mapped so the table enumerates every emittable rule; these two carry a
    # hardcoded message and so have no nudge key to shorten.
    "present_completion_contract_ask_retry": "present_completion_contract_ask_retry",
    "typed_ask_subject_auto_answer": "typed_ask_subject_auto_answer",
}


def _strip_input_images(current_input: str | list) -> tuple[str | list, bool]:
    """Replace ``input_image`` parts in *current_input* with a text placeholder.

    Used on context-overflow retry to ensure a freshly injected screenshot
    payload doesn't re-trigger the same failure. Returns ``(pruned, stripped)``
    where ``stripped`` is True iff at least one image was removed — the caller
    uses that to warn the agent not to reason about the page from memory.
    """
    if not isinstance(current_input, list):
        return current_input, False
    stripped_any = False
    result: list[Any] = []
    for item in current_input:
        if not isinstance(item, dict):
            result.append(item)
            continue
        content = item.get("content")
        if not isinstance(content, list):
            result.append(item)
            continue
        new_content: list[Any] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "input_image":
                new_content.append({"type": "input_text", "text": SCREENSHOT_PLACEHOLDER})
                stripped_any = True
            else:
                new_content.append(part)
        result.append({**item, "content": new_content})
    return result, stripped_any


async def _recover_from_context_overflow(session: Any, current_input: str | list) -> tuple[str | list, bool]:
    """Aggressively prune the working context (session + current turn input) so
    the next Runner.run_streamed call fits within the context window.

    Strips images from *current_input* regardless of session state: a freshly
    injected screenshot payload is the most likely cause of overflow on the
    session-backed path, where session history is already filter-bounded.

    Returns ``(recovered_input, images_stripped)``.
    """
    stripped_any = False
    stripped_input: str | list
    if isinstance(current_input, list):
        image_free, stripped_any = _strip_input_images(current_input)
        if isinstance(image_free, list) and session is None:
            stripped_input = aggressive_prune(image_free)
        else:
            stripped_input = image_free
    else:
        stripped_input = current_input

    if session is not None:
        all_items = await session.get_items()
        pruned = aggressive_prune(all_items)
        await session.clear_session()
        await session.add_items(pruned)
        return stripped_input, stripped_any
    if isinstance(stripped_input, list):
        return stripped_input, stripped_any
    raise RuntimeError("Cannot recover from context overflow: no session and input is not a list")


class _SendTrackingStream:
    """Wraps EventSourceStream to report whether any frame was sent.

    Used to decide whether an overflow-retry would duplicate SSE frames: if
    the provider raises before the first successful ``.send()``, retry is
    safe. Otherwise the client has already seen partial output and the caller
    must re-raise rather than retry.
    """

    def __init__(self, inner: EventSourceStream) -> None:
        self._inner = inner
        self.emitted = False

    async def send(self, data: Any) -> bool:
        ok = await self._inner.send(data)
        if ok:
            self.emitted = True
        return ok

    async def is_disconnected(self) -> bool:
        return await self._inner.is_disconnected()

    async def close(self) -> None:
        await self._inner.close()


def _accumulate_usage(result: RunResultStreaming, ctx: Any) -> None:
    """Sum the SDK's per-iteration usage into ``ctx``.

    The SDK aggregates usage into ``context_wrapper.usage`` before tool execution,
    so prior-turn tokens survive a mid-tool abort; each ``Runner.run_streamed``
    call gets a fresh wrapper, so totals must accumulate on ``ctx`` across
    iterations rather than overwrite.
    """
    if not hasattr(ctx, "total_tokens_used"):
        return
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage is None:
        return

    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or 0

    if not (input_tokens or output_tokens or total_tokens):
        return

    ctx.input_tokens_used = (ctx.input_tokens_used or 0) + input_tokens
    ctx.output_tokens_used = (ctx.output_tokens_used or 0) + output_tokens
    ctx.total_tokens_used = (ctx.total_tokens_used or 0) + total_tokens


async def _run_streamed_with_deadline(
    agent: Agent,
    current_input: str | list,
    ctx: Any,
    session: Any,
    tracked_stream: _SendTrackingStream,
    runner_kwargs: dict[str, Any],
    start_time: float,
    iteration: int,
) -> Any:
    """Run ``Runner.run_streamed`` + ``stream_to_sse`` with a deadline
    against ``TOTAL_TIMEOUT_SECONDS``.

    The top-of-loop elapsed check only fires between iterations; a
    long-running tool inside ``Runner.run_streamed`` needs ``wait_for``
    to raise ``CopilotTotalTimeoutError`` mid-tool so the caller's
    ``_build_exit_result`` path emits a non-empty REPLY before the
    client's own transport timeout closes the stream.

    ``MIN_DEADLINE_REMAINING_SECONDS`` floors ``remaining`` so
    ``wait_for(timeout=0)`` never panics on an already-spent budget.
    """
    elapsed = _elapsed_run_seconds(ctx, start_time)
    remaining = max(MIN_DEADLINE_REMAINING_SECONDS, TOTAL_TIMEOUT_SECONDS - elapsed)
    result = Runner.run_streamed(agent, input=current_input, context=ctx, session=session, **runner_kwargs)
    try:
        try:
            await asyncio.wait_for(streaming_adapter.stream_to_sse(result, tracked_stream, ctx), timeout=remaining)
        finally:
            _accumulate_usage(result, ctx)
    except asyncio.TimeoutError:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=_elapsed_run_seconds(ctx, start_time), iteration=iteration)
        raise CopilotTotalTimeoutError() from None
    return result


def _maybe_synthesized_block_offer_msg(ctx: Any) -> dict[str, Any] | None:
    """Post-turn fallback offer of a deterministically synthesized code block, in code-only mode.

    Returns a single user message wrapping the synthesized Playwright block, or
    None when the policy/latch/empty-trajectory guards do not hold. Shares the
    latch with the pre-authoring prompt-side offer. The initial offer suppresses
    near-duplicate repeats, but a materially longer scout trajectory can refresh
    the deterministic code before the model authors the workflow.
    """
    extraction_plan = requested_output_extraction_plan(ctx)
    requested_extraction = bool(_requested_output_paths_for_ctx(ctx))
    if requested_extraction and extraction_plan is None:
        return None
    plan_changed = requested_output_extraction_plan_changed(ctx, extraction_plan)
    reopened_after_failed_run = synthesized_persistence_reopened_after_failed_run(ctx)
    reopened = synthesized_persistence_reopened(ctx) or plan_changed
    if getattr(ctx, "update_workflow_called", False) and not reopened:
        return None
    if normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)) != (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ):
        return None
    trajectory = getattr(ctx, "scout_trajectory", None) or []
    if not trajectory:
        return None
    if is_optional_dismissal_only_trajectory(trajectory):
        return None
    trajectory_len = len(trajectory)
    previous_offer_len = getattr(ctx, "synthesized_block_offered_trajectory_len", 0) or 0
    trajectory_goal_complete = synthesized_trajectory_is_goal_complete(ctx)
    known_terminal_actions = _known_non_method_mandated_terminal_actions(ctx)
    business_goal_complete = (
        _trajectory_reaches_post_credential_commit(ctx) if known_terminal_actions else trajectory_goal_complete
    )
    if (
        (known_terminal_actions or _active_floor_rekeyed_runtime_outputs(ctx))
        and _last_scout_credential_fill_index(trajectory) is not None
        and not business_goal_complete
    ):
        return None
    if (
        getattr(ctx, "synthesized_block_offered", False)
        and trajectory_len < previous_offer_len + SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD
        and (not trajectory_goal_complete or getattr(ctx, "synthesized_block_offered_goal_complete", False))
        and not reopened
    ):
        return None
    synthesized = (
        synthesize_code_block_with_extraction(
            trajectory,
            extraction_plan,
            strict_selectors=True,
            reached_download_target=getattr(ctx, "reached_download_target", None),
        )
        if extraction_plan is not None
        else synthesize_code_block(
            trajectory,
            reached_download_target=getattr(ctx, "reached_download_target", None),
        )
    )
    if synthesized is None:
        return None
    if extraction_plan is not None:
        candidate = freeze_requested_output_extraction_candidate(synthesized, extraction_plan, source="generated")
        if candidate is None:
            return None
        existing_candidate = getattr(ctx, "requested_output_extraction_candidate", None)
        if existing_candidate is not None and existing_candidate != candidate and not reopened:
            return None
        ctx.requested_output_extraction_candidate = candidate

    ctx.synthesized_block_offered = True
    ctx.synthesized_block_offered_trajectory_len = trajectory_len
    ctx.synthesized_block_offered_goal_complete = trajectory_goal_complete
    if reopened_after_failed_run:
        ctx.synthesized_block_reopened_after_failed_run = True
    goal = getattr(ctx, "block_goal_main_goal", "") or getattr(ctx, "user_message", "") or ""
    offer_text = render_synthesized_offer_text(synthesized, trajectory, goal=goal)
    missing_steps = _get_scouted_spine_missing_steps_for_halt(ctx)
    if missing_steps:
        offer_text += f"\n\n**Note:** This draft is missing these demonstrated steps: {missing_steps}"
    return {"role": "user", "content": offer_text}


def _completion_verification_unsatisfied(ctx: Any) -> bool:
    result = getattr(ctx, "completion_verification_result", None)
    if result is None or getattr(result, "status", None) != "evaluated":
        return False
    is_fully_satisfied = getattr(result, "is_fully_satisfied", None)
    if callable(is_fully_satisfied) and is_fully_satisfied():
        return False
    return True


def _last_scout_interaction_commits(trajectory: list[Any]) -> bool:
    if not trajectory:
        return False
    last = trajectory[-1]
    if not isinstance(last, dict):
        return False
    return str(last.get("tool_name") or "") in _SYNTHESIZED_BLOCK_COMMIT_TOOLS and not is_generic_entry_opener_click(
        last
    )


def synthesized_persistence_reopened_after_failed_run(ctx: Any) -> bool:
    if getattr(ctx, "synthesized_block_reopened_after_failed_run", False):
        return True
    if not getattr(ctx, "update_workflow_called", False):
        return False
    if not getattr(ctx, "test_after_update_done", False):
        return False
    if getattr(ctx, "last_test_ok", None) is not False:
        return False
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return False
    if not _completion_verification_unsatisfied(ctx):
        return False
    trajectory = getattr(ctx, "scout_trajectory", None)
    if not isinstance(trajectory, list):
        return False
    previous_offer_len = getattr(ctx, "synthesized_block_offered_trajectory_len", 0) or 0
    if len(trajectory) <= previous_offer_len:
        return False
    return _last_scout_interaction_commits(trajectory)


def synthesized_offer_reopened_for_extraction_plan(
    ctx: AgentContext, plan: RequestedOutputExtractionPlan | None
) -> bool:
    """Whether a newly provable read is worth reopening a closed offer for.

    The offer is made once per authoring, but the value a turn was asked for often only becomes
    provable after the draft exists — the page carrying it is reached later. Without this the proven
    read is derived and never offered, and the draft's own guess is what the first run executes.
    Reopening stops as soon as the authored block already carries this plan, so it cannot loop.

    A page whose wording never matches the request binds no plan at all, and the relations it does
    offer are named only by the offer this gate guards; requiring a bound plan to reopen therefore
    asked the read that makes the plan bind to already exist. Unbound candidates reopen it too, and
    stop doing so once one has been read into a requested path.
    """
    if plan is None:
        requested = _requested_output_paths_for_ctx(ctx)
        if not requested or not unbound_candidate_relations(ctx.flow_evidence):
            return False
        # The offer names relations no read has claimed, so it stops once one of them has been read
        # into a requested path. Without this the prompt re-carries the whole offer on every build.
        return not (requested & set(_witness_values_for_derivation(ctx)))
    carried = (ctx.requested_output_extraction_candidate, ctx.pending_requested_output_extraction_candidate)
    return all(candidate is None or candidate.plan_identity != plan.identity for candidate in carried)


def synthesized_persistence_reopened(ctx: AgentContext) -> bool:
    if ctx.synthesized_block_reopened_for_credential_scout:
        return True
    if synthesized_goal_completion_landing_pending(ctx):
        return True
    return synthesized_persistence_reopened_after_failed_run(ctx)


# Intentionally distinct from request_policy._OUTPUT_GENERIC_WORDS: this list filters output-path leaf
# tokens for coverage token matching, so it keeps phrase words the other list drops. Not unified — the
# consumers differ.
_COVERAGE_GENERIC_TOKENS = frozenset(
    {
        "output",
        "value",
        "values",
        "data",
        "result",
        "results",
        "record",
        "records",
        "detail",
        "details",
        "info",
        "information",
        "field",
        "fields",
        "the",
        "of",
    }
)


def _canonical_output_path(path: str) -> str:
    return path if path.startswith("output.") else requested_output_path_for_field(path)


def _active_completion_criteria(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    turn_state = getattr(ctx, "completion_criteria_turn_state", None)
    if turn_state is None or turn_state.decision is None:
        return ()
    return turn_state.decision.criteria


def _coverage_completion_criteria(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    criteria = list(_active_completion_criteria(ctx))
    policy = getattr(ctx, "request_policy", None)
    if isinstance(policy, RequestPolicy):
        known = {(criterion.id, criterion.output_path) for criterion in criteria}
        criteria.extend(
            criterion for criterion in policy.completion_criteria if (criterion.id, criterion.output_path) not in known
        )
    return tuple(criteria)


def _pre_run_gated_completion_criteria(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    """Completion criteria whose requested output is observable before a run. A criterion whose
    evidence comes from an independent run, registered output parameter, or artifact content is
    only confirmable post-run, so gating the scout window on it would demand an unsatisfiable
    pre-run observation. The persist scaffold still demands those paths at author time — that gate
    is SKY-11591's."""
    return tuple(
        criterion
        for criterion in _coverage_completion_criteria(ctx)
        if criterion.requested_output_evidence_source not in _PRE_RUN_UNGATED_EVIDENCE_SOURCES
    )


def _floor_rekeyed_requested_output_paths(ctx: AgentContext) -> set[str]:
    return floor_rekeyed_requested_output_paths(_pre_run_gated_completion_criteria(ctx))


def pre_run_gated_outputs_without_path(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    """Pre-run-gated runtime-output criteria carrying neither an ``output_path`` nor rekey
    provenance, so nothing identifies them and they would drop from the gate unseen."""
    return tuple(
        criterion
        for criterion in _pre_run_gated_completion_criteria(ctx)
        if criterion.kind == "outcome"
        and criterion.level != "definition"
        and not criterion.method_mandated
        and criterion.requested_output_evidence_source == "runtime_output"
        and not criterion.output_path
        and not (criterion.requested_output_floor_rekeyed and criterion.floor_rekeyed_from_path)
    )


def _requested_output_paths_for_ctx(ctx: AgentContext) -> set[str]:
    pre_run_gated_paths = set(requested_output_paths(_pre_run_gated_completion_criteria(ctx)))
    unregisterable = pre_run_gated_outputs_without_path(ctx)
    if unregisterable:
        LOG.warning(
            "copilot_pre_run_gated_output_criterion_without_path",
            count=len(unregisterable),
            criterion_ids=[criterion.id for criterion in unregisterable],
            outcomes=[criterion.outcome[:80] for criterion in unregisterable],
            floor_rekeyed=[criterion.requested_output_floor_rekeyed for criterion in unregisterable],
            floor_rekeyed_from_path=[criterion.floor_rekeyed_from_path for criterion in unregisterable],
        )
    paths = set(pre_run_gated_paths) | _floor_rekeyed_requested_output_paths(ctx)
    repair_context = ctx.last_code_authoring_repair_context
    if repair_context is not None:
        paths.update(
            _canonical_output_path(raw)
            for raw in repair_context.required_goal_value_paths
            if isinstance(raw, str) and raw
        )
    coverage_criteria = _coverage_completion_criteria(ctx)
    independent_run_evidence_paths = {
        _canonical_output_path(criterion.output_path)
        for criterion in coverage_criteria
        if criterion.requested_output_evidence_source == "independent_run_evidence" and criterion.output_path
    }
    non_independent_evidence_paths = {
        _canonical_output_path(criterion.output_path)
        for criterion in coverage_criteria
        if criterion.requested_output_evidence_source != "independent_run_evidence" and criterion.output_path
    }
    independent_only_paths = independent_run_evidence_paths - non_independent_evidence_paths
    paths.difference_update(independent_only_paths)
    return paths


def _requested_output_coverage_tokens(ctx: AgentContext) -> dict[str, frozenset[str]]:
    aliases = schema_output_path_aliases_from_criteria(list(_pre_run_gated_completion_criteria(ctx)))
    tokens_by_path: dict[str, set[str]] = {}
    for alias_key, path in aliases.items():
        tokens_by_path.setdefault(path, set()).update(COVERAGE_TOKEN_RE.findall(alias_key.lower()))
    for path in _requested_output_paths_for_ctx(ctx):
        leaf_tokens = COVERAGE_TOKEN_RE.findall(path.removeprefix("output.").lower())
        tokens_by_path.setdefault(path, set()).update(
            token for token in leaf_tokens if token not in _COVERAGE_GENERIC_TOKENS
        )
    # A rekeyed path is an opaque digest whose leaf would never match the page and would false-match
    # on "request"/"slot", so coverage keys on the outcome text instead.
    for criterion in _pre_run_gated_completion_criteria(ctx):
        if not (criterion.requested_output_floor_rekeyed and criterion.floor_rekeyed_from_path):
            continue
        outcome_tokens = {
            token
            for token in COVERAGE_TOKEN_RE.findall((criterion.outcome or "").lower())
            if token not in _COVERAGE_GENERIC_TOKENS
        }
        if outcome_tokens:
            tokens_by_path[criterion.floor_rekeyed_from_path] = outcome_tokens
    return {
        path: frozenset(token for token in tokens if not token.isdigit()) for path, tokens in tokens_by_path.items()
    }


def _registered_download_deliverable_paths(ctx: AgentContext) -> set[str]:
    return {
        criterion.output_path
        for criterion in _pre_run_gated_completion_criteria(ctx)
        if criterion.declared_deliverable_kind == "registered_download" and criterion.output_path
    }


def download_satisfied_requested_output_paths(ctx: AgentContext) -> set[str]:
    """Requested-output paths a reached download registration satisfies at runtime rather than a
    page-scalar read: the registered-download alias paths plus the paths the classifier declared as
    ``registered_download`` deliverables. Empty unless a download target with a captured selector
    was reached. Author-time seam classification only — it never credits scout coverage."""
    download = ctx.reached_download_target
    if download is None or not download.selector:
        return set()
    requested = _requested_output_paths_for_ctx(ctx)
    # The scout reads page scalars; it can never read a file that exists only once a download fires.
    # So a declared download kind on a path the scout DID cover is a classifier false positive, and the
    # path stays a live-read scalar. The canonical alias paths are download-registered by definition.
    declared = _registered_download_deliverable_paths(ctx) - set(ctx.scouted_output_covered_paths)
    return requested & (REGISTERED_DOWNLOAD_REQUESTED_OUTPUT_PATHS | declared)


def uncovered_requested_output_paths(ctx: AgentContext) -> set[str]:
    """Requested-output paths not yet credited by scouted evidence. A path whose identifying
    tokens are all generic (e.g. ``output.data``) is uncoverable by token match and is exempted,
    so it falls through to the shape heuristic instead of pinning the gate open forever."""
    requested = _requested_output_paths_for_ctx(ctx)
    if not requested:
        return set()
    tokens_by_path = _requested_output_coverage_tokens(ctx)
    covered: set[str] = set(ctx.scouted_output_covered_paths) | download_satisfied_requested_output_paths(ctx)
    return {path for path in requested if path not in covered and tokens_by_path.get(path)}


def _effective_requested_output_path(criterion: CompletionCriterion) -> str | None:
    """The path a requested output is known by, falling back to the identity a slot rekey preserved."""
    if criterion.output_path:
        return criterion.output_path
    if criterion.requested_output_floor_rekeyed and criterion.floor_rekeyed_from_path:
        return criterion.floor_rekeyed_from_path
    return None


def requested_output_paths_for_derivation(ctx: AgentContext) -> set[str]:
    """The paths derivation will try to bind, including floor-rekeyed and repair-context ones.

    The offer and the binder have to read one set. Gating the offer on the policy's criteria alone
    withheld it for a path that reached derivation by rekey, so the page's own candidates were never
    named and the read that would have witnessed the value was never invited (SKY-13226).
    """
    return _requested_output_paths_for_ctx(ctx)


def _requested_output_labels_by_path(ctx: AgentContext) -> dict[str, tuple[str, ...]]:
    requested_paths = _requested_output_paths_for_ctx(ctx)
    labels_by_path: dict[str, tuple[str, ...]] = {}
    for criterion in _pre_run_gated_completion_criteria(ctx):
        label = (criterion.requested_output_label or criterion.outcome).strip()
        path = _effective_requested_output_path(criterion)
        if path in requested_paths and label:
            labels_by_path.setdefault(path, ())
            labels_by_path[path] += (label,)
    return labels_by_path


def _witnessed_values_by_path(ctx: AgentContext) -> dict[str, str]:
    """The scalar each requested output was read as, keyed by the path the read claimed.

    Capture retains every read of a path and defers the choice to synthesis, so this is where that
    choice lives: differing reads resolve to the one value a selectable observation still shows,
    because the page corroborates what was read from it and cannot corroborate a probe's echo. A
    conflict the page corroborates for none, or for more than one, still carries no witness.
    """
    reads: dict[str, list[str]] = {}
    for interaction in ctx.scout_trajectory:
        if interaction.get("tool_name") != "read_value":
            continue
        # A read that inherited its path by elimination says nothing about that path: an early probe
        # of a login form was promoted that way and its JSON became the witness for a metric the page
        # had not shown yet. Only a read that named the path witnesses it (SKY-13226).
        if interaction.get("read_output_path_source") != "declared":
            continue
        path = str(interaction.get("read_output_path") or "")
        value = str(interaction.get("read_result_value") or "")
        if path and value:
            reads.setdefault(path, []).append(value)
    resolved: dict[str, str] = {}
    for path, values in reads.items():
        distinct = list(dict.fromkeys(values))
        if len(distinct) == 1:
            resolved[path] = distinct[0]
            continue
        shown = [
            value
            for value in distinct
            if value_shown_in_selectable_evidence(getattr(ctx, "flow_evidence", None) or [], value)
        ]
        if len(shown) == 1:
            resolved[path] = shown[0]
    return resolved


def dump_derivation_inputs(ctx: AgentContext, *, outcome: str) -> None:
    """Write the inputs a derivation was given, when a local run asks for them.

    Derivation and the synthesis it feeds are both pure, so a live outcome is reproducible offline
    from these values alone; without them every attempt at either costs a full turn. Successes are
    written as well as failures because the code a bound plan generates is only reachable from a
    packet that bound. The evidence carries whatever text the page held, so writing takes both an
    explicit path and a local environment rather than the path alone: a deployed run cannot be
    talked into dumping page contents by its environment.
    """
    directory = os.environ.get("COPILOT_DUMP_DERIVATION_INPUTS")
    if not directory or settings.ENV != "local":
        return
    try:
        os.makedirs(directory, exist_ok=True)
        payload = {
            "outcome": outcome,
            "labels_by_path": {path: list(labels) for path, labels in _requested_output_labels_by_path(ctx).items()},
            "witnessed_by_path": _witnessed_values_by_path(ctx),
            # Scope is authoritative, so a replay that rebuilds it from the labels is not the same
            # derivation the run performed.
            "requested_paths": sorted(_requested_output_paths_for_ctx(ctx)),
            "designations": list(ctx.requested_output_designations),
            "flow_evidence": ctx.flow_evidence,
            "scout_trajectory": list(ctx.scout_trajectory),
        }
        target = os.path.join(directory, f"derivation-{outcome}-{len(ctx.flow_evidence)}-{uuid.uuid4().hex[:8]}.json")
        with open(target, "w") as handle:
            json.dump(payload, handle, default=str)
    except Exception:
        LOG.info("copilot_derivation_input_dump_failed", exc_info=True)


def _current_page_designations(ctx: AgentContext) -> list[dict[str, Any]]:
    """Designations pin coordinates on one page; once the browser moves they describe a page the
    block will not be looking at, so they are dropped rather than compiled into a stale read."""
    designations = ctx.requested_output_designations
    if not designations:
        return []
    page_evidence = ctx.composition_page_evidence or {}
    current_url = str(page_evidence.get("current_url") or "")
    # An unknown current URL is the ordinary state right after designating — the probe stamps the
    # page, but only a composition inspection records one — so it cannot stand in for staleness.
    if not current_url:
        return list(designations)
    return [
        designation
        for designation in designations
        if not str(designation.get("url") or "") or str(designation.get("url")) == current_url
    ]


def _designated_values_by_path(ctx: AgentContext) -> dict[str, str]:
    """The value the model designated and the page confirmed, keyed by the path it fills.

    A designation is a witness the page validated against the live DOM, so it arms the value binder
    without the model having to author an expression that returns exactly one scalar (SKY-13226).
    """
    values: dict[str, str] = {}
    for designation in _current_page_designations(ctx):
        path = designation.get("output_path")
        text = designation.get("text")
        if isinstance(path, str) and isinstance(text, str) and text:
            values[path] = text
    return values


def _witness_values_for_derivation(ctx: AgentContext) -> dict[str, str]:
    """Read-witnessed values, overridden for any path the page confirmed a designation for."""
    return {**_witnessed_values_by_path(ctx), **_designated_values_by_path(ctx)}


def _labels_outranked_by_designation(
    ctx: AgentContext, labels_by_path: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """Drop the lexical channel for a path the model designated on the live page.

    Both channels can bind the same path to different relations, and the designation is the one an
    element was actually resolved from.
    """
    designated = _designated_values_by_path(ctx)
    return {path: labels for path, labels in labels_by_path.items() if path not in designated}


def requested_output_extraction_plan_diagnostic(ctx: AgentContext) -> dict[str, Any]:
    """Why a derivation returned nothing: gate mismatch, or no bindable packet.

    Without both sides plus the trajectory's stamps, an unavailable-plan log cannot separate the two
    and each guess costs a live run.
    """
    requested_paths = _requested_output_paths_for_ctx(ctx)
    labels_by_path = _requested_output_labels_by_path(ctx)
    return {
        "requested_paths": sorted(requested_paths),
        "label_paths": sorted(labels_by_path),
        # The values, not just the keys: a fallen-back outcome sentence and a minted page noun have
        # identical paths, and only the value says which one the binder was actually given.
        "labels_by_path": {path: list(labels) for path, labels in sorted(labels_by_path.items())},
        "paths_match": set(labels_by_path) == requested_paths,
        "designations": len(_current_page_designations(ctx)),
        "bail_reason": derivation_bail_reason(
            flow_evidence=ctx.flow_evidence,
            labels_by_path=labels_by_path,
            witnessed_by_path=_witness_values_for_derivation(ctx),
            requested_paths=requested_paths,
        ),
        "candidate_headings": bindable_candidate_headings(ctx.flow_evidence),
        "flow_evidence_reached_via": [
            str(entry.get("reached_via") or "") for entry in ctx.flow_evidence if isinstance(entry, dict)
        ],
    }


def requested_output_extraction_plan(ctx: AgentContext) -> RequestedOutputExtractionPlan | None:
    requested_paths = _requested_output_paths_for_ctx(ctx)
    if not requested_paths:
        return None
    # Labels are one channel for meeting the request, not the definition of it: withholding the plan
    # unless every requested path carried a label meant a page whose wording the request never uses
    # was refused before the value witness that exists for it could be tried (SKY-13226).
    labels_by_path = _labels_outranked_by_designation(ctx, _requested_output_labels_by_path(ctx))
    plan = derive_requested_output_extraction_plan(
        flow_evidence=ctx.flow_evidence,
        labels_by_path=labels_by_path,
        witnessed_by_path=_witness_values_for_derivation(ctx),
        requested_paths=requested_paths,
    )
    if plan is not None:
        ctx.last_bound_requested_output_extraction_plan = plan
        return plan
    # The structured packet could not carry the designated element — it is truncated, or the value
    # is not a relation the capture models. The probe already pinned it, so read it where it sits.
    designated = plan_from_designations(_current_page_designations(ctx), requested_paths)
    if designated is not None:
        ctx.last_bound_requested_output_extraction_plan = designated
        return designated
    # Derivation reads the freshest packet, and most are truncated or unbindable, so a plan that did
    # bind every requested path is answered with rather than re-derived away before the imposition
    # that needs it. A changed request abandons it: it bound paths the turn no longer asks for.
    retained = ctx.last_bound_requested_output_extraction_plan
    if retained is not None and set(retained.requested_output_paths) == requested_paths:
        return retained
    return None


def unbound_requested_output_paths_for_designation(ctx: AgentContext) -> set[str]:
    """Requested paths no plan has bound yet, so the resolver only decides what is still open.

    Keyed on the bound plan rather than on a witness existing: a declared read can leave a stale or
    unbindable scalar behind, and treating that as settled retires the path while it is still unread
    — the page that finally shows the value would never be offered (SKY-13485).
    """
    requested = _requested_output_paths_for_ctx(ctx)
    if not requested:
        return set()
    plan = ctx.last_bound_requested_output_extraction_plan
    bound = set(plan.requested_output_paths) if plan is not None else set()
    return requested - bound


def requested_scalar_output_extraction_plan(ctx: AgentContext) -> RequestedOutputExtractionPlan | None:
    """Extraction plan over the page-scalar subset of requested outputs (requested minus the
    download-registered paths), for the mixed download+scalar shape whose download half is
    satisfied by execution registration rather than a static keyed read."""
    requested_paths = _requested_output_paths_for_ctx(ctx) - download_satisfied_requested_output_paths(ctx)
    if not requested_paths:
        return None
    labels_by_path: dict[str, tuple[str, ...]] = {}
    for criterion in _pre_run_gated_completion_criteria(ctx):
        outcome = criterion.outcome.strip()
        path = _effective_requested_output_path(criterion)
        if path in requested_paths and outcome:
            labels_by_path.setdefault(path, ())
            labels_by_path[path] += (outcome,)
    plan = derive_requested_output_extraction_plan(
        flow_evidence=ctx.flow_evidence,
        labels_by_path=_labels_outranked_by_designation(ctx, labels_by_path),
        witnessed_by_path=_witness_values_for_derivation(ctx),
        requested_paths=requested_paths,
    )
    if plan is not None:
        return plan
    return plan_from_designations(_current_page_designations(ctx), requested_paths)


def requested_output_extraction_plan_changed(ctx: AgentContext, current: RequestedOutputExtractionPlan | None) -> bool:
    if current is None or len(ctx.flow_evidence) < 2:
        return False
    previous = derive_requested_output_extraction_plan(
        flow_evidence=ctx.flow_evidence[:-1],
        labels_by_path=_labels_outranked_by_designation(ctx, _requested_output_labels_by_path(ctx)),
        witnessed_by_path=_witness_values_for_derivation(ctx),
        requested_paths=_requested_output_paths_for_ctx(ctx),
    )
    return previous is not None and previous.identity != current.identity


def mint_scout_observation_contract_for_ctx(
    ctx: AgentContext,
    page_evidence: dict[str, Any],
    *,
    url: str,
) -> ScoutObservationContract | None:
    labels_by_path = _requested_output_labels_by_path(ctx)
    if not labels_by_path:
        return None
    copilot_config = getattr(ctx, "copilot_config", None)
    shape_registry = copilot_config.requested_output_shape_expectations if copilot_config is not None else None
    shape_expectations_by_path = resolve_shape_expectations_by_path(set(labels_by_path), shape_registry)
    return mint_scout_observation_contract(
        page_evidence,
        labels_by_path=labels_by_path,
        url=url,
        has_bounded_page_schema=has_bounded_page_schema(page_evidence),
        shape_expectations_by_path=shape_expectations_by_path or None,
    )


def record_scouted_output_coverage(
    ctx: AgentContext,
    page_evidence: dict[str, Any],
    *,
    contract: ScoutObservationContract | None = None,
    include_lexical: bool = True,
) -> None:
    lexical_covered: set[str] = set()
    if include_lexical:
        coverage_tokens = _requested_output_coverage_tokens(ctx)
        if coverage_tokens:
            lexical_covered = covered_output_paths_in_result_containers(
                page_evidence.get("result_containers"), coverage_tokens
            )
    contract_covered: set[str] = set()
    bound_paths = scout_observation_bound_paths(contract)
    if bound_paths:
        contract_covered = bound_paths & _requested_output_paths_for_ctx(ctx)
    candidate = lexical_covered | contract_covered
    if not candidate:
        return
    newly_covered = candidate - ctx.scouted_output_covered_paths
    if not newly_covered:
        return
    ctx.scouted_output_covered_paths.update(newly_covered)
    value_grounded = newly_covered & contract_covered
    lexical_new = newly_covered & lexical_covered
    if value_grounded and lexical_new:
        provenance = "both"
    elif value_grounded:
        provenance = "value_grounded"
    else:
        provenance = "lexical"
    LOG.info(
        "copilot_scouted_output_coverage_credited",
        newly_covered_paths=sorted(newly_covered),
        provenance=provenance,
        value_grounded_paths=sorted(value_grounded),
        source_url=page_evidence.get("current_url") or (contract.source_url if contract is not None else "") or "",
    )


def _credential_flow_filled_fields_by_credential(interactions: list[dict[str, Any]]) -> dict[str, set[str]]:
    filled: dict[str, set[str]] = {}
    for item in interactions:
        if str(item.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        field_name = str(item.get("credential_field") or "").strip()
        if field_name not in LIVE_SCOUT_CREDENTIAL_FIELDS:
            continue
        credential_id = str(item.get("credential_id") or "").strip()
        if not credential_id:
            continue
        filled.setdefault(credential_id, set()).add(field_name)
    return filled


def _credential_password_demand_holds(ctx: Any, interactions: list[dict[str, Any]], credential_id: str) -> bool:
    """The password requirement stands until a page observation lands after a post-fill submit that
    ``credential_scout_gap`` itself would credit (fill-source-url matched), and stays whenever that
    latest observed page still shows a password-type control."""
    latest_fill_index = -1
    fill_source_urls: set[str] = set()
    for index, item in enumerate(interactions):
        if (
            str(item.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME
            or str(item.get("credential_id") or "").strip() != credential_id
            or str(item.get("credential_field") or "").strip() not in LIVE_SCOUT_CREDENTIAL_FIELDS
        ):
            continue
        latest_fill_index = index
        source_url = str(item.get("source_url") or "").strip()
        if source_url:
            fill_source_urls.add(source_url)
    submit_index = first_matched_post_fill_submit_index(interactions, latest_fill_index, fill_source_urls)
    if submit_index is None:
        return True
    submit_trajectory_index = interactions[submit_index].get("trajectory_index")
    observed_index = getattr(ctx, "last_scout_observation_trajectory_index", None)
    if (
        not isinstance(submit_trajectory_index, int)
        or not isinstance(observed_index, int)
        or observed_index < submit_trajectory_index
    ):
        return True
    return bool(getattr(ctx, "last_scout_observation_has_password_control", False))


def _credential_flow_scout_gap_incomplete(ctx: Any, trajectory: list[Any]) -> bool:
    """Trajectory- and inventory-scoped mirror of the persist seam's credential scout gate: engaged
    credentials (username/password fills) must have every required field filled plus a post-fill
    submit before the synthesized trajectory may grade goal-complete."""
    interactions = [item for item in trajectory if isinstance(item, dict)]
    filled_by_credential = _credential_flow_filled_fields_by_credential(interactions)
    if not filled_by_credential:
        return False
    raw_inventory = getattr(ctx, "scouted_credential_field_inventory_by_credential_id", None)
    inventory: Mapping[str, frozenset[str]] = raw_inventory if isinstance(raw_inventory, Mapping) else {}
    requirements: list[tuple[frozenset[str], frozenset[str]]] = []
    for credential_id, filled_fields in filled_by_credential.items():
        required_fields = set(filled_fields)
        if "password" in inventory.get(credential_id, frozenset()) and _credential_password_demand_holds(
            ctx, interactions, credential_id
        ):
            required_fields.add("password")
        requirements.append((frozenset({credential_id}), frozenset(required_fields)))
    # requires_submit is always True here: the predicate is deliberately stricter than the persist
    # gate, which demands a submit only when the block's code itself performs one.
    gap = credential_scout_gap(interactions, requirements, requires_submit=True)
    if gap.missing_submit and _active_non_method_mandated_terminal_actions(ctx):
        credential_index = _last_scout_credential_fill_index(interactions)
        if (
            credential_index is not None
            and _first_stable_login_submit_index(interactions, credential_index) is not None
        ):
            return bool(gap.missing_fields)
    return bool(gap.missing_fields) or gap.missing_submit


def _active_non_method_mandated_terminal_actions(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    return tuple(
        criterion
        for criterion in _active_completion_criteria(ctx)
        if criterion.kind == "terminal_action" and not criterion.method_mandated
    )


def _known_non_method_mandated_terminal_actions(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    return tuple(
        criterion
        for criterion in _coverage_completion_criteria(ctx)
        if criterion.kind == "terminal_action" and not criterion.method_mandated
    )


def _active_floor_rekeyed_runtime_outputs(ctx: AgentContext) -> tuple[CompletionCriterion, ...]:
    """Runtime outputs whose exact paths were moved to producer-floor custody.

    They no longer participate in requested-output extraction planning, but they still prove the
    workflow has a business goal beyond authentication. A credential-only trajectory must not use
    the generic fill/commit heuristic to offer a completed block while these outputs are pending.
    """
    return tuple(
        criterion
        for criterion in _active_completion_criteria(ctx)
        if criterion.level == "run"
        and criterion.requested_output_floor_rekeyed
        and bool(criterion.floor_rekeyed_from_path)
        and criterion.requested_output_evidence_source == "runtime_output"
    )


def _trajectory_has_noncredential_business_fill(trajectory: Sequence[Mapping[str, Any]]) -> bool:
    return trajectory_has_browser_fill_interaction(
        [
            interaction
            for interaction in trajectory
            if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME
        ]
    )


def synthesized_trajectory_reaches_goal(ctx: AgentContext) -> bool:
    """The scout trajectory covers an opening click followed by a commit, a durable entry followed by a commit,
    or a reached download target with a selector. Monotone in what the scout captured."""
    trajectory = ctx.scout_trajectory
    if not trajectory:
        return False
    if _active_floor_rekeyed_runtime_outputs(ctx) and not _trajectory_has_noncredential_business_fill(trajectory):
        return False
    if _active_non_method_mandated_terminal_actions(ctx) or (
        _active_floor_rekeyed_runtime_outputs(ctx) and _last_scout_credential_fill_index(trajectory) is not None
    ):
        return _trajectory_reaches_post_credential_commit(ctx)
    if _read_deliverable_reached(ctx):
        return True
    return _trajectory_reaches_generic_goal(ctx, trajectory, include_download=True)


def _read_deliverable_reached(ctx: AgentContext) -> bool:
    """A read deliverable has no commit to reach; the bound requested-output extraction plan is its
    reach evidence. Keys on the retained plan rather than a fresh derivation so reach stays monotone
    per attempt, which is what the ownership latch requires (SKY-13485)."""
    if _request_expects_unreached_download(ctx):
        return False
    # A mandated action is part of what was asked for, so a read that binds while it is still
    # outstanding has not reached the goal. Non-method-mandated ones never arrive here — the caller
    # routes them to the post-credential commit shape first.
    if any(criterion.kind == "terminal_action" for criterion in _active_completion_criteria(ctx)):
        return False
    plan = ctx.last_bound_requested_output_extraction_plan
    if plan is None:
        return False
    requested = _requested_output_paths_for_ctx(ctx)
    return bool(requested) and requested.issubset(set(plan.requested_output_paths))


def _trajectory_reaches_generic_goal(
    ctx: AgentContext,
    trajectory: list[Any],
    *,
    include_download: bool,
    allow_intermediate_interactions: bool = False,
) -> bool:
    """Apply the established download, open-to-commit, and durable-entry reach shapes to one trajectory slice."""
    download = getattr(ctx, "reached_download_target", None)
    if include_download and download is not None and download.selector:
        return True
    opening_trajectory_index: int | None = None
    ordered_pair_candidates = trajectory if allow_intermediate_interactions or len(trajectory) == 2 else []
    for interaction in ordered_pair_candidates:
        if not isinstance(interaction, dict):
            continue
        trajectory_index = interaction.get("trajectory_index")
        if not isinstance(trajectory_index, int):
            continue
        if (
            opening_trajectory_index is not None
            and trajectory_index > opening_trajectory_index
            and str(interaction.get("tool_name") or "") in _SYNTHESIZED_BLOCK_COMMIT_TOOLS
            and not is_generic_entry_opener_click(interaction)
            and not _is_result_surface_navigation_click(interaction)
        ):
            return True
        if opening_trajectory_index is None and str(interaction.get("tool_name") or "") == "click":
            opening_trajectory_index = trajectory_index
    last_entry_index: int | None = None
    for index, item in enumerate(trajectory):
        if isinstance(item, dict) and is_durable_fallback_entry_target(item):
            last_entry_index = index
    if last_entry_index is None:
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("tool_name") or "") in _SYNTHESIZED_BLOCK_COMMIT_TOOLS
        and not is_generic_entry_opener_click(item)
        and not _is_result_surface_navigation_click(item)
        for item in trajectory[last_entry_index + 1 :]
    )


def _is_result_surface_navigation_click(interaction: Mapping[str, Any]) -> bool:
    """A results/list navigation click is not evidence that a business mutation committed."""
    if str(interaction.get("tool_name") or "") != "click":
        return False
    target = " ".join(
        (
            str(interaction.get("selector") or ""),
            str(interaction.get("accessible_name") or ""),
        )
    ).lower()
    if any(token in target for token in ("submit", "confirm", "save", "place-order", "place_order")):
        return False
    selector = str(interaction.get("selector") or "").strip().lower()
    role = str(interaction.get("role") or "").strip().lower()
    if role == "link" or selector.startswith(("a[", "a.", "a#")):
        return True
    return any(token in target for token in ("table", "results", "history", "listing"))


def _request_expects_unreached_download(ctx: AgentContext) -> bool:
    # A registered-download deliverable is confirmable only post-run, so it is absent from the pre-run
    # requested-output gate — a goal-reaching prefix (e.g. sign-in) would otherwise read goal-complete
    # before the scout reaches the download and land the latch on a partial spine.
    download = ctx.reached_download_target
    if download is not None and download.selector:
        return False
    return any(criterion.deliverable_kind == "registered_download" for criterion in _active_completion_criteria(ctx))


def _trajectory_reaches_post_credential_commit(ctx: AgentContext) -> bool:
    """Apply the ordinary reach shapes only to the business spine after the credential submit."""
    trajectory = ctx.scout_trajectory
    if not trajectory:
        return False
    interactions = [item for item in trajectory if isinstance(item, dict)]
    credential_index = _last_scout_credential_fill_index(interactions)
    if credential_index is None:
        return _trajectory_reaches_generic_goal(
            ctx,
            interactions,
            include_download=False,
            allow_intermediate_interactions=True,
        )
    credential_submit_index = credential_submit_boundary_index(interactions, credential_index)
    if credential_submit_index is None:
        return False
    return _trajectory_reaches_generic_goal(
        ctx,
        interactions[credential_submit_index + 1 :],
        include_download=False,
        allow_intermediate_interactions=True,
    )


def reached_terminal_action_criterion_ids(ctx: AgentContext) -> set[str]:
    """Active, non-method-mandated terminal_action criterion ids the scout has structurally reached: empty
    until the post-credential trajectory shows an ordered open->commit pair or durable entry->commit. The
    method_mandated synthetic durable-fill criterion is excluded so a login-only turn never self-releases."""
    if not _trajectory_reaches_post_credential_commit(ctx):
        return set()
    return {criterion.id for criterion in _active_non_method_mandated_terminal_actions(ctx)}


def record_reached_terminal_action_observation(ctx: AgentContext) -> None:
    reached = reached_terminal_action_criterion_ids(ctx)
    if not reached:
        return
    newly_observed = reached - ctx.scout_observed_terminal_criterion_ids
    if not newly_observed:
        return
    ctx.scout_observed_terminal_criterion_ids.update(newly_observed)
    LOG.info("copilot_reached_terminal_action_observed", criterion_ids=sorted(newly_observed))


def _request_expects_unreached_terminal_action(ctx: AgentContext) -> bool:
    # A terminal_action criterion is reached only once the scout observes its downstream page, which no
    # pre-run page scalar evidences; a goal-reaching login prefix would otherwise read goal-complete before
    # the scout crosses into the business spine and land the latch on a login-only trajectory.
    for criterion in _active_non_method_mandated_terminal_actions(ctx):
        if criterion.id not in ctx.scout_observed_terminal_criterion_ids:
            return True
    return False


def synthesized_trajectory_is_goal_complete(ctx: AgentContext) -> bool:
    """A goal-reaching trajectory with no requested-output path left uncovered; an empty requested-output set falls
    through to the reach shape byte-identically, so an entry ``synthesize_code_block`` would drop never counts."""
    if uncovered_requested_output_paths(ctx):
        return False
    if _request_expects_unreached_download(ctx):
        return False
    if _request_expects_unreached_terminal_action(ctx):
        return False
    scalar_paths = _requested_output_paths_for_ctx(ctx) - download_satisfied_requested_output_paths(ctx)
    if scalar_paths:
        plan = requested_scalar_output_extraction_plan(ctx)
        if plan is None or not scalar_paths.issubset(set(plan.requested_output_paths)):
            return False
    if _credential_flow_scout_gap_incomplete(ctx, ctx.scout_trajectory):
        return False
    return synthesized_trajectory_reaches_goal(ctx)


def synthesized_goal_completion_landing_pending(ctx: AgentContext) -> bool:
    """A goal-complete scout trajectory whose spine has not yet landed in a persisted draft. Only the imposition
    seam lands a spine and only an authoring call can leave one unlanded, so both are preconditions."""
    if not ctx.impose_synthesized_code_block:
        return False
    if not ctx.update_workflow_called:
        return False
    if ctx.synthesized_goal_complete_landed:
        return False
    return synthesized_trajectory_is_goal_complete(ctx)


def _has_unconsumed_output_contract_advisory_grant(ctx: Any) -> bool:
    states = getattr(ctx, "output_contract_actuation_by_signature", None)
    if not isinstance(states, dict):
        return False
    return any(state == OutputContractAdvisoryState.GRANTED for state in states.values())


def _should_force_advisory_run_dispatch(ctx: Any) -> bool:
    """Actuate a granted output-contract advisory run through the same tool_choice forcing lane as the
    synthesized-persistence force, rather than leaving dispatch to the model. Fires only while a grant is
    unconsumed, authority permits running blocks, and no genuinely-terminal blocker holds."""
    if not _has_unconsumed_output_contract_advisory_grant(ctx):
        return False
    if not _turn_intent_can_update_and_run_without_user_input(getattr(ctx, "turn_intent", None)):
        return False
    if normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)) != (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ):
        return False
    if getattr(ctx, "turn_halt", None) is not None:
        return False
    return not blocker_signal_is_genuinely_terminal(getattr(ctx, "blocker_signal", None))


def arm_credential_scout_reopen(ctx: AgentContext, identity_digest: str) -> bool:
    """Arm a one-shot scout-window reopen for the first author-time credential-scout reject per
    (structural identity + credential binding) digest. A repeat identical reject returns False and
    falls through so it counts normally toward the repair ceiling."""
    if ctx.credential_scout_rescout_context_key == identity_digest:
        return False
    ctx.credential_scout_rescout_context_key = identity_digest
    ctx.synthesized_block_reopened_for_credential_scout = True
    return True


def _runner_kwargs_with_forced_tool_choice(runner_kwargs: dict[str, Any], tool_name: str) -> dict[str, Any]:
    run_config = runner_kwargs.get("run_config")
    if isinstance(run_config, RunConfig):
        model_settings = run_config.model_settings
        if isinstance(model_settings, ModelSettings):
            forced_settings = replace(model_settings, tool_choice=tool_name)
        else:
            forced_settings = ModelSettings(tool_choice=tool_name)
        return {**runner_kwargs, "run_config": replace(run_config, model_settings=forced_settings)}
    return {**runner_kwargs, "run_config": RunConfig(model_settings=ModelSettings(tool_choice=tool_name))}


def _assemble_enforcement_messages(
    screenshot_msg: dict[str, Any] | None,
    nudge_content: str | None,
    synthesized_msg: dict[str, Any] | None,
) -> list[Any]:
    """Build the extra messages for an enforcement retry, ordered so a nudge, when present, stays last.

    The screenshot rides as its own user-role message because OpenAI rejects image parts inside a tool message.
    """
    extra_msgs: list[Any] = []
    if screenshot_msg is not None:
        extra_msgs.append(screenshot_msg)
    if nudge_content is not None:
        extra_msgs.append({"role": "user", "content": NUDGE_SENTINEL + nudge_content})
    if synthesized_msg is not None:
        extra_msgs.insert(0, synthesized_msg)
    return extra_msgs


async def run_with_enforcement(
    agent: Agent,
    initial_input: str | list,
    ctx: Any,
    stream: EventSourceStream,
    **runner_kwargs: Any,
) -> RunResultStreaming:
    """Run agent with enforcement nudges, preserving conversation history."""
    session = runner_kwargs.pop("session", None)
    copilot_config = runner_kwargs.pop("copilot_config", None) or CopilotConfig()
    current_input: str | list = initial_input
    start_time = time.monotonic()
    ctx.copilot_run_start_monotonic = start_time
    iteration = 0
    pending_recovery_nudge: str | None = None

    while True:
        # Client disconnect is no longer treated as a stop signal. The
        # SSE stream silently drops events once the browser is gone, but
        # the agent keeps running so the reply can be persisted to the
        # chat history on the server side (see SKY-8986).
        elapsed = _elapsed_run_seconds(ctx, start_time)
        if elapsed > TOTAL_TIMEOUT_SECONDS:
            _mark_copilot_total_timeout(ctx, elapsed_seconds=elapsed, iteration=iteration)
            raise CopilotTotalTimeoutError()

        # When the current turn contains image payloads, the session-backed
        # input filter cannot protect us — the payload is in current_input,
        # not in session history. Estimate regardless of session.
        if isinstance(current_input, list):
            est = estimate_tokens(current_input)
            LOG.info("Token estimate before model call", tokens=est, iteration=iteration)
            if est > copilot_config.token_budget:
                LOG.warning(
                    "Token estimate exceeds budget, aggressively pruning",
                    tokens=est,
                    budget=copilot_config.token_budget,
                )
                current_input = aggressive_prune(current_input)

        tracked_stream = _SendTrackingStream(stream)
        with copilot_span(
            "enforcement_iteration",
            data={"iteration": iteration, "elapsed_seconds": round(elapsed, 3)},
        ):
            force_advisory_run_dispatch = _should_force_advisory_run_dispatch(ctx)
            # The advisory-dispatch force claims the actuation ladder itself (same-claimant), so the
            # grant-consumption path can never self-deadlock.
            if force_advisory_run_dispatch:
                claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION)
            current_runner_kwargs = (
                _runner_kwargs_with_forced_tool_choice(runner_kwargs, SYNTHESIZED_BLOCK_PERSISTENCE_TOOL)
                if force_advisory_run_dispatch
                else runner_kwargs
            )
            effective_run_config = current_runner_kwargs.get("run_config")
            effective_model_settings = (
                effective_run_config.model_settings if isinstance(effective_run_config, RunConfig) else None
            )
            turn_intent = getattr(ctx, "turn_intent", None)
            turn_intent_authority = getattr(turn_intent, "authority", None)
            LOG.info(
                "copilot advisory run dispatch force decision",
                force_advisory_run_dispatch=force_advisory_run_dispatch,
                forced_tool_name=(SYNTHESIZED_BLOCK_PERSISTENCE_TOOL if force_advisory_run_dispatch else None),
                chosen_tool_name=(SYNTHESIZED_BLOCK_PERSISTENCE_TOOL if force_advisory_run_dispatch else None),
                turn_intent_mode=getattr(getattr(turn_intent, "mode", None), "value", None),
                turn_intent_may_update_workflow=getattr(turn_intent_authority, "may_update_workflow", None),
                turn_intent_may_run_blocks=getattr(turn_intent_authority, "may_run_blocks", None),
                turn_intent_requires_user_input=getattr(turn_intent_authority, "requires_user_input", None),
                block_authoring_policy=getattr(
                    normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)),
                    "value",
                    None,
                ),
                synthesized_block_offered=getattr(ctx, "synthesized_block_offered", False),
                synthesized_block_offered_trajectory_len=(
                    getattr(ctx, "synthesized_block_offered_trajectory_len", 0) or 0
                ),
                update_workflow_called=getattr(ctx, "update_workflow_called", False),
                effective_tool_choice=getattr(effective_model_settings, "tool_choice", None),
            )
            try:
                result = await _run_streamed_with_deadline(
                    agent,
                    current_input,
                    ctx,
                    session,
                    tracked_stream,
                    current_runner_kwargs,
                    start_time,
                    iteration,
                )
            except asyncio.CancelledError:
                _mark_copilot_total_timeout_if_elapsed(ctx, start_time, iteration)
                raise
            except Exception as e:
                if not _is_context_window_error(e):
                    raise
                if tracked_stream.emitted:
                    # The provider started streaming then aborted; retrying
                    # would double-emit frames to the client.
                    LOG.error(
                        "Context window exceeded after partial emission; not retrying",
                        error=str(e),
                        iteration=iteration,
                        has_session=session is not None,
                    )
                    raise
                LOG.error(
                    "Context window exceeded, retrying with aggressive prune",
                    error=str(e),
                    iteration=iteration,
                    has_session=session is not None,
                )
                try:
                    current_input, images_stripped = await _recover_from_context_overflow(session, current_input)
                except asyncio.CancelledError:
                    _mark_copilot_total_timeout_if_elapsed(ctx, start_time, iteration)
                    raise
                if images_stripped:
                    # The agent could otherwise reason about the page from
                    # memory on the next turn; warn it explicitly.
                    pending_recovery_nudge = _nudge(copilot_config, "screenshot_dropped")
                tracked_stream = _SendTrackingStream(stream)
                try:
                    result = await _run_streamed_with_deadline(
                        agent,
                        current_input,
                        ctx,
                        session,
                        tracked_stream,
                        current_runner_kwargs,
                        start_time,
                        iteration,
                    )
                except asyncio.CancelledError:
                    _mark_copilot_total_timeout_if_elapsed(ctx, start_time, iteration)
                    raise
                except Exception as retry_err:
                    # Never retry twice; even a second overflow surfaces as a
                    # real failure rather than spinning.
                    LOG.error(
                        "Context window recovery retry failed",
                        original_error=str(e),
                        retry_error=str(retry_err),
                        iteration=iteration,
                        has_session=session is not None,
                    )
                    raise

        # The post-run screenshot drain must follow the enforcement check:
        # without a nudge, re-invoking with just the screenshot would replace
        # the agent's already-final REPLY with one synthesized from a single
        # browser frame.
        if pending_recovery_nudge is not None:
            decision: EnforcementDecision | None = EnforcementDecision(
                rule="screenshot_dropped", message=pending_recovery_nudge
            )
            pending_recovery_nudge = None
        else:
            decision = enforcement_decision(ctx, result, copilot_config)

        # The offer is independent of the nudge: a clean scout-then-author turn
        # finalizes with nudge=None, so injecting it only inside the nudge branch
        # would never reach the model. Compute it once here so it rides both the
        # nudge path and the finalize path.
        synthesized_msg = _maybe_synthesized_block_offer_msg(ctx)

        if decision is None:
            # Checked whenever there's no regular nudge, even if a synthesized
            # offer is also pending: a credential-blocked run's diagnosis can
            # coincide with a reopened synthesized-block offer, and the pause
            # must win so the loop doesn't send the offer instead of the card.
            pause_used_before_this_call = getattr(ctx, "credential_pause_used", False)
            resume_msgs = await maybe_credential_pause(ctx, result, stream, copilot_config)
            if resume_msgs is not None:
                current_input = (
                    resume_msgs if session is not None else _prune_input_list(result.to_input_list()) + resume_msgs
                )
                iteration += 1
                continue
            if (
                not pause_used_before_this_call
                and getattr(ctx, "credential_pause_used", False)
                and getattr(ctx, "credential_pause_outcome", None) == "declined"
            ):
                # The latch just flipped on THIS call with no frame ever sent
                # (disconnect, cache gone, or the reason vanished under the
                # async-only checks credential_pause_would_fire's docstring notes
                # it excludes) -- fall back to whatever nudge this iteration would
                # have gotten without the pre-empt, instead of silently finalizing
                # an uncorrected reply. Gated on the latch's own transition (not
                # just the outcome value) so a later iteration's unrelated
                # nudge=None doesn't re-trigger this off a stale "declined".
                decision = enforcement_decision(ctx, result, copilot_config)
            if decision is None and synthesized_msg is None:
                _consume_pending_screenshots(ctx)
                _maybe_raise_non_retriable_nav(ctx)
                return result

        if decision is not None and decision.rule == "post_update":
            if ctx.post_update_nudge_count >= MAX_POST_UPDATE_NUDGES:
                LOG.warning(
                    "Enforcement exhausted post-update nudges, allowing response",
                    nudge_count=ctx.post_update_nudge_count,
                )
                _consume_pending_screenshots(ctx)
                _maybe_raise_non_retriable_nav(ctx)
                return result
            ctx.post_update_nudge_count += 1

        if decision is not None:
            nudge_type = _NUDGE_TYPE_BY_KEY.get(decision.rule, decision.rule)
        else:
            nudge_type = "synthesized_block_offer"
        LOG.info("Enforcement nudge", nudge_type=nudge_type, iteration=iteration)

        # OpenAI rejects images in tool messages, so a queued post-run
        # screenshot rides as its own user message just before the nudge.
        screenshot_msg = _consume_pending_screenshots(ctx)
        if screenshot_msg is not None:
            LOG.info("Injecting screenshot user message", count=len(screenshot_msg["content"]) - 1)

        with copilot_span("enforcement_nudge", data={"nudge_type": nudge_type, "iteration": iteration}):
            extra_msgs = _assemble_enforcement_messages(
                screenshot_msg, decision.message if decision is not None else None, synthesized_msg
            )
            current_input = (
                extra_msgs if session is not None else _prune_input_list(result.to_input_list()) + extra_msgs
            )
        # Signal the narrator that the agent is re-entering the loop after an
        # enforcement correction. stream_to_sse creates the state on the first
        # pass; on later passes we poke the transition latch directly so the
        # next narration (produced after the next tool round-trip) can describe
        # the course-correction.
        narrator_state = getattr(ctx, "narrator_state", None)
        if narrator_state is not None:
            narrator_state.record_transition(TransitionKind.ENFORCEMENT_RETRY)
        iteration += 1
