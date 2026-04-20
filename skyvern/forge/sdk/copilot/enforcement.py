"""Enforcement wrapper — nudge agent when it skips required steps."""

from __future__ import annotations

import copy
import json
import re
import time
from typing import TYPE_CHECKING, Any

import structlog
from agents.run import Runner

from skyvern.forge.sdk.copilot.failure_tracking import normalize_failure_reason
from skyvern.forge.sdk.copilot.output_utils import extract_final_text, parse_final_response
from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.utils.token_counter import count_tokens

if TYPE_CHECKING:
    from agents.agent import Agent
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

MAX_POST_UPDATE_NUDGES = 2
MAX_INTERMEDIATE_NUDGES = 8
MAX_FAILED_TEST_NUDGES = 2
MAX_FORMAT_NUDGES = 2
MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES = 2
# Escalate after this many consecutive all-null extraction runs so the agent
# inspects browser state instead of re-prompting the extractor.
NULL_DATA_STREAK_ESCALATE_AT = 2
# Streak levels for repeated-failure (same frontier + same failure signature).
REPEATED_FRONTIER_STREAK_ESCALATE_AT = 2
REPEATED_FRONTIER_STREAK_STOP_AT = 3
MIN_BLOCKS_FOR_AUTO_COMPLETE = 10
TOTAL_TIMEOUT_SECONDS = 600
# Belt-and-braces cap alongside the elapsed-time budget. Per-nudge caps
# already prevent individual branches from looping; this stops a brand-new
# enforcement rule that forgets its own counter from spinning within 600s.
MAX_ITERATIONS = 50

SCREENSHOT_SENTINEL = "[copilot:screenshot] "
NUDGE_SENTINEL = "[copilot:nudge] "
SCREENSHOT_PLACEHOLDER = SCREENSHOT_SENTINEL + "[prior screenshot removed to save context]"
SCREENSHOT_DROPPED_NUDGE = (
    "Your previous screenshot was dropped from context to recover from a token-budget overflow. "
    "Do NOT reason about the page from memory. Re-take the screenshot "
    "(get_browser_screenshot) or call evaluate before deciding your next step."
)
TOKEN_BUDGET = 90_000
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

POST_UPDATE_NUDGE = (
    "You updated the workflow but did not test it. "
    "You MUST call run_blocks_and_collect_debug (or update_and_run_blocks next time) "
    "to test at least the first block before responding to the user. "
    "This verifies the workflow actually works."
)

POST_NAVIGATE_NUDGE = (
    "You navigated to a page but did not observe its content. "
    "You MUST use evaluate, get_browser_screenshot, click, type_text, "
    "scroll, select_option, press_key, or console_messages "
    "to inspect the page before responding. Do NOT answer from memory."
)

POST_INTERMEDIATE_SUCCESS_NUDGE = (
    "STOP — do NOT respond to the user yet. "
    "Your workflow only covers a subset of what the user asked for. "
    "You MUST add the next block now: call update_and_run_blocks with the current "
    "block chain. The tool preserves verified prefix state and reruns only the "
    "invalidated frontier, so passing the full chain is cheap. "
    "Only respond to the user when every distinct action they requested is covered "
    "by a workflow block, or you have clear evidence that continuing is infeasible."
)

POST_FAILED_TEST_NUDGE = (
    "STOP — your last test run FAILED. Do NOT respond to the user yet.\n"
    "1. First, call get_run_results — pass the workflow_run_id from the prior "
    "update_and_run_blocks or run_blocks_and_collect_debug response to make the "
    "lookup unambiguous. That returns per-block failure_reason, output, and any "
    "failed-block screenshots, which is the diagnostic data you need.\n"
    "2. Then decide: if the failure looks fixable (wrong goal wording, popup "
    "blocking, timeout, element not found), adjust the workflow with a DIFFERENT "
    "approach and call update_and_run_blocks again — the tool will rerun from "
    "the earliest invalidated block so only the changed part is retested.\n"
    "3. If you have now failed multiple times with genuinely different approaches "
    "and the evidence strongly suggests the site cannot satisfy the request, "
    "respond explaining exactly what you tried and what blocked you.\n"
    "Do NOT resubmit the same workflow — you must change something substantive."
)

POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE = (
    "STOP — you explored the page using direct browser tools but did NOT engage "
    "the workflow path. You MUST follow the WORKFLOW-FIRST EXECUTION PATH:\n"
    "1. If no workflow exists yet, call update_workflow with at least a navigation "
    "block for the target URL.\n"
    "2. If a workflow already exists, call run_blocks_and_collect_debug to test it.\n"
    "3. Use the test results to decide next steps.\n"
    "Do NOT make feasibility judgments from browser exploration alone — "
    "build and test workflow blocks first."
)

POST_SUSPICIOUS_SUCCESS_NUDGE = (
    "STOP — your last test run completed (status=completed) but data-producing "
    "blocks (extraction/text_prompt) produced no meaningful output "
    "(missing, empty, or all-null fields). This is NOT a success.\n"
    "1. Call get_run_results to inspect what each block actually returned.\n"
    "2. If the extraction/text_prompt block returned empty, all-null, or "
    "irrelevant data, the upstream block likely fetched an error page "
    "(e.g. 403, CAPTCHA, 'no results'), landed on the wrong page, or the "
    "data is rendered differently than expected.\n"
    "3. Use get_browser_screenshot or evaluate to inspect what the workflow "
    "browser actually sees — do NOT just retry extraction with a different prompt.\n"
    "4. Fix the root cause — do NOT declare the workflow working based on "
    "status alone. Verify the actual extracted data answers the user's question."
)

POST_REPEATED_NULL_DATA_NUDGE = (
    "STOP — you have now produced multiple consecutive test runs where "
    "extraction/text_prompt blocks returned all-null or empty data. "
    "Re-prompting the extractor is not working — the problem is almost "
    "certainly NOT how the extraction goal is worded.\n"
    "You MUST now do ONE of the following before another update_workflow call:\n"
    "1. Call get_browser_screenshot on the workflow's browser session to see "
    "exactly what page the workflow is actually loading (it may differ from "
    "what you expect — e.g. a 'no results' fallback, cookie wall, or bot block).\n"
    "2. Call evaluate with JavaScript that searches for the expected content "
    "on the workflow's browser — confirm whether the data is even present.\n"
    "3. If the page the workflow loads genuinely does not contain the data, "
    "pivot to a different URL or source entirely — do NOT keep retrying "
    "extraction against the same failing page.\n"
    "Do NOT call update_and_run_blocks again until you have concrete evidence "
    "about what the workflow browser is actually seeing."
)

POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE = (
    "STOP — this is the second run with the same frontier and the same failure "
    "signature. Re-running the same change again is unlikely to help.\n"
    "Before another update_and_run_blocks call, you MUST:\n"
    "1. Call get_run_results to inspect the full failure evidence (per-block "
    "failure_reason, action_trace, and any failed-block screenshots).\n"
    "2. If the evidence is still ambiguous, use get_browser_screenshot or evaluate "
    "to check what the workflow browser is actually seeing.\n"
    "3. Then make a materially different change — different block ordering, a "
    "different selector strategy, a different entry URL, or different parameters. "
    "Changes to wording of the same prompt do not count as materially different."
)

POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE = (
    "STOP — you have now attempted the same frontier with the same failure "
    "signature THREE times without making progress. Do NOT call "
    "update_and_run_blocks or run_blocks_and_collect_debug again on this "
    "frontier.\n"
    "Choose ONE:\n"
    "A) Finalize now with a clear blocker explanation that references the "
    "specific failure_reason and failure_categories you observed.\n"
    "B) If required user input is missing (credential, ambiguous goal, "
    "site-specific detail), respond with an ASK_QUESTION instead. Do not "
    "retry the same repair again."
)

POST_PARAMETER_BINDING_WARN_NUDGE = (
    "STOP — your last test run failed with a PARAMETER_BINDING_ERROR. "
    "This is an INTERNAL workflow configuration mismatch, not a site or "
    "selector problem.\n"
    "The workflow definition references a parameter (by Jinja key) that is "
    "not in the top-level workflow parameters list, or the list declares a "
    "parameter the blocks do not use.\n"
    "Do NOT retry with different selectors, URLs, or navigation changes — "
    "those will not help. Instead:\n"
    "1. Reconcile the workflow's top-level parameters with what the blocks "
    "actually reference via {{ parameters.<key> }}.\n"
    "2. Inline one-off literals rather than adding a parameter for each.\n"
    "3. Then call update_and_run_blocks again with a corrected YAML and, "
    "for any remaining parameters, concrete values passed via the "
    "`parameters` argument."
)

POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE = (
    "STOP — the target URL is unreachable and further retries cannot succeed. "
    "The navigation failed with a permanent error (DNS resolution, SSL/cert, "
    "or invalid URL). Do NOT retry and do NOT edit the workflow. Reply to the "
    "user now: state that the URL could not be reached, quote the exact error "
    "message from the last failure_reason, and ask them to verify the URL."
)

POST_PARAMETER_BINDING_STOP_NUDGE = (
    "STOP — you have retried the same PARAMETER_BINDING_ERROR multiple times "
    "without reconciling the workflow configuration. Do NOT call "
    "update_and_run_blocks or run_blocks_and_collect_debug again until the "
    "workflow parameters list matches the block references.\n"
    "Choose ONE:\n"
    "A) Finalize now with a blocker explanation that names the specific "
    "parameter keys that are out of sync.\n"
    "B) If you need missing values from the user (credential, identifier) "
    "to decide what belongs in the parameters list, respond with an "
    "ASK_QUESTION instead. Do not resubmit a workflow that still has the "
    "same parameter-binding drift."
)

POST_ANTI_BOT_FAILED_TEST_NUDGE = (
    "STOP — your last test run failed due to an anti-bot/WAF block "
    "(Access Denied, Cloudflare, Akamai, etc.).\n"
    "IMPORTANT: An HTTP_REQUEST or navigation block from the SAME server IP "
    "will almost certainly receive the same block. Do NOT retry with:\n"
    "- A simple wait/delay block (timing does not fix IP bans)\n"
    "- A raw HTTP_REQUEST to the same URL (same IP = same block)\n"
    "Instead, try:\n"
    "1. Set proxy_location on the workflow to route through a different IP.\n"
    "2. If proxy is not available, explain to the user that the site has "
    "anti-bot protection that requires proxy configuration.\n"
    "Do NOT resubmit the same workflow with trivial changes."
)

POST_FORMAT_NUDGE = (
    "Your reply reads as a progress report, not a completed proposal. "
    "If you are not ready to finalize, emit ASK_QUESTION with a specific question. "
    "Otherwise, finish the workflow and present it as a completed proposal."
)

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


class CopilotTotalTimeoutError(Exception):
    """Raised when the copilot agent exceeds the total allowed runtime."""


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


_ACTION_CATEGORIES: list[list[str]] = [
    ["navigate", "go to", "open", "visit"],
    ["download", "save", "export"],
    ["extract", "scrape", "collect", "gather", "get all"],
    ["login", "log in", "sign in", "authenticate"],
    ["search", "find", "look for", "look up"],
    ["fill", "enter", "type", "submit", "complete the form"],
    ["click", "select", "choose", "pick"],
    ["upload", "attach"],
]

_SEQUENTIAL_CONNECTORS = [" and then ", " then ", " after that ", " next ", " followed by ", " afterward "]


def _goal_likely_needs_more_blocks(user_message: Any, block_count: int) -> bool:
    """Return True when the goal likely requires more blocks than currently exist."""
    if block_count >= MIN_BLOCKS_FOR_AUTO_COMPLETE:
        return False
    if not isinstance(user_message, str):
        return False
    text = user_message.lower()

    matched_categories = sum(1 for category in _ACTION_CATEGORIES if any(keyword in text for keyword in category))
    has_sequential = any(conn in text for conn in _SEQUENTIAL_CONNECTORS)

    estimated_min_blocks = max(matched_categories, 2) if has_sequential else matched_categories
    return block_count < estimated_min_blocks


def _response_coverage_nudge(ctx: Any, parsed: dict[str, Any]) -> str | None:
    """Peek at the model's final output and return a nudge for coverage gaps
    or progress-narration format. ASK_QUESTION is always let through so the
    agent can request missing credentials or disambiguation.

    Returns the nudge string to inject, or None to let the response through.
    """
    response_type = parsed.get("type")
    if response_type not in ("REPLY", "REPLACE_WORKFLOW"):
        return None

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
        if isinstance(block_count, int) and _goal_likely_needs_more_blocks(user_message, block_count):
            nudge_count = getattr(ctx, "coverage_nudge_count", 0)
            if nudge_count < MAX_INTERMEDIATE_NUDGES:
                ctx.coverage_nudge_count = nudge_count + 1
                return POST_INTERMEDIATE_SUCCESS_NUDGE

    if _is_progress_narration(parsed.get("user_response")):
        nudge_count = getattr(ctx, "format_nudge_count", 0)
        if nudge_count < MAX_FORMAT_NUDGES:
            ctx.format_nudge_count = nudge_count + 1
            return POST_FORMAT_NUDGE

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
    if getattr(ctx, "last_test_ok", None) is not False:
        return False
    if not getattr(ctx, "test_after_update_done", False):
        return False
    nudge_count = getattr(ctx, "failed_test_nudge_count", 0)
    return nudge_count < MAX_FAILED_TEST_NUDGES


