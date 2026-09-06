"""Enforcement wrapper — nudge agent when it skips required steps."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog
from agents.exceptions import MaxTurnsExceeded
from agents.memory.session import Session
from agents.run import Runner

from skyvern.config import settings
from skyvern.forge.sdk.copilot import streaming_adapter
from skyvern.forge.sdk.copilot.budget_expiry import (
    BudgetExpirySource,
    serialize_budget_expiry_observation,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    CREDENTIAL_FILL_TOOL_NAME,
    credential_scout_gap,
    credential_submit_boundary_index,
    first_matched_post_fill_submit_index,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    first_stable_login_submit_index as _first_stable_login_submit_index,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    is_durable_fallback_entry_target,
    is_generic_entry_opener_click,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    last_scout_credential_fill_index as _last_scout_credential_fill_index,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import (
    trajectory_has_browser_fill_interaction,
)
from skyvern.forge.sdk.copilot.completion_criteria_store import requested_output_paths
from skyvern.forge.sdk.copilot.completion_verification import only_structural_requested_output_abstentions
from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema
from skyvern.forge.sdk.copilot.config import (
    DEFAULT_ENFORCEMENT_NUDGES,
    DEFAULT_TOKEN_BUDGET,
    CopilotConfig,
)
from skyvern.forge.sdk.copilot.credential_fill_fields import LIVE_SCOUT_CREDENTIAL_FIELDS
from skyvern.forge.sdk.copilot.credential_pause import maybe_credential_pause, release_credential_pause_gate
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction
from skyvern.forge.sdk.copilot.human_input_wait import HumanInputWait
from skyvern.forge.sdk.copilot.narration import TransitionKind
from skyvern.forge.sdk.copilot.output_extraction_plan import (
    resolve_shape_expectations_by_path,
)
from skyvern.forge.sdk.copilot.output_policy import (
    normalize_response_scaffolding,
)
from skyvern.forge.sdk.copilot.output_utils import (
    BUILD_TEST_PACKET_KEY,
    MCP_RESULT_PROVENANCE_KEY,
    MCP_RESULT_PROVENANCE_VALUE,
    extract_final_text,
    parse_final_response,
)
from skyvern.forge.sdk.copilot.pending_operation import (
    install_pending_operation_slot,
    pending_operation,
    pending_operation_fields,
)
from skyvern.forge.sdk.copilot.request_policy import (
    CompletionCriterion,
    RequestPolicy,
    floor_rekeyed_requested_output_paths,
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
from skyvern.forge.sdk.copilot.runtime import (
    AgentContext,
)
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotActionRelation, ScreenshotEntry
from skyvern.forge.sdk.copilot.terminal_predicates import (
    artifact_health_blocked,
    outcome_criteria_evaluated,
    outcome_fully_verified,
)
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import (
    raise_if_turn_halt,
    stash_turn_halt_from_blocker_signal,
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

TOTAL_TIMEOUT_SECONDS = settings.WORKFLOW_COPILOT_TOTAL_TIMEOUT_SECONDS or 900
BUDGET_DRAIN_HEADROOM = 4
HARD_BACKSTOP_ALLOWANCE_SECONDS = 90
# Floor for the per-iteration ``wait_for`` deadline so an already-spent budget
# never yields ``wait_for(timeout=0)`` (which raises immediately). Kept as a
# constant so tests can shrink it instead of paying a full second per deadline.
MIN_DEADLINE_REMAINING_SECONDS = 1.0
SCREENSHOT_SENTINEL = "[copilot:screenshot] "
PAIRED_OBSERVATION_MARKER = "[copilot:paired-observation] "
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
    return True


def built_unverified_repair_inert_context(ctx: CopilotContext) -> bool:
    return (
        ctx.last_test_ok is True
        and ctx.last_full_workflow_test_ok is True
        and _outcome_criteria_evaluated(ctx)
        and _latest_diagnosis_contract_selects_no_repair(ctx)
        and _completion_verification_only_structural_abstentions(ctx)
    )


def gate_decision_trace_fields(ctx: CopilotContext) -> dict[str, bool]:
    """The terminal-gate decision plus the conjuncts that explain it.

    Captured wherever the gate is evaluated (including when it returns False, the
    signal that explains why the turn continued) so a single trace shows whether
    the gate failed on the test, the full-workflow run, the diagnosis contract,
    or the absence of outcome verification.
    """
    return {
        "gate_satisfied": verified_goal_satisfied_context(ctx),
        "gate_built_unverified_repair_inert": built_unverified_repair_inert_context(ctx),
        "gate_built_complete_without_evaluated_outcome": built_complete_without_evaluated_outcome(ctx),
        "gate_last_test_ok": ctx.last_test_ok is True,
        "gate_last_full_workflow_test_ok": ctx.last_full_workflow_test_ok is True,
        "gate_diagnosis_contract_satisfies_goal": latest_diagnosis_contract_satisfies_goal(ctx),
        "gate_outcome_criteria_evaluated": _outcome_criteria_evaluated(ctx),
        "gate_artifact_health_blocked": artifact_health_blocked(ctx),
        "gate_evaluated_this_turn": True,
    }


def _mark_copilot_total_timeout(ctx: Any, *, elapsed_seconds: float, iteration: int) -> None:
    already_marked = ctx.copilot_total_timeout_exceeded is True
    ctx.copilot_total_timeout_exceeded = True
    if ctx.budget_expiry_state.source is None:
        ctx.budget_expiry_state.source = "deadline"
    if already_marked:
        return
    LOG.warning(
        "copilot_turn_deadline_expired",
        elapsed_seconds=round(elapsed_seconds, 3),
        iteration=iteration,
        **pending_operation_fields(),
    )


def _elapsed_run_seconds(ctx: Any, start_time: float) -> float:
    """Wall-clock elapsed since ``start_time``, minus completed and active human-input waits.

    Keeps TOTAL_TIMEOUT_SECONDS a budget over actual agent work, not real
    time, so a paused-and-resumed turn isn't penalized for pause time.

    ``pause_seconds`` is coerced defensively: tests commonly pass a bare
    ``MagicMock()`` as ``ctx``, whose ``getattr(..., default)`` returns a
    fresh Mock instead of the default (Mock never raises AttributeError).
    """
    pause_seconds = getattr(ctx, "copilot_credential_pause_seconds", 0.0)
    if not isinstance(pause_seconds, (int, float)):
        pause_seconds = 0.0
    question_seconds = getattr(ctx, "copilot_question_pause_seconds", 0.0)
    if not isinstance(question_seconds, (int, float)):
        question_seconds = 0.0
    now = time.monotonic()
    wait = getattr(ctx, "human_input_wait", None)
    # Overlapping waits credit their union only when the last waiter exits.
    # Until then, exclude the active union at model/tool completion boundaries.
    active_wait_seconds = now - wait.started_at if isinstance(wait, HumanInputWait) and wait.count > 0 else 0.0
    return now - start_time - pause_seconds - question_seconds - active_wait_seconds


def _mark_copilot_total_timeout_if_elapsed(ctx: Any, start_time: float, iteration: int) -> None:
    elapsed = _elapsed_run_seconds(ctx, start_time)
    if elapsed >= TOTAL_TIMEOUT_SECONDS:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=elapsed, iteration=iteration)


def _record_copilot_cancellation(ctx: Any, start_time: float, iteration: int) -> None:
    """Record a cancellation raised at a model-call boundary, whatever the elapsed budget.

    Synchronous and never raising, so the caller's ``raise`` re-raises the original
    cancellation neither masked nor delayed.
    """
    try:
        elapsed = _elapsed_run_seconds(ctx, start_time)
        _mark_copilot_total_timeout_if_elapsed(ctx, start_time, iteration)
        ctx.copilot_turn_cancelled_iteration = iteration
        LOG.warning(
            "copilot_turn_cancelled",
            elapsed_seconds=round(elapsed, 3),
            iteration=iteration,
            deadline_exceeded=ctx.copilot_total_timeout_exceeded is True,
            **pending_operation_fields(),
        )
    except Exception:
        LOG.exception("Failed to record a copilot turn cancellation", iteration=iteration)


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


def pending_screenshot_message(ctx: Any) -> dict[str, Any] | None:
    """Build the synthetic user message for the staged frame without draining it.

    Tool results stay text-only because OpenAI rejects images in tool
    messages, so screenshots are delivered as a follow-up user message.
    """
    # Re-checked here rather than only at enqueue: a retriable failure can swap in a
    # non-vision fallback model after the frame was staged, so every delivery path needs it.
    if not getattr(ctx, "supports_vision", False):
        return None
    pending = getattr(ctx, "pending_screenshots", None)
    if not isinstance(pending, list) or not pending:
        return None
    screenshots: list[ScreenshotEntry] = list(pending)
    provenance_lines: list[str] = []
    for entry in screenshots:
        provenance = entry.provenance
        fields = {
            "capture_id": entry.capture_id,
            "source_tool": provenance.source_tool,
            "captured_url": provenance.captured_url or "unavailable",
            "dispatch_url": provenance.dispatch_url or "unavailable",
            "observation_step": provenance.observation_step
            if provenance.observation_step is not None
            else "unavailable",
            "browser_session_id": provenance.browser_session_id or "unavailable",
            "dispatch_browser_session_id": provenance.dispatch_browser_session_id or "unavailable",
            "producer_browser_session_id": provenance.producer_browser_session_id or "unavailable",
            "session_binding": provenance.session_binding.value,
            "workflow_run_id": provenance.workflow_run_id or "unavailable",
            "action_relation": provenance.action_relation.value,
        }
        rendered = "; ".join(f"{key}={value}" for key, value in fields.items())
        relation = (
            "This frame was captured during the named page observation."
            if provenance.action_relation is ScreenshotActionRelation.SAME_PAGE_OBSERVATION
            else "This frame records the named source at its stated action relation."
        )
        provenance_lines.append(
            f"Frame provenance: {rendered}. {relation} Its provenance does not claim freshness after later actions."
        )
    paired_marker = (
        PAIRED_OBSERVATION_MARKER
        if screenshots[0].provenance.action_relation is ScreenshotActionRelation.SAME_PAGE_OBSERVATION
        else ""
    )
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": SCREENSHOT_SENTINEL + paired_marker + "\n".join(provenance_lines),
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


def _consume_pending_screenshots(ctx: Any) -> dict[str, Any] | None:
    """Build the screenshot message and clear the queue — the end-of-turn drain."""
    message = pending_screenshot_message(ctx)
    pending = getattr(ctx, "pending_screenshots", None)
    if isinstance(pending, list):
        pending.clear()
    if hasattr(ctx, "pending_frame_lease"):
        ctx.pending_frame_lease = None
    return message


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


def enforcement_decision(
    ctx: Any,
    result: RunResultStreaming | None = None,
    config: CopilotConfig | None = None,
) -> EnforcementDecision | None:
    """Propagate terminal runtime evidence without prescribing another authoring action."""
    del result, config
    verified = outcome_fully_verified(ctx)
    terminal_signal = getattr(ctx, "latest_tool_blocker_signal", None) or getattr(ctx, "blocker_signal", None)
    if terminal_signal is not None:
        stash_turn_halt_from_blocker_signal(ctx, terminal_signal, source="enforcement_backstop")
    raise_if_turn_halt(ctx, verified=verified)
    _raise_if_unrecoverable_contract_stop(ctx)

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


def is_paired_observation_message(item: Any) -> bool:
    """Return True only for explicitly marked observation-bound frames."""
    if _item_field(item, "role") != "user":
        return False
    prefix = SCREENSHOT_SENTINEL + PAIRED_OBSERVATION_MARKER
    content = _item_field(item, "content")
    if isinstance(content, str):
        return content.startswith(prefix)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(_item_field(block, "text"), str) and _item_field(block, "text").startswith(prefix)
        for block in content
    )


def _is_nudge_message(item: Any) -> bool:
    """Return True if the item is a synthetic enforcement nudge."""
    if _item_field(item, "role") != "user":
        return False
    content = _item_field(item, "content")
    return isinstance(content, str) and content.startswith(NUDGE_SENTINEL)


def is_synthetic_user_message(item: Any) -> bool:
    """Return True if item is a screenshot or enforcement nudge, not a real user turn."""
    return is_screenshot_message(item) or _is_nudge_message(item)


_PACKET_FAILURE_SCALARS = ("block_label", "block_status", "reason", "failing_line")
_PACKET_LIST_CAP = 6
_PACKET_REASON_CAP = 200


def _bounded_error_codes(codes: Any) -> list[str]:
    return [str(c)[:64] for c in codes if isinstance(c, str)][:_PACKET_LIST_CAP]


def _retained_run_packet(packet: Any) -> dict[str, Any] | None:
    """The packet's identity and failure scalars, bounded. Lists are tails, so they yield first."""
    if not isinstance(packet, dict):
        return None
    kept: dict[str, Any] = {}
    version = packet.get("contract_version")
    if isinstance(version, str) and version:
        kept["contract_version"] = version
    run = packet.get("run")
    if isinstance(run, dict):
        run_id = run.get("workflow_run_id")
        status = run.get("status")
        bounded_run = {k: v for k, v in (("workflow_run_id", run_id), ("status", status)) if v}
        if bounded_run:
            kept["run"] = bounded_run
    failure = packet.get("failure")
    if isinstance(failure, dict):
        bounded: dict[str, Any] = {}
        for field in _PACKET_FAILURE_SCALARS:
            value = failure.get(field)
            if value is None or value == "":
                continue
            bounded[field] = value if isinstance(value, (bool, int, float)) else str(value)[:_PACKET_REASON_CAP]
        codes = failure.get("error_codes")
        if isinstance(codes, list) and codes:
            bounded["error_codes"] = _bounded_error_codes(codes)
        if bounded:
            kept["failure"] = bounded
    return kept or None


