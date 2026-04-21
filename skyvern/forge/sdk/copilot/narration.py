"""User-facing progress narration for the workflow copilot.

The main agent loop can run for 1-5 minutes between submit and final reply.
This module watches the agent's tool round-trips, detects meaningful state
transitions, and emits short human-readable sentences over the existing SSE
channel so the user can see "what the copilot is doing" in real time.

Narration is ephemeral -- not persisted to chat history. The frontend clears
it when the final response lands. The narrator LLM runs as a background task
so it never blocks the primary event pump. At most one narration is in flight
at a time; if a second transition fires while the first is still in flight,
it is dropped (cadence is already transition-driven, not spammy).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotNarrationUpdate,
    WorkflowCopilotStreamMessageType,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.routes.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

# Lower bound on time between narration emissions. The ticket asks for roughly
# one narration every 10-20 seconds; the state-transition trigger sets the
# upper bound loosely (a quiet agent produces none), and this floor prevents
# a burst of transitions (tool cluster + workflow_updated arriving together)
# from producing back-to-back emissions.
MIN_NARRATION_GAP_SECONDS = 10.0

# Cap on how many tool round-trips we hand to the narrator LLM. The narrator
# only needs recent context; keeping this small caps prompt cost.
MAX_TOOL_ACTIVITY_BUFFER = 8

# Tight deadline on the narrator LLM call. On timeout we drop the emission
# rather than delaying narration further.
NARRATOR_TIMEOUT_SECONDS = 8.0


class TransitionKind(StrEnum):
    # Ordered by ascending priority: higher-priority transitions overwrite a
    # lower-priority pending one within the min-gap window.
    TOOL_STARTED = "tool_started"
    NEW_TOOL_CLUSTER = "new_tool_cluster"
    ENFORCEMENT_RETRY = "enforcement_retry"
    NAVIGATION_COMPLETED = "navigation_completed"
    TEST_COMPLETED = "test_completed"
    WORKFLOW_UPDATED = "workflow_updated"


_TRANSITION_PRIORITY: dict[TransitionKind, int] = {kind: rank for rank, kind in enumerate(TransitionKind)}


@dataclass
class _ToolActivityEntry:
    tool_name: str
    summary: str
    success: bool
    iteration: int
    # Compact excerpt of the tool's parsed payload (counts, domains, statuses
    # -- see extract_tool_details). Gives the narrator concrete nouns.
    details: str = ""


@dataclass
class NarratorState:
    """Cadence + buffer state carried across stream_to_sse iterations."""

    last_emitted_at: float | None = None
    pending_activity: deque[_ToolActivityEntry] = field(default_factory=lambda: deque(maxlen=MAX_TOOL_ACTIVITY_BUFFER))
    in_flight_task: asyncio.Task[None] | None = None
    pending_transition: TransitionKind | None = None
    user_goal: str = ""
    # Tool whose tool_called arrived but tool_output hasn't yet. Cleared on
    # tool_output so post-tool transitions describe the finished action, not
    # the in-flight one.
    pending_tool_name: str | None = None

    def record_tool(
        self,
        tool_name: str,
        summary: str,
        success: bool,
        iteration: int,
        details: str = "",
    ) -> None:
        self.pending_activity.append(
            _ToolActivityEntry(
                tool_name=tool_name,
                summary=summary,
                success=success,
                iteration=iteration,
                details=details,
            )
        )

    def record_transition(self, kind: TransitionKind) -> None:
        if (
            self.pending_transition is None
            or _TRANSITION_PRIORITY[kind] > _TRANSITION_PRIORITY[self.pending_transition]
        ):
            self.pending_transition = kind


@dataclass(frozen=True)
class _CtxSnapshot:
    """Subset of copilot-context flags the narrator watches for transitions."""

    update_workflow_called: bool
    test_after_update_done: bool
    navigate_called: bool
    observation_after_navigate: bool


def snapshot_ctx(ctx: Any) -> _CtxSnapshot:
    return _CtxSnapshot(
        update_workflow_called=bool(getattr(ctx, "update_workflow_called", False)),
        test_after_update_done=bool(getattr(ctx, "test_after_update_done", False)),
        navigate_called=bool(getattr(ctx, "navigate_called", False)),
        observation_after_navigate=bool(getattr(ctx, "observation_after_navigate", False)),
    )


def detect_transitions(
    before: _CtxSnapshot,
    after: _CtxSnapshot,
    tool_name: str,
    prior_tool_name: str | None,
) -> list[TransitionKind]:
    transitions: list[TransitionKind] = []
    if not before.update_workflow_called and after.update_workflow_called:
        transitions.append(TransitionKind.WORKFLOW_UPDATED)
    if not before.test_after_update_done and after.test_after_update_done:
        transitions.append(TransitionKind.TEST_COMPLETED)
    if not before.navigate_called and after.navigate_called:
        transitions.append(TransitionKind.NAVIGATION_COMPLETED)
    if prior_tool_name is not None and tool_name != prior_tool_name:
        transitions.append(TransitionKind.NEW_TOOL_CLUSTER)
    return transitions


@dataclass(frozen=True)
class _NarratorPromptContext:
    """Frozen snapshot of prompt inputs passed to the background task."""

    transition: TransitionKind
    activity: list[_ToolActivityEntry]
    user_goal: str = ""
    pending_tool_name: str | None = None


def should_emit(state: NarratorState, now: float) -> bool:
    if state.pending_transition is None:
        return False
    if state.in_flight_task is not None and not state.in_flight_task.done():
        return False
    if state.last_emitted_at is not None and (now - state.last_emitted_at) < MIN_NARRATION_GAP_SECONDS:
        return False
    return True


def schedule_narration(
    state: NarratorState,
    stream: EventSourceStream,
    iteration: int,
) -> None:
    """Kick off a background narration task if the gate allows. Fire-and-drop:
    errors, timeouts, and empty responses are swallowed inside the task."""
    now = time.monotonic()
    if not should_emit(state, now):
        return

    transition = state.pending_transition
    assert transition is not None  # guaranteed by should_emit
    state.pending_transition = None
    # last_emitted_at is advanced only after a narration is actually delivered
    # (see _narration_task_body). Advancing here would silence the next 10s of
    # valid transitions when a narration fails, times out, or is leak-dropped
    # -- a bad trade since the in_flight_task slot already prevents concurrent
    # emissions during the LLM call.

    # Copy the deque at schedule time so the background task sees a stable
    # view while streaming_adapter keeps appending.
    prompt_ctx = _NarratorPromptContext(
        transition=transition,
        activity=list(state.pending_activity),
        user_goal=state.user_goal,
        pending_tool_name=state.pending_tool_name,
    )
    task = asyncio.create_task(
        _narration_task_body(state=state, stream=stream, iteration=iteration, prompt_ctx=prompt_ctx)
    )
    state.in_flight_task = task


async def cancel_in_flight(state: NarratorState) -> None:
    """Hard-cancel any in-flight narration task.

    Called from ``stream_to_sse``'s finally. A narration LLM call takes ~2-3s;
    blocking the final-response send for that window just to let one more
    narration land is the wrong trade -- the final assistant message is about
    to appear anyway, and on a client disconnect the narration has nowhere to
    go. Cancel immediately; fire-and-drop semantics cover the loss.
    """
    task = state.in_flight_task
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _narration_task_body(
    state: NarratorState,
    stream: EventSourceStream,
    iteration: int,
    prompt_ctx: _NarratorPromptContext,
) -> None:
    transition_value = prompt_ctx.transition.value
    try:
        try:
            narration = await _call_narrator_llm(prompt_ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("copilot narrator failed, dropping emission", error=str(exc), transition=transition_value)
            return

        if not narration:
            return

        try:
            await stream.send(
                WorkflowCopilotNarrationUpdate(
                    type=WorkflowCopilotStreamMessageType.NARRATION,
                    narration=narration,
                    iteration=iteration,
                    timestamp=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            LOG.warning("copilot narrator send failed", error=str(exc), transition=transition_value)
            return
        # Only advance last_emitted_at after a real delivery. A failed /
        # empty / leak-dropped emission leaves the clock where it was so the
        # next valid transition can emit immediately instead of waiting 10s
        # behind a narration that never reached the user.
        state.last_emitted_at = time.monotonic()
    finally:
        # Release the slot only after the send completes (or errors). Clearing
        # earlier opened a window where schedule_narration could spawn a
        # second task during the await stream.send, running two narrations
        # concurrently.
        state.in_flight_task = None


async def _call_narrator_llm(prompt_ctx: _NarratorPromptContext) -> str | None:
    """Invoke a small/fast LLM to produce one user-facing sentence.

    Returns None on timeout or when no handler is configured. Never raises;
    failures propagate as None so the narration is silently dropped.
    """
    handler = _get_narrator_handler()
    if handler is None:
        return None

    prompt = _build_narrator_prompt(prompt_ctx)
    try:
        # force_dict=False keeps the handler from running its json_repair /
        # JSON-dict coercion on a response that's intentionally plain prose.
        # With the default force_dict=True the handler raises InvalidLLMResponseType
        # on a one-sentence narration and we lose every emission.
        response = await asyncio.wait_for(
            handler(prompt=prompt, prompt_name="workflow-copilot-narration", force_dict=False),
            timeout=NARRATOR_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOG.warning(
            "copilot narrator timed out",
            timeout=NARRATOR_TIMEOUT_SECONDS,
            transition=prompt_ctx.transition.value,
        )
        return None

    narration = _extract_narration_text(response)
    if not narration:
        return None
    sanitized = _sanitize_narration(narration)
    if _narration_leaks_identifier(sanitized):
        # Drop the emission rather than ship an identifier to the user. The
        # next transition will get another chance; cadence is transition-driven
        # so one dropped sentence just means a slightly longer silence, not a
        # bad user experience of copilot jargon bleeding through.
        LOG.warning(
            "copilot narrator dropped due to identifier leak",
            transition=prompt_ctx.transition.value,
            preview=sanitized[:120],
        )
        return None
    return sanitized


def _get_narrator_handler() -> Any:
    # Reuse SECONDARY_LLM_API_HANDLER so deployments already wired for the
    # feasibility gate get narration for free. Returns None when the app
    # holder isn't initialized (unit tests, pre-startup).
    try:
        handler = app.SECONDARY_LLM_API_HANDLER
    except (RuntimeError, AttributeError):
        return None
    return handler


def handler_available() -> bool:
    """Cheap check callers can use to skip all narrator-side bookkeeping
    (transition detection, tool-details extraction, state updates) when no
    narrator LLM is configured. Resolved once per stream, not per event."""
    return _get_narrator_handler() is not None


def _build_narrator_prompt(prompt_ctx: _NarratorPromptContext) -> str:
    # Tool names are remapped to user-facing labels before reaching the LLM so
    # the model cannot echo raw internal identifiers back at the user. The
    # ``details`` field carries concrete nouns (block labels, domains, counts)
    # extracted from the tool's parsed payload so the narrator can be specific
    # instead of defaulting to filler like "Analyzing results".
    activity_lines: list[str] = []
    for entry in prompt_ctx.activity:
        label = _USER_FACING_TOOL_LABELS.get(entry.tool_name, "running a tool")
        status = "ok" if entry.success else "failed"
        detail = entry.details.strip()
        if len(detail) > 200:
            detail = detail[:200].rstrip() + "..."
        line = f"- {label} ({status})"
        if detail:
            line += f": {detail}"
        activity_lines.append(line)

    transition_label = _TRANSITION_LABELS[prompt_ctx.transition]
    activity_block = "\n".join(activity_lines) if activity_lines else "(no tool activity yet)"

    goal_snippet = (prompt_ctx.user_goal or "").strip().replace("\n", " ")
    if len(goal_snippet) > 240:
        goal_snippet = goal_snippet[:240].rstrip() + "..."
    goal_block = goal_snippet or "(no goal provided)"

    if prompt_ctx.pending_tool_name:
        current_action_label = _USER_FACING_TOOL_LABELS.get(prompt_ctx.pending_tool_name, "working on the task")
    else:
        current_action_label = "no action in flight"

    # Return JSON rather than raw prose: the shared LLM handler runs
    # json_repair on the response body and coerces unparseable prose to an
    # empty string, which silently drops every narration. Asking the model to
    # emit {"narration": "..."} keeps json_repair happy and preserves the text.
    return (
        "You are a narrator for a workflow-building copilot. Write ONE short "
        "sentence (max 14 words) describing what the copilot is doing right "
        "now, grounded in the user's goal.\n\n"
        "Rules (hard):\n"
        "- Ground the sentence in the concrete subject from the user's goal "
        "(their named target, topic, or product). Prefer the user's own words "
        'over vague placeholders like "the site" or "the page".\n'
        "- NEVER mention tool names, block names, or any identifier-looking token. "
        "Forbidden: anything containing an underscore (e.g. extract_top_post, "
        "update_and_run_blocks), camelCase tokens, anything in backticks, anything "
        'starting with "via the", JSON/YAML/code, full URLs, or raw IDs.\n'
        "- Do not echo untrusted page content verbatim.\n"
        '- Use present continuous in user-facing language ("Setting up the '
        'workflow", "Extracting the requested fields").\n'
        "- If the most recent action failed, say what it is retrying or correcting.\n"
        '- Return ONLY a JSON object: {"narration": "<sentence>"}. No prose, no markdown.\n\n'
        "Good examples:\n"
        '  {"narration": "Setting up the workflow."}\n'
        '  {"narration": "Running the workflow to gather the requested data."}\n'
        '  {"narration": "Checking the extracted results."}\n'
        "Bad examples (do NOT do this):\n"
        '  {"narration": "Extracting the values via the parse_results block."}\n'
        '  {"narration": "Running update_and_run_blocks on the workflow."}\n\n'
        f"User goal: {goal_block}\n\n"
        f"Currently doing: {current_action_label}\n\n"
        f"Latest signal: {transition_label}\n\n"
        f"Recent activity (most recent last):\n{activity_block}\n\n"
        "JSON:"
    )


# Agent tool names get remapped before reaching the LLM so internal identifiers
# can't surface via prompt echo. Unknown tools fall back to a generic phrase.
_USER_FACING_TOOL_LABELS: dict[str, str] = {
    "update_workflow": "revising the workflow draft",
    "update_and_run_blocks": "revising and testing the workflow",
    "run_blocks_and_collect_debug": "running a test of the workflow",
    "navigate_browser": "opening a page in the browser",
    "get_browser_screenshot": "taking a screenshot",
    "click": "clicking an element on the page",
    "type_text": "filling a field on the page",
    "select_option": "choosing an option from a dropdown",
    "press_key": "pressing a key",
    "scroll": "scrolling the page",
    "evaluate": "inspecting the page",
    "console_messages": "checking the browser console",
    "list_credentials": "checking saved credentials",
    "get_block_schema": "looking up workflow block options",
    "validate_block": "checking workflow block configuration",
    "get_run_results": "checking results of a prior run",
}


_TRANSITION_LABELS: dict[TransitionKind, str] = {
    TransitionKind.TOOL_STARTED: "just started a new action",
    TransitionKind.NEW_TOOL_CLUSTER: "starting a different kind of work",
    TransitionKind.ENFORCEMENT_RETRY: "course-correcting after a check",
    TransitionKind.NAVIGATION_COMPLETED: "just finished loading a page",
    TransitionKind.TEST_COMPLETED: "just finished a test of the workflow",
    TransitionKind.WORKFLOW_UPDATED: "just updated the workflow draft",
}


_MAX_DETAILS_CHARS = 240


def extract_tool_details(tool_name: str, parsed: dict[str, Any]) -> str:
    """Compact narrator-friendly excerpt from a tool's parsed payload.

    Intentionally narrow: counts, domains, and high-level statuses only.
    Raw labels (block names, field names, URL paths, page content) are excluded
    so they can't reach the narrator prompt and be echoed at the user.
    """
    if not isinstance(parsed, dict):
        return ""
    if not parsed.get("ok", True):
        return "last action failed"

    data = parsed.get("data")
    if not isinstance(data, dict):
        return ""

    if tool_name == "update_workflow" or tool_name == "update_and_run_blocks":
        return _format_step_status(data.get("block_count"), data)

    if tool_name == "run_blocks_and_collect_debug":
        executed = data.get("executed_block_labels") or [
            b.get("label") for b in data.get("blocks", []) if isinstance(b, dict)
        ]
        executed_count = sum(1 for label in executed if label)
        return _format_step_status(executed_count, data)

    if tool_name == "navigate_browser":
        return _format_url_detail(parsed.get("url") or data.get("url"), "domain")

    if tool_name == "get_browser_screenshot":
        return _format_url_detail(data.get("url"), "on")

    if tool_name == "get_run_results":
        return f"{len(data)} extracted field(s)" if data else ""

    if tool_name == "validate_block":
        valid = data.get("valid")
        if valid is True:
            return "configuration valid"
        if valid is False:
            return "configuration invalid"
        return ""

    if tool_name == "list_credentials":
        return _format_int_count(data, "credential")

    if tool_name == "get_block_schema":
        return _format_int_count(data, "step type")

    return ""


def _format_step_status(count: Any, data: dict[str, Any]) -> str:
    parts: list[str] = []
    if isinstance(count, int) and count:
        parts.append(f"{count} step(s)")
    status = data.get("overall_status") or data.get("status")
    if isinstance(status, str) and status:
        parts.append(f"status: {status}")
    return _bound(" - ".join(parts))


def _format_url_detail(url: Any, prefix: str) -> str:
    if isinstance(url, str):
        return f"{prefix}: {_domain_only(url)}"
    return ""


def _format_int_count(data: dict[str, Any], noun: str) -> str:
    count = data.get("count")
    if isinstance(count, int):
        return f"{count} {noun}(s)"
    return ""


def _domain_only(url: str) -> str:
    # Narrator sees only the host. Prevents query-string / path content from
    # leaking into output.
    try:
        host = urlparse(url).hostname
    except ValueError:
        host = None
    if host:
        return host[:80]
    # Fallback for schemeless or malformed inputs that urlparse returns ""/None for.
    return url.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0][:80]


def _bound(text: str) -> str:
    return text[:_MAX_DETAILS_CHARS]


def _extract_narration_text(response: Any) -> str | None:
    """Pull a plain string from whatever the LLM handler returned.

    Handlers may return a str, a dict with ``user_response``/``content``, or
    some other shape depending on experimentation wiring. Fall back to str()
    only when nothing structured is recognizable.
    """
    if isinstance(response, str):
        return response.strip() or None
    if isinstance(response, dict):
        for key in ("narration", "sentence", "user_response", "content", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    return None


# Narration sanitization: trim, strip trailing quotes/fences the model might
# have included, collapse whitespace, and enforce a hard length ceiling.
# Belt-and-braces layer in addition to the prompt rules.
_MAX_NARRATION_CHARS = 180
_NARRATION_DELIMITERS = ("```", '"', "'")


def _sanitize_narration(text: str) -> str:
    cleaned = text.strip()
    for delim in _NARRATION_DELIMITERS:
        if cleaned.startswith(delim):
            cleaned = cleaned[len(delim) :].lstrip()
        if cleaned.endswith(delim):
            cleaned = cleaned[: -len(delim)].rstrip()
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _MAX_NARRATION_CHARS:
        cleaned = cleaned[:_MAX_NARRATION_CHARS].rstrip() + "..."
    return cleaned


# Any token that looks like an internal identifier: snake_case, camelCase with
# at least one lowercase-then-uppercase boundary, kebab-case with 3+ segments
# (to spare ordinary English compounds like "follow-up"), or anything wrapped
# in backticks. Belt-and-braces guard on top of the prompt rules: if the model
# still sneaks a block/tool name through, the narration is dropped rather
# than shipped. False positives are cheap (one silent cadence slot) while a
# missed leak ships jargon to the user.
_IDENTIFIER_LEAK_PATTERNS = (
    re.compile(r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+"),
    re.compile(r"\b[a-z][a-z0-9]*[A-Z][A-Za-z0-9]+\b"),
    re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+){2,}\b"),
    re.compile(r"`[^`]+`"),
    re.compile(r"\bvia the\b", re.IGNORECASE),
)


def _narration_leaks_identifier(narration: str) -> bool:
    return any(pattern.search(narration) for pattern in _IDENTIFIER_LEAK_PATTERNS)