def _needs_suspicious_success_nudge(ctx: Any) -> bool:
    """Return True when the last test 'completed' but data blocks had no output."""
    # A non-retriable nav failure cannot be "suspiciously successful" — defer
    # to the dedicated stop path rather than competing for the nudge slot.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return False
    return bool(getattr(ctx, "last_test_suspicious_success", False))


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


def _repeated_frontier_failure_nudge(ctx: Any) -> str | None:
    """Emit each escalation level at most once per streak. The streak itself
    keeps climbing on further identical failures (incremented elsewhere by
    update_repeated_failure_state), so the stop nudge fires naturally on the
    next repeat after a warn."""
    # Non-retriable nav errors get their own dedicated stop path; don't let a
    # repeated-frontier nudge smuggle different retry advice past the gate.
    if getattr(ctx, "last_test_non_retriable_nav_error", None):
        return None
    streak = _get_int(ctx, "repeated_failure_streak_count")
    emitted = _get_int(ctx, "repeated_failure_nudge_emitted_at_streak")
    top_category = getattr(ctx, "last_failure_category_top", None)
    is_param_binding = top_category == "PARAMETER_BINDING_ERROR"

    if streak >= REPEATED_FRONTIER_STREAK_STOP_AT and emitted < REPEATED_FRONTIER_STREAK_STOP_AT:
        return POST_PARAMETER_BINDING_STOP_NUDGE if is_param_binding else POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE
    if streak >= REPEATED_FRONTIER_STREAK_ESCALATE_AT and emitted < REPEATED_FRONTIER_STREAK_ESCALATE_AT:
        return POST_PARAMETER_BINDING_WARN_NUDGE if is_param_binding else POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE
    return None


_STOP_LEVEL_FRONTIER_NUDGES = frozenset({POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE, POST_PARAMETER_BINDING_STOP_NUDGE})


def _non_retriable_nav_error_nudge(ctx: Any) -> tuple[str, str] | None:
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
    return POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE, signature


def _check_enforcement(ctx: Any, result: RunResultStreaming | None = None) -> str | None:
    # Terminal failure-mode signals must pre-empt tool-call hygiene nudges.
    # A permanent navigation error (DNS / cert / SSL / invalid URL) cannot be
    # resolved by observing a prior navigate or by testing an updated
    # workflow against the same bad URL, so let it speak first.
    non_retriable = _non_retriable_nav_error_nudge(ctx)
    if non_retriable is not None:
        nudge_msg, signature = non_retriable
        ctx.non_retriable_nav_error_last_emitted_signature = signature
        return nudge_msg

    if ctx.navigate_called and not ctx.observation_after_navigate and not ctx.navigate_enforcement_done:
        ctx.navigate_enforcement_done = True
        return POST_NAVIGATE_NUDGE

    if _needs_explore_without_workflow_nudge(ctx):
        ctx.explore_without_workflow_nudge_count += 1
        return POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    if ctx.update_workflow_called and not ctx.test_after_update_done:
        return POST_UPDATE_NUDGE

    repeated_frontier_nudge = _repeated_frontier_failure_nudge(ctx)
    if repeated_frontier_nudge is not None:
        # Latch the emitted level so each escalation fires at most once per streak.
        ctx.repeated_failure_nudge_emitted_at_streak = (
            REPEATED_FRONTIER_STREAK_STOP_AT
            if repeated_frontier_nudge in _STOP_LEVEL_FRONTIER_NUDGES
            else REPEATED_FRONTIER_STREAK_ESCALATE_AT
        )
        return repeated_frontier_nudge

    # Do NOT clear last_test_suspicious_success here. tools._record_run_blocks_result
    # resets it on every new run; if the agent ignores the nudge and answers
    # without rerunning, we want _check_enforcement to re-emit the nudge.
    if _needs_repeated_null_data_nudge(ctx):
        return POST_REPEATED_NULL_DATA_NUDGE

    if _needs_suspicious_success_nudge(ctx):
        return POST_SUSPICIOUS_SUCCESS_NUDGE

    if _needs_failed_test_nudge(ctx):
        ctx.failed_test_nudge_count += 1
        if getattr(ctx, "last_test_anti_bot", None):
            return POST_ANTI_BOT_FAILED_TEST_NUDGE
        return POST_FAILED_TEST_NUDGE

    # Response-time gate: peek at the model's final output to tell ASK_QUESTION
    # (always allowed) from a REPLY with a coverage gap or progress-narration.
    # Only runs when no state-based nudge fired.
    if result is not None:
        parsed = parse_final_response(extract_final_text(result))
        return _response_coverage_nudge(ctx, parsed)

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
    POST_FAILED_TEST_NUDGE: "post_failed_test",
    SCREENSHOT_DROPPED_NUDGE: "screenshot_dropped_on_recovery",
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


