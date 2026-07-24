"""Enforcement wrapper — nudge agent when it skips required steps."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog
from agents import ModelSettings, RunConfig
from agents.run import Runner

from skyvern.forge.sdk.copilot import config as copilot_config_defaults
from skyvern.forge.sdk.copilot import streaming_adapter
from skyvern.forge.sdk.copilot.blocker_signal import (
    RAW_SECRET_LEAK_REASON_CODE,
    SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
    UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE,
    CopilotToolBlockerSignal,
    clear_tool_blocker_signals_for_reason_codes,
    compose_loop_blocker_user_facing_reason,
    loop_blocker_evidence_from_ctx,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.build_phase import DISCOVERY_FAILURE_STREAK_ESCAPE_THRESHOLD, DISCOVERY_PERMITTED_PHASES
from skyvern.forge.sdk.copilot.build_test_outcome import (
    PostRunPagePathFailure,
    RecordedBuildTestOutcome,
    author_time_reject_missing_output_paths,
    latest_recorded_build_test_outcome_repeated,
    record_build_test_outcome,
    recorded_outcome_from_authoring_repair_context,
    run_backed_repair_evidence_exists,
)
from skyvern.forge.sdk.copilot.challenge_evidence import composition_challenge_carrier
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    CREDENTIAL_FILL_TOOL_NAME,
    LIVE_SCOUT_CREDENTIAL_FIELDS,
    ObligationFinding,
    credential_scout_gap,
    first_matched_post_fill_submit_index,
    freeze_requested_output_extraction_candidate,
    is_durable_fallback_entry_target,
    is_generic_entry_opener_click,
    is_optional_dismissal_only_trajectory,
    locator_selector_literals,
    missing_rung_text,
    normalized_scout_selector,
    obligation_finding_reason_code,
    obligation_finding_selector,
    render_missing_rung_call_sources,
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
    POST_ANTI_BOT_FAILED_TEST_NUDGE,
    POST_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGE,
    POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
    POST_FAILED_TEST_INSPECT_FIRST_NUDGE,
    POST_FAILED_TEST_NUDGE,
    POST_NAVIGATE_NUDGE,
    POST_NO_WORKFLOW_DELIVERY_NUDGE,
    POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE,
    POST_PARAMETER_BINDING_STOP_NUDGE,
    POST_PARAMETER_BINDING_WARN_NUDGE,
    POST_PER_TOOL_BUDGET_NUDGE,
    POST_PER_TOOL_BUDGET_STOP_NUDGE,
    POST_PROBABLE_SITE_BLOCK_STOP_NUDGE,
    POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE,
    POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE,
    POST_SUSPICIOUS_SUCCESS_NUDGE,
    POST_UPDATE_NUDGE,
    PRE_DISCOVERY_URL_QUESTION_NUDGE,
    SCREENSHOT_DROPPED_NUDGE,
    SYNTHESIZED_OFFER_REFRESH_STEP_THRESHOLD,
    BlockAuthoringPolicy,
    CopilotConfig,
    normalize_block_authoring_policy,
)
from skyvern.forge.sdk.copilot.context import (
    AskSubject,
    CodeAuthoringRepairContext,
    coerce_ask_subject,
    parsed_ask_refs,
)
from skyvern.forge.sdk.copilot.credential_pause import credential_pause_would_fire, maybe_credential_pause
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisRepairContract,
    RepairLoopState,
    RepairNextAction,
)
from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY, normalize_failure_reason
from skyvern.forge.sdk.copilot.narration import TransitionKind
from skyvern.forge.sdk.copilot.output_contracts import OutputContractAdvisoryState
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    RequestedOutputExtractionPlan,
    derive_requested_output_extraction_plan,
    resolve_shape_expectations_by_path,
)
from skyvern.forge.sdk.copilot.output_policy import (
    completion_criterion_requires_browser_fill_delivery,
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
    AuthorTimeGateAblationPayload,
    PostRunPagePathInteractionWindow,
    record_author_time_gate_ablation_event,
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
    stash_repair_ceiling_turn_halt,
    stash_turn_halt_from_blocker_signal,
)
from skyvern.forge.sdk.copilot.turn_intent import RequiredContextKey, TurnIntent, TurnIntentMode
from skyvern.forge.sdk.copilot.turn_ownership import (
    ClaimOutcome,
    TurnClaimant,
    claim_and_stash_blocker_signal,
    claim_turn,
    claimant_outranks,
    current_turn_owner,
    emit_blocker_signal_payload,
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
PROBABLE_SITE_BLOCK_STREAK_STOP_AT = 2
# Caps how many times the stop nudge can re-fire — without this, the streak
# stays latched while no new test runs reset it and every subsequent turn
# re-injects the same nudge until MAX_ITERATIONS. Independent of
# PROBABLE_SITE_BLOCK_STREAK_STOP_AT (both default to 2 but tune different
# axes: streak depth vs nudge count).
MAX_PROBABLE_SITE_BLOCK_STOP_NUDGES = 2
# Caps how many times the per-tool-budget split nudge can fire. After two
# trips the agent should already be at single-block granularity; further
# trips fall through to the repeated-frontier escalation path.
MAX_PER_TOOL_BUDGET_NUDGES = 2
# Code-authoring guardrail churn: distinct-reject convergence backstop. An
# accepted persist resets the counter, so this many unaccepted rejections is
# pathological and leaves three free repair attempts before the halt fires —
# far inside both the 900s budget and the SDK max-turns cap.
MAX_CODE_AUTHORING_GUARDRAIL_REJECTS = 4
# Credential-priority rejects defer to the credential-scout message until this
# higher bound, which must stay above MAX_CODE_AUTHORING_GUARDRAIL_REJECTS so the
# non-credential backstop and the low-count credential deferral are untouched.
MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS = 8
# Far under the SDK max-turns cap so the halt arrives first.
MAX_NO_PROGRESS_INTERACTION_ATTEMPTS = 4
_NO_PROGRESS_INTERACTION_REASON_CODES = frozenset({"loop_detected_no_forward_progress_interaction"})
MIN_BLOCKS_FOR_AUTO_COMPLETE = 10
TOTAL_TIMEOUT_SECONDS = 900
# Floor for the per-iteration ``wait_for`` deadline so an already-spent budget
# never yields ``wait_for(timeout=0)`` (which raises immediately). Kept as a
# constant so tests can shrink it instead of paying a full second per deadline.
MIN_DEADLINE_REMAINING_SECONDS = 1.0
# Belt-and-braces cap alongside the elapsed-time budget. Per-nudge caps
# already prevent individual branches from looping; this stops a brand-new
# enforcement rule that forgets its own counter from spinning within 900s.
MAX_ITERATIONS = 50
SCREENSHOT_SENTINEL = "[copilot:screenshot] "
NUDGE_SENTINEL = "[copilot:nudge] "
SCREENSHOT_PLACEHOLDER = SCREENSHOT_SENTINEL + "[prior screenshot removed to save context]"
TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET
SYNTHESIZED_BLOCK_PERSISTENCE_TOOL = "update_and_run_blocks"
_SYNTHESIZED_BLOCK_PERSISTENCE_ALLOWED_TOOLS = frozenset(
    {SYNTHESIZED_BLOCK_PERSISTENCE_TOOL, "fill_credential_field", "update_workflow"}
)
_ACTUATION_OBLIGATION_REQUIRED_FILL_TOOL = "type_text"
_SYNTHESIZED_BLOCK_PERSISTENCE_MUTATING_TOOLS = frozenset(
    {"click", "press_key", "type_text", "select_option", "navigate_browser"}
)
# Both tools re-author the workflow draft and clear the coverage-reopen flag; the steer must fire
# for either or an update_workflow re-author silently spends the one-shot rescout.
_SYNTHESIZED_BLOCK_REAUTHORING_TOOLS = frozenset({SYNTHESIZED_BLOCK_PERSISTENCE_TOOL, "update_workflow"})
_SYNTHESIZED_BLOCK_COMMIT_TOOLS = frozenset({"click", "press_key"})
_POST_RUN_PAGE_PATH_INTERACTION_BUDGET = 4
_LOGIN_SUBMIT_NAME_PATTERN = re.compile(
    r"^(?:log in|login|sign in|authenticate)(?: now| securely| to continue)?$",
    re.I,
)
_LOGIN_SUBMIT_SELECTOR_PATTERN = re.compile(
    r"^(?:(?:log in|login|sign in|authenticate)(?: submit| button| btn)?|"
    r"(?:submit|button|btn) (?:log in|login|sign in|authenticate))$",
    re.I,
)
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
_RECENT_TOOL_OUTPUT_CHAR_CAP = 2000
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


def _probable_site_block_proxy_options(ctx: Any, *, include_whether: bool = True) -> str:
    proxy_label = _effective_proxy_label(ctx)
    if proxy_label is None:
        options = "try a different URL, configure a proxy, or provide an alternate entry point."
        return f"whether to {options}" if include_whether else options
    if proxy_label == "RESIDENTIAL":
        options = (
            "try a different proxy location (for example US-CA or US-NY), use a different "
            "residential/ISP option if supported, or provide an alternate entry point."
        )
        return f"whether to {options}" if include_whether else options
    options = (
        f"try a different proxy/location than {proxy_label}, use a different residential/ISP option if supported, "
        "or provide an alternate entry point."
    )
    return f"whether to {options}" if include_whether else options


def _probable_site_block_stop_nudge(ctx: Any, config: CopilotConfig | None = None) -> str:
    return _nudge(config, "post_probable_site_block_stop_prefix") + _probable_site_block_proxy_options(ctx)


def _probable_site_block_stop_agent_text(ctx: Any, config: CopilotConfig | None = None) -> str:
    return (
        f"{_probable_site_block_stop_nudge(ctx, config)}\n"
        f"Latest internal failure reason: {_single_line_failure_reason(ctx)}"
    )


def _single_line_failure_reason(ctx: Any) -> str:
    reason = getattr(ctx, "last_test_failure_reason", None)
    if not isinstance(reason, str) or not reason.strip():
        return "Skyvern failed to load the website."
    return " ".join(reason.split())


def build_probable_site_block_user_question(ctx: Any) -> str | None:
    """Return a concise user-facing blocker question after the site-block stop nudge."""
    if _get_int(ctx, "probable_site_block_stop_nudge_count") <= 0:
        return None

    failure_reason = _single_line_failure_reason(ctx)
    options = _probable_site_block_proxy_options(ctx, include_whether=False)
    return (
        "The site could not be loaded after repeated attempts. "
        f'The latest failure_reason was: "{failure_reason}". '
        "Repeating the same IP/workflow shape is unlikely to help, so I should stop this path.\n\n"
        f"Would you like me to {options}"
    )


def _probable_site_block_stop_signal(ctx: Any, config: CopilotConfig | None = None) -> CopilotToolBlockerSignal:
    user_facing = build_probable_site_block_user_question(ctx)
    if user_facing is None:
        user_facing = (
            "The site could not be loaded after repeated attempts. Tell me whether to try a different URL, "
            "configure a proxy, or use an alternate entry point."
        )
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=_probable_site_block_stop_agent_text(ctx, config),
        user_facing_reason=user_facing,
        recovery_hint="ask_user_clarifying",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=True,
        renders_final_reply=True,
        internal_reason_code="probable_site_block_stop",
        blocked_tool="update_and_run_blocks",
    )


def _repair_loop_state(ctx: Any) -> RepairLoopState | None:
    contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
    state = getattr(contract, "repair_loop_state", None)
    return state if isinstance(state, RepairLoopState) else None


def _needs_repair_ceiling_halt(ctx: Any) -> bool:
    state = _repair_loop_state(ctx)
    return state is not None and state.ceiling_reached is True and run_backed_repair_evidence_exists(ctx)


def repair_ceiling_stop_signal(
    ctx: Any,
    contract: DiagnosisRepairContract | None,
    config: CopilotConfig | None = None,
) -> CopilotToolBlockerSignal:
    state = contract.repair_loop_state if isinstance(contract, DiagnosisRepairContract) else None
    count = state.consecutive_identical_repair_count if state is not None else 0
    evidence = loop_blocker_evidence_from_ctx(ctx)
    user_facing, _tiers = compose_loop_blocker_user_facing_reason(
        "repair_ceiling_reached", evidence, blocked_tool="update_and_run_blocks"
    )
    agent_steering = (
        f"This repair has made no verified progress across {count} attempts; "
        "stop retrying and report the recorded blocker from the preserved draft."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=evidence.has_draft,
        renders_final_reply=True,
        internal_reason_code="repair_ceiling_reached",
        blocked_tool="update_and_run_blocks",
    )


def code_authoring_churn_stop_signal(ctx: Any) -> CopilotToolBlockerSignal:
    count = _get_int(ctx, "code_authoring_guardrail_reject_count")
    evidence = loop_blocker_evidence_from_ctx(ctx)
    user_facing, _tiers = compose_loop_blocker_user_facing_reason(
        "code_authoring_guardrail_churn", evidence, blocked_tool="update_workflow"
    )
    agent_steering = (
        f"The generated code has been rejected {count} times without an accepted save; "
        "stop rewriting it and report the recorded blocker from the preserved draft."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=evidence.has_draft,
        renders_final_reply=True,
        internal_reason_code="code_authoring_guardrail_churn",
        blocked_tool="update_workflow",
    )


def credential_priority_authoring_churn_stop_signal(ctx: Any) -> CopilotToolBlockerSignal:
    count = _get_int(ctx, "code_authoring_guardrail_reject_count")
    evidence = loop_blocker_evidence_from_ctx(ctx)
    user_facing, _tiers = compose_loop_blocker_user_facing_reason(
        "credential_priority_authoring_churn", evidence, blocked_tool="update_workflow"
    )
    # Older context snapshots may not carry fields added after the snapshot was created.
    reject_reason_codes = getattr(ctx, "last_output_policy_reject_reason_codes", None)
    if reject_reason_codes == frozenset({RAW_SECRET_LEAK_REASON_CODE}):
        agent_steering = (
            "The generated login code embedded the credential's secret value and was refused; "
            "stop rewriting it and report the recorded blocker from the preserved draft."
        )
    else:
        agent_steering = (
            f"The credential-scout gate has rejected the generated code {count} times without an accepted save; "
            "stop rewriting it and report the recorded blocker from the preserved draft."
        )
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=evidence.has_draft,
        renders_final_reply=True,
        internal_reason_code="credential_priority_authoring_churn",
        blocked_tool="update_workflow",
    )


_CHURN_REASON_CODES = frozenset({"code_authoring_guardrail_churn", "credential_priority_authoring_churn"})
_SCOUTED_SPINE_CHECKPOINT_BLOCK_LABEL = "persisted_draft"


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


SCOUTED_SPINE_TURN_HALT_USER_REASON = (
    "I couldn't get past the same problem after several attempts. The saved draft is still missing "
    "steps I demonstrated while scouting, and my rewrites kept leaving them out. "
    "Tell me what to change and I'll try a different approach."
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


def _scouted_spine_turn_end_nudge(ctx: AgentContext) -> str | None:
    try:
        findings = _scouted_spine_open_obligation(ctx)
    except Exception:
        LOG.warning("copilot_scouted_spine_turn_end_check_failed", exc_info=True)
        return None
    if not findings:
        return None
    if ctx.scouted_spine_checkpoint_fired:
        _log_scouted_spine_unresolved(findings, site="turn_end")
        return None
    ctx.scouted_spine_checkpoint_fired = True
    first = findings[0]
    reason_code = obligation_finding_reason_code(first)
    repair_context = CodeAuthoringRepairContext(
        block_label=_SCOUTED_SPINE_CHECKPOINT_BLOCK_LABEL,
        reason_code=reason_code,
        selector=obligation_finding_selector(first),
    )
    ctx.last_code_authoring_repair_context = repair_context
    record_build_test_outcome(ctx, recorded_outcome_from_authoring_repair_context(repair_context))
    _record_code_authoring_guardrail_reject(ctx)
    uncovered = uncovered_rung_records(findings)
    missing_text = _scouted_spine_missing_text(findings)
    LOG.info(
        "copilot_scouted_spine_under_build",
        block_label=_SCOUTED_SPINE_CHECKPOINT_BLOCK_LABEL,
        site="turn_end",
        reason_code=reason_code,
        missing_rung_count=len(uncovered),
        missing_rungs=missing_text,
    )
    nudge = (
        f"The persisted draft under-builds the scouted spine ({reason_code}): "
        f"missing rung(s): {missing_text}. Resubmit the code block through update_workflow so every scouted "
        "rung is replayed — reuse the synthesized code block verbatim."
    )
    artifact = render_missing_rung_call_sources(uncovered)
    if artifact:
        nudge += "\n" + artifact
    return nudge


def _record_code_authoring_guardrail_reject(
    ctx: AgentContext,
    *,
    defer_churn_stop: bool = False,
    frontier_unchanged: bool = False,
    output_policy_reason_codes: frozenset[str] | None = None,
) -> None:
    # Callers record the current build-test outcome first so repeat detection compares that key to history.
    repeated_outcome = latest_recorded_build_test_outcome_repeated(ctx)
    # A frontier-unchanged reject is churn even when sibling edits move the whole-signature key each
    # turn (which reads as a non-repeat); it must accumulate toward the churn stop, not reset.
    if repeated_outcome is False and not frontier_unchanged:
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
    if defer_churn_stop:
        if ctx.code_authoring_guardrail_reject_count < MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS:
            return
        signal: CopilotToolBlockerSignal = credential_priority_authoring_churn_stop_signal(ctx)
    elif ctx.code_authoring_guardrail_reject_count < MAX_CODE_AUTHORING_GUARDRAIL_REJECTS:
        return
    else:
        signal = code_authoring_churn_stop_signal(ctx)
    # A genuinely-terminal held blocker keeps both the rendered reply and the
    # halt kind, so the churn stop defers to it rather than overriding.
    if blocker_signal_is_genuinely_terminal(ctx.blocker_signal):
        return
    claimant = TurnClaimant.CREDENTIAL_PRIORITY_CHURN if defer_churn_stop else TurnClaimant.CODE_AUTHORING_CHURN
    if claim_and_stash_blocker_signal(ctx, claimant, signal, force_stash=True) is None:
        return
    try:
        unresolved_obligation = _scouted_spine_open_obligation(ctx)
    except Exception:
        LOG.warning("copilot_scouted_spine_churn_stop_check_failed", exc_info=True)
        unresolved_obligation = []
    if unresolved_obligation:
        _log_scouted_spine_unresolved(unresolved_obligation, site="churn_stop")


def no_forward_progress_interaction_stop_signal(ctx: Any) -> CopilotToolBlockerSignal:
    count = _get_int(ctx, "consecutive_no_progress_interaction_count")
    evidence = loop_blocker_evidence_from_ctx(ctx)
    user_facing, _tiers = compose_loop_blocker_user_facing_reason(
        "loop_detected_no_forward_progress_interaction", evidence, blocked_tool="click"
    )
    agent_steering = (
        f"Clicking has not advanced the page across {count} attempts; "
        "stop trying new selectors and report the recorded blocker from the preserved draft."
    )
    return CopilotToolBlockerSignal(
        blocker_kind="loop_detected",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint="report_blocker_to_user",
        cleared_by_tools=frozenset(),
        preserves_workflow_draft=evidence.has_draft,
        renders_final_reply=True,
        internal_reason_code="loop_detected_no_forward_progress_interaction",
        blocked_tool="click",
    )


def _needs_no_progress_interaction_halt(ctx: Any) -> bool:
    return _get_int(ctx, "consecutive_no_progress_interaction_count") >= MAX_NO_PROGRESS_INTERACTION_ATTEMPTS


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
    if count < MAX_NO_PROGRESS_INTERACTION_ATTEMPTS:
        return
    if blocker_signal_is_genuinely_terminal(ctx.blocker_signal):
        return
    signal = no_forward_progress_interaction_stop_signal(ctx)
    claim_and_stash_blocker_signal(ctx, TurnClaimant.LOOP_DETECTED, signal, force_stash=True)


def _needs_code_authoring_churn_halt(ctx: Any) -> bool:
    count = _get_int(ctx, "code_authoring_guardrail_reject_count")
    if getattr(ctx, "last_code_authoring_reject_was_credential_priority", False) is True:
        return count >= MAX_CREDENTIAL_PRIORITY_AUTHORING_REJECTS
    return count >= MAX_CODE_AUTHORING_GUARDRAIL_REJECTS


def _churn_signal_if_halting(ctx: Any) -> CopilotToolBlockerSignal | None:
    if not _needs_code_authoring_churn_halt(ctx):
        return None
    if getattr(ctx, "last_code_authoring_reject_was_credential_priority", False) is True:
        return credential_priority_authoring_churn_stop_signal(ctx)
    return code_authoring_churn_stop_signal(ctx)


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
    page_reason = _structured_page_challenge_reason(ctx, evidence)
    if page_reason is None:
        return None
    carrier = composition_challenge_carrier(
        evidence if evidence is not None else getattr(ctx, "composition_page_evidence", None)
    )
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
    if isinstance(single, dict):
        candidates.append(single)
    return candidates


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


class CopilotGoalSatisfied(Exception):
    """Raised when a tool proves the workflow already satisfies the turn."""


class CopilotBuiltUnverified(Exception):
    """Raised when clean tests should stop repair without claiming goal success."""


BUILT_UNVERIFIED_REPAIR_INERT_TERMINAL_REASON = "built_unverified_repair_inert"


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


def _mark_copilot_total_timeout(ctx: Any) -> None:
    ctx.copilot_total_timeout_exceeded = True


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


def _mark_copilot_total_timeout_if_elapsed(ctx: Any, start_time: float) -> None:
    if _elapsed_run_seconds(ctx, start_time) >= TOTAL_TIMEOUT_SECONDS:
        _mark_copilot_total_timeout(ctx)


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


def _present_completion_contract_ask_retry(ctx: CopilotContext, parsed: dict[str, Any]) -> str | None:
    if parsed.get("type") != "ASK_QUESTION":
        return None
    ask_subject = coerce_ask_subject(parsed.get("ask_subject"))
    if ask_subject is not None:
        # A schema ask the contract already answers is redundant whether or not the turn has
        # built anything yet, so it resolves on the base admission without the attempt check.
        if _present_completion_contract_ask_admission_base(ctx):
            auto_answer = _typed_ask_subject_auto_answer(ctx, ask_subject, parsed)
            if auto_answer is not None:
                return auto_answer
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
    return PRESENT_COMPLETION_CONTRACT_ASK_RETRY


def _typed_ask_subject_auto_answer(ctx: CopilotContext, ask_subject: AskSubject, parsed: dict[str, Any]) -> str | None:
    if ask_subject != "output_schema":
        return None
    refs = parsed_ask_refs(parsed.get("refs"))
    if not refs:
        return None
    # Reads the raw policy criteria rather than the turn-active set the other requested-output
    # consumers use: floor-rekey annotations are baked in at request-policy build time, and what
    # the model may cite as refs is rendered from this same set in `prompt_summary`.
    policy = ctx.request_policy
    if not isinstance(policy, RequestPolicy):
        return None
    criteria = policy.graded_completion_criteria()
    requested = requested_output_paths(criteria) | floor_rekeyed_requested_output_paths(criteria)
    if not requested or not set(refs) <= requested:
        return None
    resolved = sorted(set(refs))
    LOG.info(
        "copilot_ask_subject_auto_answered",
        subject=ask_subject,
        resolved_refs=resolved,
    )
    return (
        "The outputs you asked to confirm are already pinned by this request's completion contract. "
        f"Requested output paths: {', '.join(resolved)}. Author and test the workflow to produce them, "
        "choosing a reasonable representation for each, instead of asking the user to re-confirm."
    )


def _nudge(config: CopilotConfig | None, key: str) -> str:
    if config is None:
        return DEFAULT_ENFORCEMENT_NUDGES[key]
    return config.nudge(key)


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
) -> str | None:
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
    return _nudge(config, "pre_discovery_url_question")


def _post_discovery_entrypoint_url_question_nudge(
    ctx: Any,
    parsed: dict[str, Any],
    config: CopilotConfig | None = None,
) -> str | None:
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
    return f"{_nudge(config, 'post_discovery_entrypoint_url_question')} Resolved candidate_url: {candidate_url}"


def _response_coverage_nudge(ctx: Any, parsed: dict[str, Any], config: CopilotConfig | None = None) -> str | None:
    """Peek at the model's final output and return a nudge for coverage gaps
    or progress-narration format. ASK_QUESTION is let through so the agent
    can request missing credentials or disambiguation, except when discovery
    resolved a candidate and the agent has not yet inspected or composed from
    that candidate.

    Returns the nudge string to inject, or None to let the response through.
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
            return _nudge(config, "post_no_workflow_delivery")

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
                return _nudge(config, "post_intermediate_success")

    if _is_progress_narration(parsed.get("user_response")):
        nudge_count = getattr(ctx, "format_nudge_count", 0)
        if nudge_count < MAX_FORMAT_NUDGES:
            ctx.format_nudge_count = nudge_count + 1
            return _nudge(config, "post_format")

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


