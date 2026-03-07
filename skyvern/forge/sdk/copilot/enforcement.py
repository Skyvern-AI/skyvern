"""Enforcement wrapper — nudge agent when it skips required steps."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog
from agents.run import Runner

from skyvern.forge.sdk.copilot.screenshot_utils import ScreenshotEntry
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

if TYPE_CHECKING:
    from agents.agent import Agent
    from agents.result import RunResultStreaming

    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

MAX_POST_UPDATE_NUDGES = 2
MAX_INTERMEDIATE_NUDGES = 8
MAX_FAILED_TEST_NUDGES = 2
MIN_BLOCKS_FOR_AUTO_COMPLETE = 10
TOTAL_TIMEOUT_SECONDS = 600

SCREENSHOT_SENTINEL = "[copilot:screenshot] "
NUDGE_SENTINEL = "[copilot:nudge] "
SCREENSHOT_PLACEHOLDER = SCREENSHOT_SENTINEL + "[prior screenshot removed to save context]"
TOKEN_BUDGET = 90_000
TOKENS_PER_RESIZED_IMAGE = 765
CHARS_PER_TOKEN = 4

POST_UPDATE_NUDGE = (
    "You updated the workflow but did not test it. "
    "You MUST call run_blocks_and_collect_debug to test at least the first block "
    "before responding to the user. This verifies the workflow actually works."
)

POST_NAVIGATE_NUDGE = (
    "You navigated to a page but did not observe its content. "
    "You MUST use evaluate, get_browser_screenshot, click, or type_text "
    "to inspect the page before responding. Do NOT answer from memory."
)

POST_INTERMEDIATE_SUCCESS_NUDGE = (
    "STOP — do NOT respond to the user yet. "
    "Your workflow only covers a subset of what the user asked for. "
    "You MUST add the next block now: call update_workflow with the next step, "
    "then run run_blocks_and_collect_debug on ALL current labels. "
    "Only respond to the user when every distinct action they requested is covered "
    "by a workflow block, or you have clear evidence that continuing is infeasible."
)

POST_FAILED_TEST_NUDGE = (
    "STOP — your last test run FAILED. Do NOT respond to the user yet. "
    "Analyze the failure reason and decide:\n"
    "1. If the failure looks fixable (wrong goal wording, popup blocking, timeout, "
    "element not found), adjust the workflow with a DIFFERENT approach and call "
    "update_workflow + run_blocks_and_collect_debug again.\n"
    "2. If you have now failed multiple times with genuinely different approaches "
    "and the evidence strongly suggests the site cannot satisfy the request, "
    "respond explaining exactly what you tried and what blocked you.\n"
    "Do NOT resubmit the same workflow — you must change something substantive."
)

MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES = 2

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


class CopilotClientDisconnectedError(Exception):
    """Raised when the client disconnects during agent execution."""


class CopilotTotalTimeoutError(Exception):
    """Raised when the copilot agent exceeds the total allowed runtime."""


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

    matched_categories = 0
    for category in _ACTION_CATEGORIES:
        if any(keyword in text for keyword in category):
            matched_categories += 1

    has_sequential = any(conn in text for conn in _SEQUENTIAL_CONNECTORS)

    estimated_min_blocks = matched_categories
    if has_sequential:
        estimated_min_blocks = max(estimated_min_blocks, 2)

    return block_count < estimated_min_blocks


def _needs_intermediate_success_nudge(ctx: Any) -> bool:
    if getattr(ctx, "premature_completion_nudge_done", False):
        return False

    nudge_count = getattr(ctx, "intermediate_nudge_count", 0)
    if nudge_count >= MAX_INTERMEDIATE_NUDGES:
        return False

    if not getattr(ctx, "update_workflow_called", False):
        return False
    if not getattr(ctx, "test_after_update_done", False):
        return False
    if getattr(ctx, "last_test_ok", None) is not True:
        return False

    block_count = getattr(ctx, "last_update_block_count", None)
    if not isinstance(block_count, int):
        return False

    return _goal_likely_needs_more_blocks(getattr(ctx, "user_message", ""), block_count)


def _consume_pending_screenshots(ctx: Any) -> dict[str, Any] | None:
    """Drain pending_screenshots and build a synthetic user message with images.

    Tool results stay text-only (OpenAI rejects images in tool messages).
    Instead, screenshots are delivered as a follow-up user message so the
    model can inspect the browser state on the next turn.
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
    if nudge_count >= MAX_EXPLORE_WITHOUT_WORKFLOW_NUDGES:
        return False
    return True