async def run_with_enforcement(
    agent: Agent,
    initial_input: str | list,
    ctx: Any,
    stream: EventSourceStream,
    **runner_kwargs: Any,
) -> RunResultStreaming:
    """Run agent with enforcement nudges, preserving conversation history."""
    # Lazy import: streaming_adapter lives in a sibling PR in the stack.
    from skyvern.forge.sdk.copilot.streaming_adapter import stream_to_sse

    session = runner_kwargs.pop("session", None)
    current_input: str | list = initial_input
    start_time = time.monotonic()
    iteration = 0
    pending_recovery_nudge: str | None = None

    while True:
        # Client disconnect is no longer treated as a stop signal. The
        # SSE stream silently drops events once the browser is gone, but
        # the agent keeps running so the reply can be persisted to the
        # chat history on the server side (see SKY-8986).
        elapsed = time.monotonic() - start_time
        if elapsed > TOTAL_TIMEOUT_SECONDS:
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
            if est > TOKEN_BUDGET:
                LOG.warning("Token estimate exceeds budget, aggressively pruning", tokens=est, budget=TOKEN_BUDGET)
                current_input = aggressive_prune(current_input)

        tracked_stream = _SendTrackingStream(stream)
        with copilot_span(
            "enforcement_iteration",
            data={"iteration": iteration, "elapsed_seconds": round(elapsed, 3)},
        ):
            try:
                result = Runner.run_streamed(agent, input=current_input, context=ctx, session=session, **runner_kwargs)
                await stream_to_sse(result, tracked_stream, ctx)
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
                current_input, images_stripped = await _recover_from_context_overflow(session, current_input)
                if images_stripped:
                    # The agent could otherwise reason about the page from
                    # memory on the next turn; warn it explicitly.
                    pending_recovery_nudge = SCREENSHOT_DROPPED_NUDGE
                tracked_stream = _SendTrackingStream(stream)
                try:
                    result = Runner.run_streamed(
                        agent, input=current_input, context=ctx, session=session, **runner_kwargs
                    )
                    await stream_to_sse(result, tracked_stream, ctx)
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

        # Inject pending screenshots as a follow-up user message because OpenAI
        # rejects images in tool messages.
        screenshot_msg = _consume_pending_screenshots(ctx)
        if screenshot_msg is not None:
            LOG.info("Injecting screenshot user message", count=len(screenshot_msg["content"]) - 1)
            current_input = (
                [screenshot_msg]
                if session is not None
                else _prune_input_list(result.to_input_list()) + [screenshot_msg]
            )
            iteration += 1
            continue

        if pending_recovery_nudge is not None:
            nudge: str | None = pending_recovery_nudge
            pending_recovery_nudge = None
        else:
            nudge = _check_enforcement(ctx, result)
        if nudge is None:
            _maybe_raise_non_retriable_nav(ctx)
            return result

        if nudge == POST_UPDATE_NUDGE:
            if ctx.post_update_nudge_count >= MAX_POST_UPDATE_NUDGES:
                LOG.warning(
                    "Enforcement exhausted post-update nudges, allowing response",
                    nudge_count=ctx.post_update_nudge_count,
                )
                _maybe_raise_non_retriable_nav(ctx)
                return result
            ctx.post_update_nudge_count += 1

        nudge_type = _NUDGE_TYPE_BY_MESSAGE.get(nudge, "intermediate_success")
        LOG.info("Enforcement nudge", nudge_type=nudge_type, iteration=iteration)

        with copilot_span("enforcement_nudge", data={"nudge_type": nudge_type, "iteration": iteration}):
            nudge_msg = {"role": "user", "content": NUDGE_SENTINEL + nudge}
            current_input = (
                [nudge_msg] if session is not None else _prune_input_list(result.to_input_list()) + [nudge_msg]
            )
        iteration += 1