def _needs_failed_test_nudge(ctx: Any) -> bool:
    """Return True when the last test failed and the agent hasn't iterated yet."""
    # A permanent nav error cannot be 'fix the workflow and retry' material —
    # the dedicated non-retriable branch in _check_enforcement owns this case.
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


def _needs_probable_site_block_stop_nudge(ctx: Any) -> bool:
    """Return True when the site-block-wall streak has reached the stop level
    AND the per-streak nudge cap has not been exhausted."""
    if _get_int(ctx, "probable_site_block_streak_count") < PROBABLE_SITE_BLOCK_STREAK_STOP_AT:
        return False
    return _get_int(ctx, "probable_site_block_stop_nudge_count") < MAX_PROBABLE_SITE_BLOCK_STOP_NUDGES


def _get_int(ctx: Any, name: str, default: int = 0) -> int:
    value = getattr(ctx, name, default)
    return value if isinstance(value, int) else default


def _repeated_frontier_failure_nudge(ctx: Any, config: CopilotConfig | None = None) -> str | None:
    """Emit each escalation level at most once per streak. The streak itself
    keeps climbing on further identical failures (incremented elsewhere by
    update_repeated_failure_state), so the stop nudge fires naturally on the
    next repeat after a warn."""
    # Non-retriable nav errors get their own dedicated stop path; don't let a
    # repeated-frontier nudge smuggle different retry advice past the gate.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return None
    # Defer to the probable-site-block stop path once the wall has been
    # confirmed across ≥ PROBABLE_SITE_BLOCK_STREAK_STOP_AT shape-independent
    # attempts — at that point "try yet another shape" is empirically wrong.
    if _get_int(ctx, "probable_site_block_streak_count") >= PROBABLE_SITE_BLOCK_STREAK_STOP_AT:
        return None
    streak = _get_int(ctx, "repeated_failure_streak_count")
    emitted = _get_int(ctx, "repeated_failure_nudge_emitted_at_streak")
    top_category = getattr(ctx, "last_failure_category_top", None)
    is_param_binding = top_category == "PARAMETER_BINDING_ERROR"

    if streak >= REPEATED_FRONTIER_STREAK_STOP_AT and emitted < REPEATED_FRONTIER_STREAK_STOP_AT:
        return _nudge(
            config,
            "post_parameter_binding_stop" if is_param_binding else "post_repeated_frontier_failure_stop",
        )
    if streak >= REPEATED_FRONTIER_STREAK_ESCALATE_AT and emitted < REPEATED_FRONTIER_STREAK_ESCALATE_AT:
        return _nudge(
            config,
            "post_parameter_binding_warn" if is_param_binding else "post_repeated_frontier_failure_warn",
        )
    return None