def _needs_failed_test_nudge(ctx: Any) -> bool:
    """Return True when the last test failed and the agent hasn't iterated yet."""
    if getattr(ctx, "last_test_ok", None) is not False:
        return False
    if not getattr(ctx, "test_after_update_done", False):
        return False
    nudge_count = getattr(ctx, "failed_test_nudge_count", 0)
    if nudge_count >= MAX_FAILED_TEST_NUDGES:
        return False
    return True


def _check_enforcement(ctx: Any) -> str | None:
    if ctx.navigate_called and not ctx.observation_after_navigate and not ctx.navigate_enforcement_done:
        ctx.navigate_enforcement_done = True
        return POST_NAVIGATE_NUDGE

    if _needs_explore_without_workflow_nudge(ctx):
        ctx.explore_without_workflow_nudge_count += 1
        return POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE

    if ctx.update_workflow_called and not ctx.test_after_update_done:
        return POST_UPDATE_NUDGE

    if _needs_intermediate_success_nudge(ctx):
        ctx.premature_completion_nudge_done = True
        ctx.intermediate_nudge_count = getattr(ctx, "intermediate_nudge_count", 0) + 1
        return POST_INTERMEDIATE_SUCCESS_NUDGE

    if _needs_failed_test_nudge(ctx):
        ctx.failed_test_nudge_count = getattr(ctx, "failed_test_nudge_count", 0) + 1
        return POST_FAILED_TEST_NUDGE

    return None


def is_screenshot_message(item: Any) -> bool:
    """Return True if the item is a synthetic screenshot user message."""
    role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
    if role != "user":
        return False
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    if isinstance(content, str):
        return content.startswith(SCREENSHOT_SENTINEL)
    if not isinstance(content, list):
        return False
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if isinstance(text, str) and text.startswith(SCREENSHOT_SENTINEL):
            return True
    return False


def _is_nudge_message(item: Any) -> bool:
    """Return True if the item is a synthetic enforcement nudge."""
    role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
    if role != "user":
        return False
    content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    return isinstance(content, str) and content.startswith(NUDGE_SENTINEL)


def is_synthetic_user_message(item: Any) -> bool:
    """Return True if item is a screenshot or nudge (not a real user turn)."""
    return is_screenshot_message(item) or _is_nudge_message(item)


def _prune_input_list(items: list[Any]) -> list[Any]:
    """Remove old screenshot messages and truncate large tool outputs.

    Keeps only the most recent synthetic screenshot message.
    Replaces older ones with a short text placeholder.
    Truncates long string-typed function_call_output.output fields.
    """
    screenshot_indices = [i for i, item in enumerate(items) if is_screenshot_message(item)]
    drop_indices = set(screenshot_indices[:-1]) if screenshot_indices else set()

    result: list[Any] = []
    for i, item in enumerate(items):
        if i in drop_indices:
            result.append({"role": "user", "content": SCREENSHOT_PLACEHOLDER})
            continue

        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if item_type == "function_call_output":
            output = item.get("output") if isinstance(item, dict) else getattr(item, "output", None)
            if isinstance(output, str) and len(output) > 2000:
                truncated = output[:2000] + "\n... [truncated]"
                if isinstance(item, dict):
                    item = {**item, "output": truncated}
                else:
                    try:
                        item.output = truncated
                    except (AttributeError, TypeError):
                        pass

        result.append(item)
    return result


def estimate_tokens(items: list[Any]) -> int:
    """Rough token estimate for an input list. Errs on the high side."""
    total = 0
    for item in items:
        if isinstance(item, dict):
            total += _estimate_dict_tokens(item)
        else:
            total += len(str(item)) // CHARS_PER_TOKEN
    return total