def _truncated_output_fallback(output: str) -> str:
    return output[:_TOOL_OUTPUT_SUMMARIZE_THRESHOLD] + _TOOL_OUTPUT_TRUNCATION_SUFFIX


# Compacting page evidence to {"ok":true} tells the model it has evidence while leaving it none, so
# raw excerpts and derived relations go first and the bounded facts describing what is on the page,
# and which browser saw it, stay. An allowlist rather than a drop-list: an evidence field nobody has
# classified yet is likelier to be raw page text than a fact worth keeping.
_PAGE_EVIDENCE_IDENTITY = (
    "source_tool",
    "current_url",
    "inspected_url",
    "page_title",
    "observation_step",
    "source_browser_session_id",
    "workflow_run_id",
    "observed_after_workflow_run",
)
_PAGE_EVIDENCE_FACT_LISTS = (
    "forms",
    "navigation_targets",
    "result_containers",
    "clickable_controls",
    "challenge_controls",
    "modal_overlays",
    "page_obstructions",
)
_PAGE_EVIDENCE_MAX_ENTRIES = 12
_PAGE_EVIDENCE_ENTRY_CHARS = 400
# Per-list bounds alone would let a page with many forms and links produce a compacted output as
# large as an uncompacted one, defeating the pass whose job is keeping the turn inside the window.
_PAGE_EVIDENCE_SUMMARY_CHARS = 4000