def _is_stop_level_frontier_nudge(nudge: str, config: CopilotConfig | None = None) -> bool:
    return nudge in {
        _nudge(config, "post_repeated_frontier_failure_stop"),
        _nudge(config, "post_parameter_binding_stop"),
    }


def _non_retriable_nav_error_nudge(ctx: Any, config: CopilotConfig | None = None) -> tuple[str, str] | None:
    """Emit POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE at most once per distinct
    non-retriable nav-error signature. Returns ``(nudge, signature)`` when it
    should fire, ``None`` otherwise. Signature normalization is shared with
    `failure_tracking.compute_failure_signature`, so a cert error after a DNS
    error (or vice versa) counts as a distinct signature and re-fires."""
    raw = getattr(ctx, "last_test_non_retriable_nav_error", None)
    if not isinstance(raw, str) or not raw:
        return None
    signature = normalize_failure_reason(raw)
    last_emitted = getattr(ctx, "non_retriable_nav_error_last_emitted_signature", None)
    if signature == last_emitted:
        return None
    return _nudge(config, "post_non_retriable_nav_error_stop"), signature


def _check_enforcement(
    ctx: Any,
    result: RunResultStreaming | None = None,
    config: CopilotConfig | None = None,
) -> str | None:
    verified = outcome_fully_verified(ctx)
    # Terminal failure-mode signals must pre-empt tool-call hygiene nudges.
    terminal_signal = getattr(ctx, "latest_tool_blocker_signal", None) or getattr(ctx, "blocker_signal", None)
    if terminal_signal is not None:
        stash_turn_halt_from_blocker_signal(ctx, terminal_signal, source="enforcement_backstop")
    raise_if_turn_halt(ctx, verified=verified)
    _raise_if_unrecoverable_contract_stop(ctx)

    if verified_goal_satisfied_context(ctx):
        raise CopilotGoalSatisfied()
    if built_unverified_repair_inert_context(ctx) or built_complete_without_evaluated_outcome(ctx):
        raise CopilotBuiltUnverified()

    if _needs_repair_ceiling_halt(ctx):
        contract = getattr(ctx, "latest_diagnosis_repair_contract", None)
        state = _repair_loop_state(ctx)
        # Backstop: detection-time latch normally raises this above; catches a run-backed increment that bypassed it.
        signal = repair_ceiling_stop_signal(ctx, contract, config)
        stash_blocker_signal(ctx, signal)
        stash_repair_ceiling_turn_halt(
            ctx,
            signal,
            consecutive_identical_repair_count=(state.consecutive_identical_repair_count if state is not None else 0),
        )
        raise_if_turn_halt(ctx, verified=verified)

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
    non_retriable = _non_retriable_nav_error_nudge(ctx, config)
    if non_retriable is not None:
        nudge_msg, signature = non_retriable
        ctx.non_retriable_nav_error_last_emitted_signature = signature
        return nudge_msg

    if ctx.navigate_called and not ctx.observation_after_navigate and not ctx.navigate_enforcement_done:
        ctx.navigate_enforcement_done = True
        return _nudge(config, "post_navigate")

    if _needs_explore_without_workflow_nudge(ctx):
        ctx.explore_without_workflow_nudge_count += 1
        return _nudge(config, "post_explore_without_workflow")

    if (
        ctx.update_workflow_called
        and not ctx.test_after_update_done
        and getattr(ctx, "allow_untested_workflow_draft", False) is not True
    ):
        return _nudge(config, "post_update")

    if _post_run_observed_reply_can_finalize(ctx, result):
        return None

    _maybe_stash_terminal_challenge_halt(ctx)
    raise_if_turn_halt(ctx, verified=verified)

    # If the last run had confirmed challenge evidence, do not misdiagnose a
    # challenge-solving loop as a long-chain budgeting problem.
    if _needs_failed_test_nudge(ctx) and getattr(ctx, "last_test_anti_bot", None):
        ctx.failed_test_nudge_count += 1
        return _nudge(config, "post_anti_bot_failed_test")

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
            return _nudge(config, "post_per_tool_budget_stop")
        return _nudge(config, "post_per_tool_budget")

    repeated_frontier_nudge = _repeated_frontier_failure_nudge(ctx, config)
    if repeated_frontier_nudge is not None:
        # Latch the emitted level so each escalation fires at most once per streak.
        ctx.repeated_failure_nudge_emitted_at_streak = (
            REPEATED_FRONTIER_STREAK_STOP_AT
            if _is_stop_level_frontier_nudge(repeated_frontier_nudge, config)
            else REPEATED_FRONTIER_STREAK_ESCALATE_AT
        )
        return repeated_frontier_nudge

    # Do NOT clear last_test_suspicious_success here. tools._record_run_blocks_result
    # resets it on every new run; if the agent ignores the nudge and answers
    # without rerunning, we want _check_enforcement to re-emit the nudge.
    if _needs_suspicious_success_nudge(ctx):
        ctx.suspicious_success_nudge_count = getattr(ctx, "suspicious_success_nudge_count", 0) + 1
        return _nudge(config, "post_suspicious_success")

    # Checked before the generic failed-test nudge so a scrape-wall streak
    # emits the specific STOP text and does not also consume a
    # failed_test_nudge_count slot.
    if _needs_probable_site_block_stop_nudge(ctx):
        ctx.probable_site_block_stop_nudge_count = getattr(ctx, "probable_site_block_stop_nudge_count", 0) + 1
        signal = _probable_site_block_stop_signal(ctx, config)
        stash_blocker_signal(ctx, signal)
        stash_turn_halt_from_blocker_signal(ctx, signal, source="enforcement")
        raise_if_turn_halt(ctx, verified=verified)

    if _needs_failed_test_nudge(ctx):
        ctx.failed_test_nudge_count += 1
        if _needs_inspect_before_repair_nudge(ctx):
            return _nudge(config, "post_failed_test_inspect_first")
        return _nudge(config, "post_failed_test")

    # The convergence floor for code-authoring guardrail churn. It is the last
    # halt so every genuinely-terminal stop and recoverable nudge above gets its
    # turn first. The permanent non-retriable nav raise is the one stop ordering
    # cannot front-run (it lives after _check_enforcement returns None, at the
    # run_with_enforcement return sites), so the floor yields to it explicitly.
    if not getattr(ctx, "last_test_non_retriable_nav_error", None):
        churn_signal = _churn_signal_if_halting(ctx)
        if churn_signal is not None:
            emit_blocker_signal_payload(ctx, churn_signal)
            stash_turn_halt_from_blocker_signal(ctx, churn_signal, source="enforcement_backstop")
            raise_if_turn_halt(ctx)
        if _needs_no_progress_interaction_halt(ctx):
            no_progress_signal = no_forward_progress_interaction_stop_signal(ctx)
            emit_blocker_signal_payload(ctx, no_progress_signal)
            stash_turn_halt_from_blocker_signal(ctx, no_progress_signal, source="enforcement_backstop")
            raise_if_turn_halt(ctx)

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