def _estimate_dict_tokens(d: dict[str, Any]) -> int:
    """Recursively estimate tokens for a dict item."""
    total = 0
    for v in d.values():
        if isinstance(v, str):
            total += len(v) // CHARS_PER_TOKEN
        elif isinstance(v, dict):
            if v.get("type") == "input_image":
                total += TOKENS_PER_RESIZED_IMAGE
            else:
                total += _estimate_dict_tokens(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    if item.get("type") == "input_image":
                        total += TOKENS_PER_RESIZED_IMAGE
                    else:
                        total += _estimate_dict_tokens(item)
                elif isinstance(item, str):
                    total += len(item) // CHARS_PER_TOKEN
    return total


def aggressive_prune(items: list[Any]) -> list[Any]:
    """Emergency prune: drop ALL screenshots, keep original message + last 3 tool pairs + latest nudge."""
    if not items:
        return items

    original = items[0]
    tail: list[Any] = []
    for item in reversed(items[1:]):
        if is_screenshot_message(item):
            continue
        tail.append(item)
        if len(tail) >= 7:
            break
    tail.reverse()
    return [original] + tail


def _is_context_window_error(exc: BaseException) -> bool:
    """Return True if the exception indicates a context window overflow."""
    msg = str(exc).lower()
    return "context_length_exceeded" in msg or "context window" in msg or "max_tokens" in msg


async def run_with_enforcement(
    agent: Agent,
    initial_input: str | list,
    ctx: Any,
    stream: EventSourceStream,
    **runner_kwargs: Any,
) -> RunResultStreaming:
    """Run agent with enforcement nudges, preserving conversation history."""
    from skyvern.forge.sdk.copilot.streaming_adapter import stream_to_sse

    session = runner_kwargs.pop("session", None)
    current_input: str | list = initial_input
    start_time = time.monotonic()
    iteration = 0

    while True:
        if await stream.is_disconnected():
            raise CopilotClientDisconnectedError()

        elapsed = time.monotonic() - start_time
        if elapsed > TOTAL_TIMEOUT_SECONDS:
            raise CopilotTotalTimeoutError()

        # Pre-call budget check (only meaningful when session is None;
        # with a session the real budget check lives in call_model_input_filter)
        if session is None and isinstance(current_input, list):
            est = estimate_tokens(current_input)
            LOG.info("Token estimate before model call", tokens=est, iteration=iteration)
            if est > TOKEN_BUDGET:
                LOG.warning(
                    "Token estimate exceeds budget, aggressively pruning",
                    tokens=est,
                    budget=TOKEN_BUDGET,
                )
                current_input = aggressive_prune(current_input)

        with copilot_span(
            "enforcement_iteration",
            data={"iteration": iteration, "elapsed_seconds": round(elapsed, 3)},
        ):
            try:
                result = Runner.run_streamed(
                    agent,
                    input=current_input,
                    context=ctx,
                    session=session,
                    **runner_kwargs,
                )
                await stream_to_sse(result, stream, ctx)
            except Exception as e:
                if not _is_context_window_error(e):
                    raise
                LOG.error(
                    "Context window exceeded, retrying with aggressive prune",
                    error=str(e),
                    iteration=iteration,
                    has_session=session is not None,
                )
                if session is not None:
                    all_items = await session.get_items()
                    pruned = aggressive_prune(all_items)
                    await session.clear_session()
                    await session.add_items(pruned)
                elif isinstance(current_input, list):
                    current_input = aggressive_prune(current_input)
                else:
                    raise
                result = Runner.run_streamed(
                    agent,
                    input=current_input,
                    context=ctx,
                    session=session,
                    **runner_kwargs,
                )
                await stream_to_sse(result, stream, ctx)

        if await stream.is_disconnected():
            raise CopilotClientDisconnectedError()

        # Inject pending screenshots as a synthetic user message so the
        # model can see browser state on the next turn.  Tool results
        # stay text-only because OpenAI rejects images in tool messages.
        screenshot_msg = _consume_pending_screenshots(ctx)
        if screenshot_msg is not None:
            LOG.info("Injecting screenshot user message", count=len(screenshot_msg["content"]) - 1)
            if session is not None:
                current_input = [screenshot_msg]
            else:
                current_input = _prune_input_list(result.to_input_list()) + [screenshot_msg]
            iteration += 1
            continue

        nudge = _check_enforcement(ctx)
        if nudge is None:
            return result

        if nudge == POST_UPDATE_NUDGE:
            if ctx.post_update_nudge_count >= MAX_POST_UPDATE_NUDGES:
                LOG.warning(
                    "Enforcement exhausted post-update nudges, allowing response",
                    nudge_count=ctx.post_update_nudge_count,
                )
                ctx.update_workflow_called = False
                ctx.post_update_nudge_count = 0
                return result

            ctx.post_update_nudge_count += 1
            nudge_type = "post_update"
        elif nudge == POST_NAVIGATE_NUDGE:
            nudge_type = "post_navigate"
        elif nudge == POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE:
            nudge_type = "explore_without_workflow"
        elif nudge == POST_FAILED_TEST_NUDGE:
            nudge_type = "post_failed_test"
        else:
            nudge_type = "intermediate_success"

        LOG.info(
            "Enforcement nudge",
            nudge_type=nudge_type,
            iteration=iteration,
        )

        with copilot_span(
            "enforcement_nudge",
            data={"nudge_type": nudge_type, "iteration": iteration},
        ):
            nudge_msg = {"role": "user", "content": NUDGE_SENTINEL + nudge}
            if session is not None:
                current_input = [nudge_msg]
            else:
                current_input = _prune_input_list(result.to_input_list()) + [nudge_msg]
        iteration += 1
