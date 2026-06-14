"""Enforcement wrapper — nudge agent when it skips required steps."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog
from agents.run import Runner

from skyvern.config import settings
from skyvern.forge.sdk.copilot import config as copilot_config_defaults
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, stash_blocker_signal
from skyvern.forge.sdk.copilot.build_phase import DISCOVERY_PERMITTED_PHASES
from skyvern.forge.sdk.copilot.code_block_synthesis import render_synthesized_offer_text, synthesize_code_block
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
    POST_REPEATED_NULL_DATA_NUDGE,
    POST_SUSPICIOUS_SUCCESS_NUDGE,
    POST_UPDATE_NUDGE,
    PRE_DISCOVERY_URL_QUESTION_NUDGE,
    SCREENSHOT_DROPPED_NUDGE,
    BlockAuthoringPolicy,
    CopilotConfig,
    normalize_block_authoring_policy,
)
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import RepairNextAction
from skyvern.forge.sdk.copilot.failure_tracking import PER_TOOL_BUDGET_FAILURE_CATEGORY, normalize_failure_reason
from skyvern.forge.sdk.copilot.narration import TransitionKind
from skyvern.forge.sdk.copilot.output_policy import normalize_response_scaffolding
from skyvern.forge.sdk.copilot.output_utils import (
    extract_final_text,
    looks_like_workflow_delivery_claim,
    parse_final_response,
)
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_halt import raise_if_turn_halt, stash_turn_halt_from_blocker_signal
from skyvern.utils.token_counter import count_tokens

if TYPE_CHECKING:
    from agents.agent import Agent
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.copilot.context import CopilotContext
    from skyvern.forge.sdk.copilot.runtime import AgentContext
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

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
# Escalate after this many consecutive all-null extraction runs so the agent
# inspects browser state instead of re-prompting the extractor.
NULL_DATA_STREAK_ESCALATE_AT = 2
# Streak levels for repeated-failure (same frontier + same failure signature).
REPEATED_FRONTIER_STREAK_ESCALATE_AT = 2
REPEATED_FRONTIER_STREAK_STOP_AT = 3
# Stop after this many consecutive runs where navigation succeeded but the
# scraper could not read the page. Aligned with MAX_FAILED_TEST_NUDGES so the
# copilot gets one generic retry nudge, then stops on the second occurrence.
PROBABLE_SITE_BLOCK_STREAK_STOP_AT = 2
UNRECOVERABLE_TOOL_ERROR_STOP_AT = 2
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
MIN_BLOCKS_FOR_AUTO_COMPLETE = 10
TOTAL_TIMEOUT_SECONDS = 900
# Belt-and-braces cap alongside the elapsed-time budget. Per-nudge caps
# already prevent individual branches from looping; this stops a brand-new
# enforcement rule that forgets its own counter from spinning within 900s.
MAX_ITERATIONS = 50
SCREENSHOT_SENTINEL = "[copilot:screenshot] "
NUDGE_SENTINEL = "[copilot:nudge] "
SCREENSHOT_PLACEHOLDER = SCREENSHOT_SENTINEL + "[prior screenshot removed to save context]"
TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET
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


class CopilotTotalTimeoutError(Exception):
    """Raised when the copilot agent exceeds the total allowed runtime."""


class CopilotGoalSatisfied(Exception):
    """Raised when a tool proves the workflow already satisfies the turn."""


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


def _outcome_criteria_evaluated(ctx: CopilotContext) -> bool:
    if not settings.COPILOT_OUTCOME_VERIFICATION_ENABLED:
        return False
    result = ctx.completion_verification_result
    return result is not None and result.status == "evaluated"


def outcome_fully_verified(ctx: CopilotContext) -> bool:
    """The judge confirmed every outcome criterion from the evidence this run produced.

    This evidence is authoritative over run status: a run that reached the goal is
    recognized even when it was canceled or only partially completed. Run status must
    never suppress recognition of an outcome the user can observe was achieved.
    """
    if not _outcome_criteria_evaluated(ctx):
        return False
    result = ctx.completion_verification_result
    return result is not None and result.is_fully_satisfied()


def verified_goal_satisfied_context(ctx: CopilotContext) -> bool:
    if outcome_fully_verified(ctx):
        return True
    # The judge verdict is authoritative: once it has evaluated, an unconfirmed
    # criterion means the outcome is unmet regardless of run status. The block-count
    # heuristic (which counts method verbs in the request) governs only when there
    # is no evaluated verdict.
    if _outcome_criteria_evaluated(ctx):
        return False
    if not (
        ctx.last_test_ok is True
        and ctx.last_full_workflow_test_ok is True
        and latest_diagnosis_contract_satisfies_goal(ctx)
    ):
        return False
    return not _verified_goal_likely_needs_more_work(ctx)


def verified_goal_claim_authorized(ctx: CopilotContext) -> bool:
    """Whether the terminal may CLAIM a tested success. Turn completion keeps
    flowing through ``verified_goal_satisfied_context``; with persisted criteria
    enabled, the claim tier additionally requires judge-confirmed outcome evidence —
    criteria-less or judge-less terminals end the turn but render built-but-unverified.
    With either flag off the judge cannot be authoritative, so the claim falls back
    to the legacy gate."""
    if not (settings.COPILOT_PERSISTED_COMPLETION_CRITERIA_ENABLED and settings.COPILOT_OUTCOME_VERIFICATION_ENABLED):
        return verified_goal_satisfied_context(ctx)
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
        "gate_claim_authorized": verified_goal_claim_authorized(ctx),
        "gate_last_test_ok": ctx.last_test_ok is True,
        "gate_last_full_workflow_test_ok": ctx.last_full_workflow_test_ok is True,
        "gate_diagnosis_contract_satisfies_goal": latest_diagnosis_contract_satisfies_goal(ctx),
        "gate_outcome_criteria_evaluated": _outcome_criteria_evaluated(ctx),
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


def _mark_copilot_total_timeout_if_elapsed(ctx: Any, start_time: float) -> None:
    if time.monotonic() - start_time >= TOTAL_TIMEOUT_SECONDS:
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


class CopilotUnrecoverableToolError(Exception):
    """Raised when browser-session tool failures prove the current loop cannot recover."""

    def __init__(self, tool_name: str, error_message: str) -> None:
        self.tool_name = tool_name
        self.error_message = error_message
        super().__init__(f"Unrecoverable tool error in {tool_name}: {error_message}")


_BROWSER_SESSION_TOOL_NAMES = frozenset(
    {
        "navigate_browser",
        "get_browser_screenshot",
        "evaluate",
        "click",
        "type_text",
        "scroll",
        "console_messages",
        "select_option",
        "press_key",
    }
)
_POST_RUN_PAGE_OBSERVATION_TOOLS = frozenset({"evaluate", "get_browser_screenshot", "inspect_page_for_composition"})
_UNRECOVERABLE_TOOL_ERROR_CATEGORY = "UNRECOVERABLE_TOOL_ERROR"
_BROWSER_SESSION_ID_RE = re.compile(r"\bpbs_[A-Za-z0-9_-]+\b")
_BROWSER_SESSION_WITH_ID_RE = re.compile(r"\bbrowser session\s+pbs_[A-Za-z0-9_-]+\b", re.IGNORECASE)


def redact_browser_session_references(value: str) -> str:
    value = _BROWSER_SESSION_WITH_ID_RE.sub("Browser session", value)
    return _BROWSER_SESSION_ID_RE.sub("the browser session", value)


def _result_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_result_text_values(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_result_text_values(item))
        return result
    return []


def _unrecoverable_tool_error_reason(output: dict[str, Any]) -> str:
    raw_reason = output.get("error")
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        data = output.get("data")
        raw_reason = data.get("failure_reason") if isinstance(data, dict) else None
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        raw_reason = " ".join(_result_text_values(output))
    reason = " ".join(str(raw_reason or "Browser session was no longer reachable.").split())
    reason = redact_browser_session_references(reason)
    return reason[:240].rstrip()


def _is_unrecoverable_browser_session_error(tool_name: str, output: dict[str, Any]) -> bool:
    if tool_name not in _BROWSER_SESSION_TOOL_NAMES or output.get("ok", True):
        return False
    lowered = " ".join(_result_text_values(output)).lower()
    if "no browser context" in lowered:
        return True
    has_session_signal = "browser session" in lowered or "browser context" in lowered
    has_lost_signal = "not found" in lowered or "404" in lowered
    return has_session_signal and has_lost_signal


def _record_unrecoverable_tool_error_contract(ctx: Any, tool_name: str, reason: str) -> None:
    from skyvern.forge.sdk.copilot.diagnosis_repair_contract import build_diagnosis_repair_contract

    result = {
        "ok": False,
        "error": reason,
        "data": {
            "overall_status": "aborted",
            "failure_reason": reason,
            "failure_categories": [{"category": _UNRECOVERABLE_TOOL_ERROR_CATEGORY, "reasoning": reason}],
        },
    }
    contract = build_diagnosis_repair_contract(source_tool=tool_name, result=result, ctx=ctx)
    ctx.latest_diagnosis_repair_contract = contract
    ctx.unrecoverable_tool_error_reason = reason
    ctx.unrecoverable_tool_error_tool_name = tool_name
    ctx.last_test_failure_reason = reason
    trace_data = contract.to_trace_data()
    LOG.warning(
        "Copilot unrecoverable tool error stop",
        tool_name=tool_name,
        error_reason=reason,
        **{f"diagnosis_repair_{key}": value for key, value in trace_data.items()},
    )
    with copilot_span("copilot_unrecoverable_tool_error", data={"tool_name": tool_name, **trace_data}):
        pass


def _maybe_raise_unrecoverable_tool_error(ctx: Any, tool_name: str, output: dict[str, Any]) -> None:
    if not _is_unrecoverable_browser_session_error(tool_name, output):
        if tool_name in _BROWSER_SESSION_TOOL_NAMES and output.get("ok", False):
            ctx.unrecoverable_tool_error_streak_count = 0
            ctx.unrecoverable_tool_error_signature = None
        return

    reason = _unrecoverable_tool_error_reason(output)
    signature = "browser_session_unreachable"
    prior_signature = getattr(ctx, "unrecoverable_tool_error_signature", None)
    prior_count = getattr(ctx, "unrecoverable_tool_error_streak_count", 0)
    prior_count = prior_count if isinstance(prior_count, int) else 0
    count = prior_count + 1 if prior_signature == signature else 1
    ctx.unrecoverable_tool_error_signature = signature
    ctx.unrecoverable_tool_error_streak_count = count
    ctx.unrecoverable_tool_error_reason = reason
    ctx.unrecoverable_tool_error_tool_name = tool_name

    if count >= UNRECOVERABLE_TOOL_ERROR_STOP_AT:
        _record_unrecoverable_tool_error_contract(ctx, tool_name, reason)
        raise CopilotUnrecoverableToolError(tool_name, reason)


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
        if isinstance(block_count, int) and _goal_likely_needs_more_blocks(
            user_message, block_count, completion_contract
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


def _needs_repeated_null_data_nudge(ctx: Any) -> bool:
    """Return True when suspicious-success has happened enough times to escalate."""
    # Same as above: non-retriable nav state never belongs on this branch.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return False
    if not getattr(ctx, "last_test_suspicious_success", False):
        return False
    streak = getattr(ctx, "null_data_streak_count", 0)
    return streak >= NULL_DATA_STREAK_ESCALATE_AT


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
    # Terminal failure-mode signals must pre-empt tool-call hygiene nudges.
    terminal_signal = getattr(ctx, "latest_tool_blocker_signal", None) or getattr(ctx, "blocker_signal", None)
    if terminal_signal is not None:
        stash_turn_halt_from_blocker_signal(ctx, terminal_signal, source="enforcement_backstop")
    raise_if_turn_halt(ctx)
    _raise_if_unrecoverable_contract_stop(ctx)

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
    if _needs_repeated_null_data_nudge(ctx):
        return _nudge(config, "post_repeated_null_data")

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
        raise_if_turn_halt(ctx)

    if _needs_failed_test_nudge(ctx):
        ctx.failed_test_nudge_count += 1
        if _needs_inspect_before_repair_nudge(ctx):
            return _nudge(config, "post_failed_test_inspect_first")
        return _nudge(config, "post_failed_test")

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
    POST_REPEATED_NULL_DATA_NUDGE: "repeated_null_data",
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
    "post_repeated_null_data": "repeated_null_data",
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

    ``max(1.0, ...)`` floors ``remaining`` so ``wait_for(timeout=0)``
    never panics on an already-spent budget.
    """
    from skyvern.forge.sdk.copilot.streaming_adapter import stream_to_sse

    elapsed = time.monotonic() - start_time
    remaining = max(1.0, TOTAL_TIMEOUT_SECONDS - elapsed)
    result = Runner.run_streamed(agent, input=current_input, context=ctx, session=session, **runner_kwargs)
    try:
        try:
            await asyncio.wait_for(stream_to_sse(result, tracked_stream, ctx), timeout=remaining)
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
    one-shot latch with the pre-authoring prompt-side offer, so whichever fires
    first wins and the model sees the offer at most once.
    """
    if getattr(ctx, "synthesized_block_offered", False):
        return None
    if getattr(ctx, "update_workflow_called", False):
        return None
    if normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None)) != (
        BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ):
        return None
    trajectory = getattr(ctx, "scout_trajectory", None) or []
    if not trajectory:
        return None
    synthesized = synthesize_code_block(trajectory)
    if synthesized is None:
        return None

    ctx.synthesized_block_offered = True
    return {"role": "user", "content": render_synthesized_offer_text(synthesized, trajectory)}


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
        elapsed = time.monotonic() - start_time
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
            try:
                result = await _run_streamed_with_deadline(
                    agent,
                    current_input,
                    ctx,
                    session,
                    tracked_stream,
                    runner_kwargs,
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
                        runner_kwargs,
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

        if nudge is None and synthesized_msg is None:
            _consume_pending_screenshots(ctx)
            _maybe_raise_non_retriable_nav(ctx)
            return result

        if nudge is not None and nudge == _nudge(copilot_config, "post_update"):
            if ctx.post_update_nudge_count >= MAX_POST_UPDATE_NUDGES:
                LOG.warning(
                    "Enforcement exhausted post-update nudges, allowing response",
                    nudge_count=ctx.post_update_nudge_count,
                )
                _consume_pending_screenshots(ctx)
                _maybe_raise_non_retriable_nav(ctx)
                return result
            ctx.post_update_nudge_count += 1

        nudge_type = _nudge_type_for_log(nudge, copilot_config) if nudge is not None else "synthesized_block_offer"
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