def is_synthetic_user_message(item: Any) -> bool:
    """Return True if item is a screenshot or nudge (not a real user turn)."""
    return is_screenshot_message(item) or _is_nudge_message(item)


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
        return args_json[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_TRUNCATION_SUFFIX
    if not isinstance(parsed, dict):
        return args_json[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_TRUNCATION_SUFFIX
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
        return args_json[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_TRUNCATION_SUFFIX


def _prune_input_list(items: list[Any]) -> list[Any]:
    """Drop all but the most recent screenshot, compress older tool outputs,
    and summarize the arguments of older tool CALLS so bulky payloads (like
    the full workflow YAML) don't accumulate in replayed context.

    Screenshots collapse to a short text placeholder. function_call_output and
    function_call items keep the last KEEP_RECENT_TOOL_OUTPUTS at full size
    (head-truncated); older ones collapse to JSON synopses.
    """
    screenshot_indices = [i for i, item in enumerate(items) if is_screenshot_message(item)]
    drop_indices = set(screenshot_indices[:-1])

    fco_indices = [i for i, item in enumerate(items) if _item_field(item, "type") == "function_call_output"]
    recent_fco_set = set(fco_indices[-KEEP_RECENT_TOOL_OUTPUTS:])

    fc_indices = [i for i, item in enumerate(items) if _item_field(item, "type") == "function_call"]
    recent_fc_set = set(fc_indices[-KEEP_RECENT_TOOL_OUTPUTS:])

    result: list[Any] = []
    for i, item in enumerate(items):
        if i in drop_indices:
            result.append({"role": "user", "content": SCREENSHOT_PLACEHOLDER})
            continue

        item_type = _item_field(item, "type")
        if item_type == "function_call_output":
            output = _item_field(item, "output")
            if isinstance(output, str):
                if i in recent_fco_set:
                    new_output = (
                        output[:_RECENT_TOOL_OUTPUT_CHAR_CAP] + _TOOL_OUTPUT_HEAD_TRUNCATION_SUFFIX
                        if len(output) > _RECENT_TOOL_OUTPUT_CHAR_CAP
                        else output
                    )
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
    tool call/output pairs + latest nudge."""
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
    return [items[0]] + tail


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


_NUDGE_TYPE_BY_MESSAGE: dict[str, str] = {
    POST_UPDATE_NUDGE: "post_update",
    POST_NAVIGATE_NUDGE: "post_navigate",
    POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE: "explore_without_workflow",
    POST_SUSPICIOUS_SUCCESS_NUDGE: "suspicious_success",
    POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE: "repeated_frontier_failure_warn",
    POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE: "repeated_frontier_failure_stop",
    POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE: "non_retriable_nav_error_stop",
    POST_PARAMETER_BINDING_WARN_NUDGE: "parameter_binding_warn",
    POST_PARAMETER_BINDING_STOP_NUDGE: "parameter_binding_stop",
    POST_ANTI_BOT_FAILED_TEST_NUDGE: "anti_bot_block",
    POST_PROBABLE_SITE_BLOCK_STOP_NUDGE: "probable_site_block_stop",
    POST_PER_TOOL_BUDGET_NUDGE: "per_tool_budget_split",
    POST_PER_TOOL_BUDGET_STOP_NUDGE: "per_tool_budget_stop",
    POST_NO_WORKFLOW_DELIVERY_NUDGE: "no_workflow_delivery",
    POST_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGE: "discovery_entrypoint_url_question",
    PRE_DISCOVERY_URL_QUESTION_NUDGE: "pre_discovery_url_question",
    POST_FAILED_TEST_NUDGE: "post_failed_test",
    POST_FAILED_TEST_INSPECT_FIRST_NUDGE: "post_failed_test_inspect_first",
    SCREENSHOT_DROPPED_NUDGE: "screenshot_dropped_on_recovery",
}


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
    "post_probable_site_block_stop": "probable_site_block_stop",
    "post_probable_site_block_stop_prefix": "probable_site_block_stop",
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
}


def _nudge_type_for_log(nudge: str, config: CopilotConfig | None = None) -> str:
    nudge_by_key = config.enforcement_nudges if config is not None else DEFAULT_ENFORCEMENT_NUDGES
    if nudge.startswith(nudge_by_key["post_probable_site_block_stop_prefix"]):
        return "probable_site_block_stop"
    for key, value in nudge_by_key.items():
        if value == nudge:
            return _NUDGE_TYPE_BY_KEY.get(key, key)
    return _NUDGE_TYPE_BY_MESSAGE.get(nudge, "intermediate_success")


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
        _mark_copilot_total_timeout(ctx)
        LOG.warning(
            "Copilot total timeout exceeded mid-iteration",
            elapsed_seconds=round(time.monotonic() - start_time, 3),
            iteration=iteration,
        )
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


def synthesized_persistence_reopened(ctx: AgentContext) -> bool:
    if ctx.synthesized_block_reopened_for_output_coverage:
        return True
    if ctx.synthesized_block_reopened_for_credential_scout:
        return True
    if getattr(ctx, "synthesized_block_reopened_for_capture_obligation", False):
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


def _requested_output_labels_by_path(ctx: AgentContext) -> dict[str, tuple[str, ...]]:
    requested_paths = _requested_output_paths_for_ctx(ctx)
    labels_by_path: dict[str, tuple[str, ...]] = {}
    for criterion in _pre_run_gated_completion_criteria(ctx):
        outcome = criterion.outcome.strip()
        path = _effective_requested_output_path(criterion)
        if path in requested_paths and outcome:
            labels_by_path.setdefault(path, ())
            labels_by_path[path] += (outcome,)
    return labels_by_path


def requested_output_extraction_plan(ctx: AgentContext) -> RequestedOutputExtractionPlan | None:
    requested_paths = _requested_output_paths_for_ctx(ctx)
    if not requested_paths:
        return None
    labels_by_path = _requested_output_labels_by_path(ctx)
    if set(labels_by_path) != requested_paths:
        return None
    return derive_requested_output_extraction_plan(
        flow_evidence=ctx.flow_evidence,
        labels_by_path=labels_by_path,
    )


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
    # A path with no label is an underivable field; withholding the plan keeps a clarification legitimate.
    if set(labels_by_path) != requested_paths:
        return None
    return derive_requested_output_extraction_plan(
        flow_evidence=ctx.flow_evidence,
        labels_by_path=labels_by_path,
    )


def requested_output_extraction_plan_changed(ctx: AgentContext, current: RequestedOutputExtractionPlan | None) -> bool:
    if current is None or len(ctx.flow_evidence) < 2:
        return False
    previous = derive_requested_output_extraction_plan(
        flow_evidence=ctx.flow_evidence[:-1],
        labels_by_path=_requested_output_labels_by_path(ctx),
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


def _first_stable_login_submit_index(interactions: Sequence[Mapping[str, Any]], credential_index: int) -> int | None:
    for index, interaction in enumerate(interactions[credential_index + 1 :], start=credential_index + 1):
        tool_name = str(interaction.get("tool_name") or "").strip()
        if tool_name == "press_key" and str(interaction.get("key") or "").strip() == "Enter":
            return index
        if tool_name != "click":
            continue
        accessible_name = re.sub(r"[^a-z0-9]+", " ", str(interaction.get("accessible_name") or "").lower()).strip()
        selector = re.sub(r"[^a-z0-9]+", " ", str(interaction.get("selector") or "").lower()).strip()
        if _LOGIN_SUBMIT_NAME_PATTERN.fullmatch(accessible_name) or _LOGIN_SUBMIT_SELECTOR_PATTERN.fullmatch(selector):
            return index
    return None


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
    return _trajectory_reaches_generic_goal(ctx, trajectory, include_download=True)


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


def _last_scout_credential_fill_index(trajectory: list[Any]) -> int | None:
    # Boundary past the ENTIRE credential flow, including a runtime-only OTP/MFA fill. Keying only on
    # username/password let an MFA step (fill totp -> verify-click) form a durable entry->commit past
    # the boundary and falsely release the terminal-action gate on a login-only trajectory.
    last_index: int | None = None
    for index, item in enumerate(trajectory):
        if isinstance(item, dict) and str(item.get("tool_name") or "").strip() == CREDENTIAL_FILL_TOOL_NAME:
            last_index = index
    return last_index


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
    credential_submit_index = _first_stable_login_submit_index(interactions, credential_index)
    latest_fill_source_url = str(interactions[credential_index].get("source_url") or "").strip()
    if credential_submit_index is None and latest_fill_source_url:
        credential_submit_index = first_matched_post_fill_submit_index(
            interactions,
            credential_index,
            {latest_fill_source_url},
        )
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


def _should_force_synthesized_block_persistence(ctx: Any) -> bool:
    if getattr(ctx, "update_workflow_called", False) and not synthesized_persistence_reopened(ctx):
        return False
    if not _turn_intent_can_update_and_run_without_user_input(getattr(ctx, "turn_intent", None)):
        return False
    if normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)) != (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ):
        return False
    if not getattr(ctx, "synthesized_block_offered", False):
        return False
    trajectory = getattr(ctx, "scout_trajectory", None) or []
    if (getattr(ctx, "synthesized_block_offered_trajectory_len", 0) or 0) != len(trajectory):
        return False
    if not getattr(ctx, "synthesized_block_offered_goal_complete", False):
        return False
    return synthesized_trajectory_is_goal_complete(ctx)


def _should_block_mutating_tool_after_synthesized_offer(ctx: Any, tool_name: str) -> bool:
    if tool_name not in _SYNTHESIZED_BLOCK_PERSISTENCE_MUTATING_TOOLS:
        return False
    if _active_non_method_mandated_terminal_actions(ctx) or _active_floor_rekeyed_runtime_outputs(ctx):
        if not synthesized_trajectory_is_goal_complete(ctx):
            return False
    else:
        if uncovered_requested_output_paths(ctx):
            return False
        if _credential_flow_scout_gap_incomplete(ctx, getattr(ctx, "scout_trajectory", None) or []):
            return False
    if getattr(ctx, "update_workflow_called", False) and not synthesized_persistence_reopened(ctx):
        return False
    if not _turn_intent_can_update_and_run_without_user_input(getattr(ctx, "turn_intent", None)):
        return False
    if normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)) != (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ):
        return False
    if not getattr(ctx, "synthesized_block_offered", False):
        return False
    trajectory = getattr(ctx, "scout_trajectory", None) or []
    return (getattr(ctx, "synthesized_block_offered_trajectory_len", 0) or 0) == len(trajectory)


def _ambiguous_bare_selector_repair_context(ctx: Any) -> Any | None:
    repair_context = getattr(ctx, "last_code_authoring_repair_context", None)
    if getattr(repair_context, "reason_code", None) != "ambiguous_bare_selector":
        return None
    if getattr(repair_context, "workflow_run_id", None):
        return None
    if getattr(ctx, "last_run_blocks_workflow_run_id", None):
        return None
    return repair_context


def _ambiguous_bare_selector_rescout_key(ctx: Any) -> str | None:
    repair_context = _ambiguous_bare_selector_repair_context(ctx)
    if repair_context is None:
        return None
    if getattr(repair_context, "refiner_selector", None):
        return None
    selector_alternatives = getattr(repair_context, "selector_alternatives", None)
    if isinstance(selector_alternatives, list) and selector_alternatives:
        return None
    payload = {
        "block_label": str(getattr(repair_context, "block_label", "") or ""),
        "selector": str(getattr(repair_context, "selector", "") or ""),
        "source_url": str(getattr(repair_context, "source_url", "") or ""),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _ambiguous_bare_selector_rescout_signal_state(ctx: Any, tool_name: str) -> str | None:
    if tool_name != "evaluate":
        return None
    repair_context = _ambiguous_bare_selector_repair_context(ctx)
    if repair_context is None:
        return None
    if getattr(repair_context, "refiner_selector", None):
        return "block"
    selector_alternatives = getattr(repair_context, "selector_alternatives", None)
    if isinstance(selector_alternatives, list) and selector_alternatives:
        return "block"
    key = _ambiguous_bare_selector_rescout_key(ctx)
    if key is None:
        return None
    if getattr(ctx, "ambiguous_bare_selector_rescout_context_key", None) == key:
        return "block"
    # Track the one allowed same-page rescout for this author-time repair context.
    ctx.ambiguous_bare_selector_rescout_context_key = key
    return "allow"


def _uncovered_output_reject_rescout_key(canonical_paths: set[str], structural_failure_identity: str) -> str:
    return f"{structural_failure_identity}|{','.join(sorted(canonical_paths))}"


def _active_uncovered_output_reject_paths(ctx: AgentContext) -> set[str]:
    canonical = {
        _canonical_output_path(path)
        for path in author_time_reject_missing_output_paths(ctx.latest_recorded_build_test_outcome)
    }
    return canonical & uncovered_requested_output_paths(ctx) if canonical else set()


def _uncovered_output_reject_admits_evaluate(ctx: CopilotContext, tool_name: str) -> bool:
    return tool_name == "evaluate" and bool(_active_uncovered_output_reject_paths(ctx))


def _actuation_obligation_live_fill_delivery_required(ctx: CopilotContext) -> bool:
    turn_intent = getattr(ctx, "turn_intent", None)
    if (
        not isinstance(turn_intent, TurnIntent)
        or turn_intent.mode != TurnIntentMode.BUILD
        or RequiredContextKey.BROWSER_STATE not in turn_intent.required_context
    ):
        return False
    if normalize_block_authoring_policy(ctx.block_authoring_policy) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return False
    request_policy = getattr(ctx, "request_policy", None)
    criteria: list[CompletionCriterion] = []
    if isinstance(request_policy, RequestPolicy):
        criteria.extend(request_policy.completion_criteria)
    turn_state = getattr(ctx, "completion_criteria_turn_state", None)
    if turn_state is not None and turn_state.decision is not None:
        criteria.extend(turn_state.decision.criteria)
    if any(completion_criterion_requires_browser_fill_delivery(criterion) for criterion in criteria):
        return True
    trajectory = getattr(ctx, "scout_trajectory", None)
    return trajectory_has_browser_fill_interaction(trajectory) if isinstance(trajectory, list) else False


def _actuation_obligation_required_fill_tool(ctx: CopilotContext) -> str | None:
    if _actuation_obligation_live_fill_delivery_required(ctx):
        return _ACTUATION_OBLIGATION_REQUIRED_FILL_TOOL
    return None


def _actuation_obligation_admits_required_fill_tool(ctx: CopilotContext, tool_name: str) -> bool:
    return tool_name == _actuation_obligation_required_fill_tool(ctx)


def _actuation_obligation_admits_login_completion_tool(
    ctx: CopilotContext, tool_name: str, arguments: Mapping[str, Any] | None
) -> bool:
    if tool_name not in _SYNTHESIZED_BLOCK_COMMIT_TOOLS:
        return False
    # Only Enter commits a login form, matching what _first_stable_login_submit_index credits as a
    # submit; every other keystroke stays gated. Absent arguments fail closed — the signature
    # defaults them to None, so a caller that omits them must not admit arbitrary keystrokes.
    if tool_name == "press_key" and (
        not isinstance(arguments, Mapping) or str(arguments.get("key") or "").strip() != "Enter"
    ):
        return False
    if not _actuation_obligation_live_fill_delivery_required(ctx):
        return False
    if _last_scout_credential_fill_index(ctx.scout_trajectory) is None:
        return False
    return not _trajectory_reaches_post_credential_commit(ctx)


def arm_credential_scout_reopen(ctx: AgentContext, identity_digest: str) -> bool:
    """Arm a one-shot scout-window reopen for the first author-time credential-scout reject per
    (structural identity + credential binding) digest. A repeat identical reject returns False and
    falls through so it counts normally toward the repair ceiling."""
    if ctx.credential_scout_rescout_context_key == identity_digest:
        return False
    ctx.credential_scout_rescout_context_key = identity_digest
    ctx.synthesized_block_reopened_for_credential_scout = True
    return True


def _credential_scout_reopen_admits_evaluate(ctx: CopilotContext, tool_name: str) -> bool:
    return tool_name == "evaluate" and bool(ctx.synthesized_block_reopened_for_credential_scout)


def _never_captured_obligation_admits_expected_tool(
    ctx: CopilotContext, tool_name: str, arguments: Mapping[str, Any] | None
) -> bool:
    obligation = getattr(ctx, "never_captured_obligation", None)
    if (
        obligation is None
        or obligation.state != "armed"
        or obligation.turn_id != ctx.turn_id
        or tool_name != obligation.expected_tool_name
        or not isinstance(arguments, Mapping)
    ):
        return False
    selector = arguments.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        return False
    expected_selectors = {
        normalized_scout_selector(candidate) for candidate in locator_selector_literals(obligation.normalized_receiver)
    }
    if normalized_scout_selector(selector.strip()) not in expected_selectors:
        return False
    expected_argument = obligation.expected_argument_literal
    if expected_argument is None:
        return True
    argument_key = {"press_key": "key", "select_option": "value", "type_text": "text"}.get(tool_name)
    return argument_key is not None and str(arguments.get(argument_key) or "") == expected_argument


def consume_uncovered_output_reopen_event(ctx: CopilotContext) -> bool:
    """Arm a one-shot scout-window reopen for the first author-time reject citing an uncovered
    requested-output path. Returns True only on that first reject per structural identity; a
    repeat identical reject falls through so it counts normally toward the repair ceiling."""
    active = _active_uncovered_output_reject_paths(ctx)
    if not active:
        return False
    latest = ctx.latest_recorded_build_test_outcome
    if latest is None:
        return False
    key = _uncovered_output_reject_rescout_key(active, latest.structural_failure_identity)
    if ctx.uncovered_output_rescout_context_key == key:
        return False
    ctx.uncovered_output_rescout_context_key = key
    ctx.synthesized_block_reopened_for_output_coverage = True
    return True


def uncovered_output_reject_scout_steer_signal(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    if tool_name not in _SYNTHESIZED_BLOCK_REAUTHORING_TOOLS:
        return None
    if not ctx.synthesized_block_reopened_for_output_coverage:
        return None
    active = _active_uncovered_output_reject_paths(ctx)
    if not active:
        return None
    latest = ctx.latest_recorded_build_test_outcome
    if latest is None:
        return None
    key = _uncovered_output_reject_rescout_key(active, latest.structural_failure_identity)
    if ctx.uncovered_output_rescout_steer_key == key:
        return None
    payload: AuthorTimeGateAblationPayload = {
        "uncovered_output_paths": sorted(active),
        "structural_failure_identity": latest.structural_failure_identity,
    }
    if record_author_time_gate_ablation_event(
        ctx,
        gate_id="uncovered_output_rescout_steer",
        reason_code=UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE,
        fingerprint=key,
        blocked_tool=tool_name,
        payload=payload,
    ):
        return None
    # Commit-after-claim: a yielded steer must not burn the one-shot rescout key.
    if claim_turn(ctx, TurnClaimant.UNCOVERED_OUTPUT_RESCOUT_STEER) is ClaimOutcome.YIELDED:
        return None
    ctx.uncovered_output_rescout_steer_key = key
    named_paths = ", ".join(sorted(active))
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "The authored workflow still leaves these requested output paths unobserved: "
            f"{named_paths}. Do NOT re-author yet. Call evaluate to scout the page where those values "
            "appear until they are observed, then author a block that returns them."
        ),
        user_facing_reason="I need to view the page with the requested details before saving the workflow.",
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=frozenset({"evaluate"}),
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code=UNCOVERED_OUTPUT_RESCOUT_STEER_REASON_CODE,
        blocked_tool=tool_name,
        extra={"uncovered_output_paths": sorted(active)},
    )


def _should_block_tool_after_unresolved_recorded_outcome(ctx: Any, tool_name: str) -> bool:
    if tool_name not in _SYNTHESIZED_BLOCK_PERSISTENCE_MUTATING_TOOLS and tool_name != "update_workflow":
        return False
    if not _turn_intent_can_update_and_run_without_user_input(getattr(ctx, "turn_intent", None)):
        return False
    if normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)) != (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ):
        return False
    latest = getattr(ctx, "latest_recorded_build_test_outcome", None)
    if not isinstance(latest, RecordedBuildTestOutcome) or not latest.is_authoritative:
        return False
    if latest.phase != "persisted_block_run" or latest.reason_code != "outcome_not_demonstrated":
        return False
    return _completion_verification_unsatisfied(ctx)


def _post_run_page_path_tool_allowed(
    condition: PostRunPagePathFailure,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> bool:
    if not condition.is_page_path or not isinstance(arguments, Mapping):
        return False
    selectors = {target.selector for target in condition.continuation_targets}
    if tool_name == "click":
        selector = arguments.get("selector")
        return isinstance(selector, str) and selector in selectors
    if tool_name != "press_key" or arguments.get("key") != "Enter" or not condition.enter_allowed:
        return False
    selector = arguments.get("selector")
    enter_selectors = {
        target.selector for target in condition.continuation_targets if target.kind in {"form_submit", "challenge"}
    }
    return isinstance(selector, str) and selector in enter_selectors


def _latest_scout_trajectory_index(ctx: CopilotContext) -> int:
    return max(
        (
            trajectory_index
            for interaction in ctx.scout_trajectory
            if isinstance((trajectory_index := interaction.get("trajectory_index")), int)
        ),
        default=-1,
    )


def _post_run_page_path_successful_interactions(
    ctx: CopilotContext,
    window: PostRunPagePathInteractionWindow,
) -> int:
    return sum(
        1
        for interaction in ctx.scout_trajectory
        if isinstance((trajectory_index := interaction.get("trajectory_index")), int)
        and trajectory_index > window.trajectory_anchor
        and str(interaction.get("tool_name") or "") in _SYNTHESIZED_BLOCK_COMMIT_TOOLS
    )


def _post_run_page_path_admission_state(
    ctx: CopilotContext,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> tuple[PostRunPagePathInteractionWindow, str, str, int, int] | None:
    if not _should_block_tool_after_unresolved_recorded_outcome(ctx, tool_name):
        return None
    latest = ctx.latest_recorded_build_test_outcome
    if latest is None:
        return None
    condition = latest.page_path_failure
    if condition is None or not _post_run_page_path_tool_allowed(condition, tool_name, arguments):
        return None
    structural_key = latest.structural_key
    workflow_run_id = latest.workflow_run_id
    if not structural_key or not workflow_run_id:
        return None
    if (
        ctx.post_run_page_observation_after_failed_test is not True
        or ctx.post_run_page_observation_tool not in _POST_RUN_PAGE_OBSERVATION_TOOLS
        or ctx.post_run_page_observation_workflow_run_id != workflow_run_id
        or ctx.post_run_page_observation_url != condition.current_url
        or condition.workflow_run_id != workflow_run_id
        or ctx.last_run_blocks_workflow_run_id != workflow_run_id
    ):
        return None
    window = ctx.post_run_page_path_interaction_window
    if window is None or (window.structural_key, window.workflow_run_id) != (structural_key, workflow_run_id):
        window = PostRunPagePathInteractionWindow(
            structural_key=structural_key,
            workflow_run_id=workflow_run_id,
            trajectory_anchor=_latest_scout_trajectory_index(ctx),
        )
    successful_interactions = _post_run_page_path_successful_interactions(ctx, window)
    observation_generation = ctx.post_run_page_observation_generation
    if window.admitted_attempts >= _POST_RUN_PAGE_PATH_INTERACTION_BUDGET or (
        successful_interactions > window.observed_successful_interactions
        and observation_generation <= window.observation_generation
    ):
        return None
    owner = current_turn_owner(ctx)
    if (
        owner is not None
        and owner.claimant is not TurnClaimant.POST_RUN_PAGE_PATH_INTERACTION
        and not claimant_outranks(TurnClaimant.POST_RUN_PAGE_PATH_INTERACTION, owner.claimant)
    ):
        return None
    return window, structural_key, workflow_run_id, successful_interactions, observation_generation


def post_run_page_path_interaction_allowed(
    ctx: CopilotContext,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> bool:
    return _post_run_page_path_admission_state(ctx, tool_name, arguments) is not None


def try_admit_post_run_page_path_interaction(
    ctx: CopilotContext,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> bool:
    state = _post_run_page_path_admission_state(ctx, tool_name, arguments)
    if state is None:
        return False
    window, structural_key, workflow_run_id, successful_interactions, observation_generation = state
    if claim_turn(ctx, TurnClaimant.POST_RUN_PAGE_PATH_INTERACTION) is ClaimOutcome.YIELDED:
        return False
    ctx.post_run_page_path_interaction_window = replace(
        window,
        admitted_attempts=window.admitted_attempts + 1,
        observation_generation=observation_generation,
        observed_successful_interactions=successful_interactions,
    )
    LOG.info(
        "copilot post-run page-path interaction admitted",
        tool_name=tool_name,
        selector=arguments.get("selector") if isinstance(arguments, Mapping) else None,
        structural_key=structural_key,
        workflow_run_id=workflow_run_id,
        admitted_attempts=window.admitted_attempts + 1,
        observation_generation=observation_generation,
    )
    return True


def synthesized_block_persistence_signal(
    ctx: Any, tool_name: str, arguments: Mapping[str, Any] | None = None
) -> CopilotToolBlockerSignal | None:
    if _should_block_tool_after_unresolved_recorded_outcome(ctx, tool_name):
        signal = CopilotToolBlockerSignal(
            blocker_kind="tool_error",
            agent_steering_text=(
                "The last recorded test outcome is authoritative and still has unsatisfied completion criteria. "
                f"Call {SYNTHESIZED_BLOCK_PERSISTENCE_TOOL} with a materially changed authored workflow now; "
                "do not spend the repair attempt on another standalone tool call."
            ),
            user_facing_reason="I need to revise and test the workflow code instead of interacting with the page.",
            recovery_hint="retry_with_different_tool",
            cleared_by_tools=frozenset({SYNTHESIZED_BLOCK_PERSISTENCE_TOOL}),
            preserves_workflow_draft=True,
            renders_final_reply=False,
            internal_reason_code=SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
            blocked_tool=tool_name,
            extra={"recorded_outcome_reason_code": "outcome_not_demonstrated"},
        )
        if try_admit_post_run_page_path_interaction(ctx, tool_name, arguments):
            return None
        return signal
    if tool_name in _SYNTHESIZED_BLOCK_PERSISTENCE_ALLOWED_TOOLS:
        return None
    ambiguous_selector_rescout_state = _ambiguous_bare_selector_rescout_signal_state(ctx, tool_name)
    if ambiguous_selector_rescout_state == "allow":
        return None
    if _uncovered_output_reject_admits_evaluate(ctx, tool_name):
        return None
    if _credential_scout_reopen_admits_evaluate(ctx, tool_name):
        claim_turn(ctx, TurnClaimant.CREDENTIAL_SCOUT_REOPEN)
        return None
    if _never_captured_obligation_admits_expected_tool(ctx, tool_name, arguments):
        claim_turn(ctx, TurnClaimant.CAPTURE_OBLIGATION_REOPEN)
        return None
    if _actuation_obligation_admits_required_fill_tool(ctx, tool_name):
        claim_turn(ctx, TurnClaimant.ACTUATION_OBLIGATION_FILL)
        return None
    if _actuation_obligation_admits_login_completion_tool(ctx, tool_name, arguments):
        claim_turn(ctx, TurnClaimant.ACTUATION_OBLIGATION_LOGIN_COMPLETION)
        return None
    if (
        ambiguous_selector_rescout_state != "block"
        and not _should_force_synthesized_block_persistence(ctx)
        and not _should_block_mutating_tool_after_synthesized_offer(ctx, tool_name)
    ):
        return None
    return CopilotToolBlockerSignal(
        blocker_kind="tool_error",
        agent_steering_text=(
            "A synthesized code block offer is already available for this authoring turn. "
            f"Call {SYNTHESIZED_BLOCK_PERSISTENCE_TOOL} with that block now before any more scouting, "
            "reading, page evaluation, or browser interaction. This blocker clears only after "
            f"{SYNTHESIZED_BLOCK_PERSISTENCE_TOOL} succeeds."
        ),
        user_facing_reason="I need to save and test the drafted workflow before scouting more.",
        recovery_hint="retry_with_different_tool",
        cleared_by_tools=frozenset({SYNTHESIZED_BLOCK_PERSISTENCE_TOOL}),
        preserves_workflow_draft=True,
        renders_final_reply=False,
        internal_reason_code=SYNTHESIZED_BLOCK_PERSISTENCE_REASON_CODE,
        blocked_tool=tool_name,
        extra={
            "synthesized_block_offered_trajectory_len": (
                getattr(ctx, "synthesized_block_offered_trajectory_len", 0) or 0
            ),
        },
    )


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
            _mark_copilot_total_timeout(ctx)
            raise CopilotTotalTimeoutError()

        if iteration >= MAX_ITERATIONS:
            LOG.error("Enforcement iteration cap reached", max_iterations=MAX_ITERATIONS)
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
            force_synthesized_block_persistence = _should_force_synthesized_block_persistence(ctx)
            force_advisory_run_dispatch = _should_force_advisory_run_dispatch(ctx)
            # The advisory-dispatch force claims the actuation ladder itself (same-claimant), so the
            # grant-consumption path can never self-deadlock.
            if force_advisory_run_dispatch:
                claim_turn(ctx, TurnClaimant.OUTPUT_CONTRACT_ACTUATION)
            force_run_dispatch = force_synthesized_block_persistence or force_advisory_run_dispatch
            current_runner_kwargs = (
                _runner_kwargs_with_forced_tool_choice(runner_kwargs, SYNTHESIZED_BLOCK_PERSISTENCE_TOOL)
                if force_run_dispatch
                else runner_kwargs
            )
            effective_run_config = current_runner_kwargs.get("run_config")
            effective_model_settings = (
                effective_run_config.model_settings if isinstance(effective_run_config, RunConfig) else None
            )
            turn_intent = getattr(ctx, "turn_intent", None)
            turn_intent_authority = getattr(turn_intent, "authority", None)
            LOG.info(
                "copilot synthesized persistence force decision",
                force_synthesized_block_persistence=force_synthesized_block_persistence,
                force_advisory_run_dispatch=force_advisory_run_dispatch,
                forced_tool_name=(SYNTHESIZED_BLOCK_PERSISTENCE_TOOL if force_run_dispatch else None),
                chosen_tool_name=(SYNTHESIZED_BLOCK_PERSISTENCE_TOOL if force_run_dispatch else None),
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
                _mark_copilot_total_timeout_if_elapsed(ctx, start_time)
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
                    _mark_copilot_total_timeout_if_elapsed(ctx, start_time)
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
                    _mark_copilot_total_timeout_if_elapsed(ctx, start_time)
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
            nudge: str | None = pending_recovery_nudge
            pending_recovery_nudge = None
        else:
            nudge = _check_enforcement(ctx, result, copilot_config)

        # The offer is independent of the nudge: a clean scout-then-author turn
        # finalizes with nudge=None, so injecting it only inside the nudge branch
        # would never reach the model. Compute it once here so it rides both the
        # nudge path and the finalize path.
        synthesized_msg = _maybe_synthesized_block_offer_msg(ctx)

        spine_checkpoint_nudge = False
        if nudge is None:
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
                nudge = _check_enforcement(ctx, result, copilot_config)
            if nudge is None and synthesized_msg is None:
                spine_nudge = _scouted_spine_turn_end_nudge(ctx)
                if spine_nudge is None:
                    _consume_pending_screenshots(ctx)
                    _maybe_raise_non_retriable_nav(ctx)
                    return result
                nudge = spine_nudge
                spine_checkpoint_nudge = True

        if nudge is not None and not spine_checkpoint_nudge and nudge == _nudge(copilot_config, "post_update"):
            if ctx.post_update_nudge_count >= MAX_POST_UPDATE_NUDGES:
                LOG.warning(
                    "Enforcement exhausted post-update nudges, allowing response",
                    nudge_count=ctx.post_update_nudge_count,
                )
                spine_nudge = _scouted_spine_turn_end_nudge(ctx)
                if spine_nudge is None:
                    _consume_pending_screenshots(ctx)
                    _maybe_raise_non_retriable_nav(ctx)
                    return result
                nudge = spine_nudge
                spine_checkpoint_nudge = True
            else:
                ctx.post_update_nudge_count += 1

        if spine_checkpoint_nudge:
            nudge_type = "scouted_spine_under_build_checkpoint"
        elif nudge is not None:
            nudge_type = _nudge_type_for_log(nudge, copilot_config)
        else:
            nudge_type = "synthesized_block_offer"
        LOG.info("Enforcement nudge", nudge_type=nudge_type, iteration=iteration)

        # OpenAI rejects images in tool messages, so a queued post-run
        # screenshot rides as its own user message just before the nudge.
        screenshot_msg = _consume_pending_screenshots(ctx)
        if screenshot_msg is not None:
            LOG.info("Injecting screenshot user message", count=len(screenshot_msg["content"]) - 1)

        with copilot_span("enforcement_nudge", data={"nudge_type": nudge_type, "iteration": iteration}):
            extra_msgs = _assemble_enforcement_messages(screenshot_msg, nudge, synthesized_msg)
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