def _is_page_evidence(data: dict[str, Any]) -> bool:
    if data.get("source_tool") == "inspect_page_for_composition":
        return True
    return any(key in data for key in _PAGE_EVIDENCE_FACT_LISTS) and "inspected_url" in data


def _compact_length(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def _bounded_evidence_entries(value: list[Any], budget: int) -> list[Any]:
    entries: list[Any] = []
    for entry in value[:_PAGE_EVIDENCE_MAX_ENTRIES]:
        blob = json.dumps(entry, separators=(",", ":"))
        bounded = entry if len(blob) <= _PAGE_EVIDENCE_ENTRY_CHARS else blob[:_PAGE_EVIDENCE_ENTRY_CHARS]
        budget -= _compact_length(bounded)
        if budget < 0:
            break
        entries.append(bounded)
    return entries


def _summarize_page_evidence(parsed: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key in _PAGE_EVIDENCE_IDENTITY:
        value = data.get(key, parsed.get(key))
        if value not in (None, ""):
            kept[key] = value
    budget = _PAGE_EVIDENCE_SUMMARY_CHARS - _compact_length(kept)
    for key in _PAGE_EVIDENCE_FACT_LISTS:
        value = data.get(key)
        if budget <= 0 or not isinstance(value, list) or not value:
            continue
        entries = _bounded_evidence_entries(value, budget)
        if entries:
            kept[key] = entries
            budget -= _compact_length(entries)
    challenge_state = data.get("challenge_state")
    if isinstance(challenge_state, dict) and challenge_state.get("detected"):
        kept["challenge_state"] = {
            key: challenge_state.get(key) for key in ("detected", "kind", "source") if challenge_state.get(key)
        }
    indicators = data.get("anti_bot_indicators")
    if isinstance(indicators, list) and indicators:
        kept["anti_bot_indicators"] = indicators[:8]
    return kept


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
    # Compaction must not launder untrusted MCP data into unlabelled context. The owned value is
    # re-stamped rather than copied, so this is not where an attacker-chosen provenance survives.
    if MCP_RESULT_PROVENANCE_KEY in parsed:
        synopsis[MCP_RESULT_PROVENANCE_KEY] = MCP_RESULT_PROVENANCE_VALUE
    if "ok" in parsed:
        synopsis["ok"] = parsed["ok"]
    if parsed.get("error"):
        synopsis["error"] = str(parsed["error"])[:200]

    data = parsed.get("data")
    if isinstance(data, dict) and _is_page_evidence(data):
        synopsis["page_evidence"] = _summarize_page_evidence(parsed, data)
        synopsis["_summarized"] = "older page evidence — bounded facts retained, raw excerpts dropped"
        try:
            return json.dumps(synopsis, separators=(",", ":"))
        except (TypeError, ValueError):
            return _truncated_output_fallback(output)

    if isinstance(data, dict):
        retained_packet = _retained_run_packet(data.get(BUILD_TEST_PACKET_KEY))
        if retained_packet is not None:
            synopsis[BUILD_TEST_PACKET_KEY] = retained_packet
        failing_line = data.get("failing_code_line")
        if type(failing_line) is int:
            synopsis["failing_code_line"] = failing_line
        code = data.get("code")
        if isinstance(code, str) and code:
            synopsis["code_chars_elided"] = len(code)
        for key in ("overall_status", "workflow_run_id", "failure_reason", "url", "message"):
            val = data.get(key)
            if val is None or val == "":
                continue
            synopsis[key] = val if isinstance(val, (bool, int, float)) else str(val)[:200]

        # An unresolved earlier failure survives compaction for the reason it is attached at all:
        # the model may decide whether to repair several turns after the run that passed, and dropping
        # it here would reproduce the loss it exists to prevent.
        unresolved = data.get("unresolved_earlier_failure")
        if isinstance(unresolved, dict) and unresolved:
            synopsis["unresolved_earlier_failure"] = unresolved

        change_identity = data.get("prior_attempt_change_identity")
        if isinstance(change_identity, dict) and change_identity:
            synopsis["prior_attempt_change_identity"] = change_identity

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
                codes = block.get("error_codes")
                if isinstance(codes, list) and codes:
                    entry["error_codes"] = _bounded_error_codes(codes)
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
    except (AttributeError, TypeError):
        LOG.debug(
            "Could not rewrite input-list item field; leaving untouched",
            field=name,
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

    screenshot_dropped = any(is_screenshot_message(item) for item in items[1:])
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
    retained_items = [opening, *retained_tail]
    screenshot_dropped_signal = _assemble_enforcement_messages(None, _nudge(None, "screenshot_dropped"))
    signal_content = _item_field(screenshot_dropped_signal[0], "content")
    if not screenshot_dropped or any(_item_field(item, "content") == signal_content for item in retained_items):
        screenshot_dropped_signal = []
    return [*retained_items, *screenshot_dropped_signal]


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
    "screenshot_dropped": "screenshot_dropped_on_recovery",
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
        with pending_operation("session.prune"):
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
    session: Session | None,
    tracked_stream: _SendTrackingStream,
    runner_kwargs: dict[str, Any],
    start_time: float,
    iteration: int,
) -> Any:
    """Run ``Runner.run_streamed`` + ``stream_to_sse`` under the hard watchdog.

    The top-of-loop elapsed check only fires between iterations; a
    long-running tool inside ``Runner.run_streamed`` needs this watchdog
    to raise ``CopilotTotalTimeoutError`` after the soft budget and drain
    allowance are both spent. The live watchdog is
    published on the context so a tool that parks on a user decision (the
    credential card) can suspend it for the length of that wait.

    ``MIN_DEADLINE_REMAINING_SECONDS`` floors ``remaining`` so a zero
    timeout never panics on an already-spent budget.
    """
    elapsed = _elapsed_run_seconds(ctx, start_time)
    remaining = max(
        MIN_DEADLINE_REMAINING_SECONDS,
        TOTAL_TIMEOUT_SECONDS + HARD_BACKSTOP_ALLOWANCE_SECONDS - elapsed,
    )
    with pending_operation("turn.stream", span=True):
        result = Runner.run_streamed(agent, input=current_input, context=ctx, session=session, **runner_kwargs)

        def check_model_work_deadline() -> None:
            if _elapsed_run_seconds(ctx, start_time) >= TOTAL_TIMEOUT_SECONDS:
                # SDK after_turn finishes pending tools and persists their outputs before
                # returning, so the drain can see them without another ordinary model call.
                result.cancel(mode="after_turn")

        ctx.check_model_work_deadline = None if ctx.budget_expiry_state.drain_active else check_model_work_deadline
        try:
            try:
                async with asyncio.timeout(remaining) as deadline:
                    ctx.model_stream_deadline = deadline
                    await streaming_adapter.stream_to_sse(result, tracked_stream, ctx)
            finally:
                ctx.model_stream_deadline = None
                ctx.check_model_work_deadline = None
                # A request_credential call the SDK rejected before its handler ran leaves the gate
                # armed with no releaser, and arm_credential_pause_gate skips a stranded Event, so
                # every later response's run tools would park on it. A rejected call never sets the
                # in-flight flag, so this still cannot open the gate under a card that is on screen.
                if not ctx.credential_ask_in_flight:
                    release_credential_pause_gate(ctx)
                _accumulate_usage(result, ctx)
        except TimeoutError:
            ctx.budget_expiry_state.hard_backstop_reached = True
            _mark_copilot_total_timeout(ctx, elapsed_seconds=_elapsed_run_seconds(ctx, start_time), iteration=iteration)
            raise CopilotTotalTimeoutError() from None
    return result


def _mark_budget_expiry(ctx: CopilotContext, source: BudgetExpirySource) -> None:
    state = ctx.budget_expiry_state
    if state.source is None:
        state.source = source
    if source == "deadline":
        ctx.copilot_total_timeout_exceeded = True
    else:
        ctx.copilot_max_turns_exceeded = True
    if state.staged_draft_id is None and ctx.staged_workflow is not None:
        state.staged_draft_id = ctx.staged_workflow.workflow_id


async def _run_budget_drain(
    agent: Agent,
    ctx: CopilotContext,
    session: Session | None,
    stream: EventSourceStream,
    runner_kwargs: dict[str, Any],
    start_time: float,
    iteration: int,
    source: BudgetExpirySource,
) -> RunResultStreaming:
    _mark_budget_expiry(ctx, source)
    state = ctx.budget_expiry_state
    if state.drain_attempted:
        raise MaxTurnsExceeded("Budget drain already attempted")
    drain_kwargs = dict(runner_kwargs)
    drain_kwargs["max_turns"] = BUDGET_DRAIN_HEADROOM
    observation = serialize_budget_expiry_observation(
        source=state.source or source,
        headroom=BUDGET_DRAIN_HEADROOM,
        staged_draft=state.staged_draft_id,
    )
    state.begin_drain()
    LOG.info(
        "copilot_budget_drain_started",
        source=state.source,
        drain_fingerprint=state.drain_fingerprint,
        staged_draft_id=state.staged_draft_id,
    )
    try:
        result = await _run_streamed_with_deadline(
            agent,
            observation,
            ctx,
            session,
            _SendTrackingStream(stream),
            drain_kwargs,
            start_time,
            iteration,
        )
    finally:
        state.drain_active = False
    return result


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
    warning_fingerprint = tuple(
        sorted(
            (
                criterion.id,
                criterion.outcome,
                criterion.requested_output_floor_rekeyed,
                criterion.floor_rekeyed_from_path or "",
            )
            for criterion in unregisterable
        )
    )
    if warning_fingerprint != ctx.pre_run_gated_output_warning_fingerprint:
        ctx.pre_run_gated_output_warning_fingerprint = warning_fingerprint
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


def uncovered_requested_output_paths(ctx: AgentContext) -> set[str]:
    """Requested-output paths not yet credited by scouted evidence. A path whose identifying
    tokens are all generic (e.g. ``output.data``) is uncoverable by token match and is exempted,
    so it falls through to the shape heuristic instead of pinning the gate open forever."""
    requested = _requested_output_paths_for_ctx(ctx)
    if not requested:
        return set()
    tokens_by_path = _requested_output_coverage_tokens(ctx)
    covered: set[str] = set(ctx.scouted_output_covered_paths)
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
    """Engaged credentials (username/password fills) must have every required field filled plus a
    post-fill submit before the synthesized trajectory may grade goal-complete."""
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


def _trajectory_reaches_generic_goal(
    trajectory: list[Any],
    *,
    allow_intermediate_interactions: bool = False,
) -> bool:
    """Apply the established open-to-commit and durable-entry reach shapes to one trajectory slice."""
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


def _trajectory_reaches_post_credential_commit(ctx: AgentContext) -> bool:
    """Apply the ordinary reach shapes only to the business spine after the credential submit."""
    trajectory = ctx.scout_trajectory
    if not trajectory:
        return False
    interactions = [item for item in trajectory if isinstance(item, dict)]
    credential_index = _last_scout_credential_fill_index(interactions)
    if credential_index is None:
        return _trajectory_reaches_generic_goal(
            interactions,
            allow_intermediate_interactions=True,
        )
    credential_submit_index = credential_submit_boundary_index(interactions, credential_index)
    if credential_submit_index is None:
        return False
    return _trajectory_reaches_generic_goal(
        interactions[credential_submit_index + 1 :],
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


def _assemble_enforcement_messages(
    screenshot_msg: dict[str, Any] | None,
    nudge_content: str | None,
) -> list[Any]:
    """Build the extra messages for an enforcement retry, ordered so a nudge, when present, stays last.

    The screenshot rides as its own user-role message because OpenAI rejects image parts inside a tool message.
    """
    extra_msgs: list[Any] = []
    if screenshot_msg is not None:
        extra_msgs.append(screenshot_msg)
    if nudge_content is not None:
        extra_msgs.append({"role": "user", "content": NUDGE_SENTINEL + nudge_content})
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
    install_pending_operation_slot(ctx)
    iteration = 0
    pending_recovery_nudge: str | None = None

    while True:
        # Client disconnect is no longer treated as a stop signal. The
        # SSE stream silently drops events once the browser is gone, but
        # the agent keeps running so the reply can be persisted to the
        # chat history on the server side (see SKY-8986).
        elapsed = _elapsed_run_seconds(ctx, start_time)
        if iteration > 0 and elapsed >= TOTAL_TIMEOUT_SECONDS:
            _mark_copilot_total_timeout(ctx, elapsed_seconds=elapsed, iteration=iteration)
            return await _run_budget_drain(
                agent,
                ctx,
                session,
                stream,
                runner_kwargs,
                start_time,
                iteration,
                "deadline",
            )

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
            current_runner_kwargs = runner_kwargs
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
                _record_copilot_cancellation(ctx, start_time, iteration)
                raise
            except MaxTurnsExceeded:
                return await _run_budget_drain(
                    agent,
                    ctx,
                    session,
                    stream,
                    runner_kwargs,
                    start_time,
                    iteration,
                    "max_turns",
                )
            except Exception as e:
                if not _is_context_window_error(e):
                    raise
                if tracked_stream.emitted:
                    # The provider started streaming then aborted; retrying
                    # would double-emit frames to the client.
                    LOG.error(
                        "Context window exceeded after partial emission; not retrying",
                        iteration=iteration,
                        has_session=session is not None,
                    )
                    raise
                LOG.error(
                    "Context window exceeded, retrying with aggressive prune",
                    iteration=iteration,
                    has_session=session is not None,
                )
                try:
                    current_input, images_stripped = await _recover_from_context_overflow(session, current_input)
                except asyncio.CancelledError:
                    _record_copilot_cancellation(ctx, start_time, iteration)
                    raise
                # Unconditional: the staged frame never reaches current_input, so images_stripped
                # cannot see it, and the filter would re-append it to the retry we just shrank.
                frame_dropped = _consume_pending_screenshots(ctx) is not None
                if images_stripped or frame_dropped:
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
                    _record_copilot_cancellation(ctx, start_time, iteration)
                    raise
                except MaxTurnsExceeded:
                    return await _run_budget_drain(
                        agent,
                        ctx,
                        session,
                        stream,
                        runner_kwargs,
                        start_time,
                        iteration,
                        "max_turns",
                    )
                except Exception:
                    # Never retry twice; even a second overflow surfaces as a
                    # real failure rather than spinning.
                    LOG.error(
                        "Context window recovery retry failed",
                        iteration=iteration,
                        has_session=session is not None,
                    )
                    raise

        elapsed = _elapsed_run_seconds(ctx, start_time)
        if elapsed >= TOTAL_TIMEOUT_SECONDS:
            _mark_copilot_total_timeout(ctx, elapsed_seconds=elapsed, iteration=iteration)
            return await _run_budget_drain(
                agent,
                ctx,
                session,
                stream,
                runner_kwargs,
                start_time,
                iteration,
                "deadline",
            )

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
                # The pause resolved without a frame; terminal runtime evidence is
                # still propagated before the turn returns.
                decision = enforcement_decision(ctx, result, copilot_config)
            if decision is None:
                _consume_pending_screenshots(ctx)
                _maybe_raise_non_retriable_nav(ctx)
                return result

        nudge_type = _NUDGE_TYPE_BY_KEY.get(decision.rule, decision.rule)
        LOG.info("Enforcement nudge", nudge_type=nudge_type, iteration=iteration)

        # OpenAI rejects images in tool messages, so a queued post-run
        # screenshot rides as its own user message just before the nudge.
        screenshot_msg = _consume_pending_screenshots(ctx)
        if screenshot_msg is not None:
            LOG.info("Injecting screenshot user message", count=len(screenshot_msg["content"]) - 1)

        with copilot_span("enforcement_nudge", data={"nudge_type": nudge_type, "iteration": iteration}):
            extra_msgs = _assemble_enforcement_messages(screenshot_msg, decision.message)
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
