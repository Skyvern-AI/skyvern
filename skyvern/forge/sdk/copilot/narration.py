"""User-facing progress narration for the workflow copilot.

The main agent loop can run for 1-5 minutes between submit and final reply.
This module watches the agent's tool round-trips, detects meaningful state
transitions, and emits short human-readable sentences over the existing SSE
channel so the user can see "what the copilot is doing" in real time.

Narration is persisted into the turn's design activity and paired to the step
it explains, so a reload shows what was live at the time. The narrator LLM runs
as a background task
so it never blocks the primary event pump. At most one narration is in flight
at a time; if a second transition fires while the first is still in flight,
it is dropped (cadence is already transition-driven, not spammy).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlparse

import structlog

from skyvern.forge.sdk.copilot.code_write_diff import CodeWriteDiff
from skyvern.forge.sdk.copilot.context import BlockRunIdentity
from skyvern.forge.sdk.copilot.llm_config import get_fast_copilot_handler, resolve_fast_copilot_handler
from skyvern.forge.sdk.copilot.output_utils import sanitize_block_label_for_display
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotBlockProgressUpdate,
    WorkflowCopilotNarrationUpdate,
    WorkflowCopilotStreamMessageType,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import NarrativeActivityEntry
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream

LOG = structlog.get_logger()

# Lower bound on time between narration emissions. The ticket asks for roughly
# one narration every 10-20 seconds; the state-transition trigger sets the
# upper bound loosely (a quiet agent produces none), and this floor prevents
# a burst of transitions (tool cluster + workflow_updated arriving together)
# from producing back-to-back emissions.
MIN_NARRATION_GAP_SECONDS = 10.0

# Floor on how often narrator_poll_tick re-fetches block statuses from the DB.
# That fetch is a free read, not an LLM call, so it must not share the
# narration floor above -- it only needs a small burst guard. Well below the
# caller's RUN_BLOCKS_POLL_INTERVAL_SECONDS (5.0 in run_execution.py), so it
# never actually binds at today's cadence -- it's a ceiling, not a throttle.
MIN_BLOCK_STATUS_POLL_GAP_SECONDS = 1.0

# Cap on how many tool round-trips we hand to the narrator LLM. The narrator
# only needs recent context; keeping this small caps prompt cost.
MAX_TOOL_ACTIVITY_BUFFER = 8

# Tight deadline on the narrator LLM call. On timeout we drop the emission
# rather than delaying narration further.
NARRATOR_TIMEOUT_SECONDS = 8.0

# Caps on the persisted per-block and design activity logs. Mirror the FE
# MAX_ACTIVITY_ENTRIES / MAX_DESIGN_ACTIVITY_ENTRIES in narrativeState.ts so
# the rehydrated bubble matches what the live stream rendered.
MAX_BLOCK_ACTIVITY_ENTRIES = 30
MAX_DESIGN_ACTIVITY_ENTRIES = 50

# Tools whose calls/results are never surfaced in the user-facing activity log.
# Mirror of the FE ACTIVITY_TOOL_DENYLIST in narrativeState.ts.
ACTIVITY_TOOL_DENYLIST = frozenset({"get_run_results", "get_browser_screenshot"})

# Tools that kick off a block run. Mirror of the FE RUN_TOOLS in narrativeState.ts.
# Their tool_call is recorded before the run flips running_block_label to the
# running block, so the matching tool_result is pinned to the call's bucket (see
# NarratorState._activity_bucket_label) rather than routed live.
_RUN_ACTIVITY_TOOLS = frozenset({"update_and_run_blocks", "edit_block_and_run", "run_blocks_and_collect_debug"})

# Shared classification for a code-authoring reject the streaming adapter renders
# as quiet de-duplicated progress. Tagged on the reject (workflow_update) and
# consumed by the SSE layer (streaming_adapter) — one source of truth for both.
CODE_REPAIR_PROGRESS_SURFACE_KIND = "code_repair_progress"
CODE_REPAIR_PROGRESS_TEXT = "Refining the workflow's code"

_TOOL_ACTIVITY_DISPLAY_LABELS = {
    # Mirror of the FE ACTIVITY_TOOL_DISPLAY_LABELS in narrativeState.ts.
    "update_workflow": "Updating workflow",
    "update_and_run_blocks": "Testing workflow",
    "edit_block_and_run": "Editing and testing block",
    "run_blocks_and_collect_debug": "Testing workflow",
    "evaluate": "Inspecting page",
    "click": "Interacting with page",
    "type_text": "Entering text",
    "scroll": "Interacting with page",
    "select_option": "Selecting option",
    "press_key": "Interacting with page",
    "navigate_browser": "Opening page",
    "get_block_schema": "Checking workflow block options",
    "get_workflow_knowledge": "Looking up workflow guidance",
    "list_integrations": "Checking connected integrations",
    "inspect_current_workflow": "Inspecting workflow",
    "discover_workflow_entrypoint": "Finding the entry page",
    "inspect_page_for_composition": "Inspecting the page",
    "inspect_locator_matches": "Comparing locator candidates",
    "list_credentials": "Checking saved credentials",
    "validate_block": "Checking the block",
    "console_messages": "Reading the browser console",
    "wait_for_either_state": "Waiting for the page",
    "skyvern_frame_list": "Finding embedded pages",
    "skyvern_frame_switch": "Opening embedded page",
    "skyvern_frame_main": "Returning to main page",
    "fill_credential_field": "Entering saved credentials",
    "edit_block": "Editing block",
    "add_block": "Adding block",
    "delete_block": "Deleting block",
    "request_credential": "Requesting a credential",
    "ask_user": "Asking you",
}

# Tools whose label names the block they operate on, read from the tool's own
# `label` argument.
_BLOCK_TARGET_LABEL_TOOLS = frozenset({"edit_block", "edit_block_and_run", "delete_block"})
_BLOCK_TARGET_VERSION_SUFFIX_RE = re.compile(r"_v\d+$", re.IGNORECASE)


def _humanize_block_target(target: str) -> str:
    # Mirror of the FE humanizeBlockLabel in blockLabel.ts, so the row matches
    # the block card rendered beside it.
    words = [w for w in re.split(r"[_\s]+", _BLOCK_TARGET_VERSION_SUFFIX_RE.sub("", target)) if w]
    if not words:
        return target
    return " ".join(word[0].upper() + word[1:] for word in words)


def tool_activity_display_label(tool_name: str, tool_input: dict[str, Any] | None = None) -> str:
    """Return a product-safe label for user-visible activity rows."""
    label = _TOOL_ACTIVITY_DISPLAY_LABELS.get(tool_name, "Working")
    if tool_name in _BLOCK_TARGET_LABEL_TOOLS and tool_input is not None:
        target = tool_input.get("label")
        if isinstance(target, str) and target.strip():
            # The target is LLM-authored, so it goes through the same quote/length
            # clamp the result-row summaries use before it is interpolated.
            humanized = sanitize_block_label_for_display(_humanize_block_target(target))
            if humanized:
                return f'{label} "{humanized}"'
    return label


def build_tool_call_activity(
    tool_name: str, iteration: int, tool_call_id: str, *, timestamp: datetime, display_label: str | None = None
) -> NarrativeActivityEntry | None:
    if tool_name in ACTIVITY_TOOL_DENYLIST:
        return None
    display_label = display_label or tool_activity_display_label(tool_name)
    return {
        "kind": "tool_call",
        "text": f"{display_label}…",
        "iteration": iteration,
        "toolName": tool_name,
        "displayLabel": display_label,
        "id": f"tc-{tool_call_id}",
        "timestamp": timestamp.isoformat(),
    }


def build_tool_result_activity(
    tool_name: str,
    summary: str,
    success: bool,
    iteration: int,
    tool_call_id: str,
    *,
    timestamp: datetime,
    display_label: str | None = None,
    code_diffs: list[CodeWriteDiff] | None = None,
) -> NarrativeActivityEntry | None:
    if tool_name in ACTIVITY_TOOL_DENYLIST:
        return None
    display_label = display_label or tool_activity_display_label(tool_name)
    entry: NarrativeActivityEntry = {
        "kind": "tool_result",
        "text": summary or display_label,
        "iteration": iteration,
        "toolName": tool_name,
        "displayLabel": display_label,
        "success": success,
        "id": f"tr-{tool_call_id}",
        "timestamp": timestamp.isoformat(),
    }
    if code_diffs:
        entry["codeDiffs"] = code_diffs
    return entry


def build_narration_activity(
    narration: str,
    iteration: int,
    timestamp: datetime,
    *,
    active_label: str | None = None,
    outcome_label: str | None = None,
) -> NarrativeActivityEntry:
    entry: NarrativeActivityEntry = {
        "kind": "narration",
        "text": narration,
        "iteration": iteration,
        "id": f"n-{iteration}-{timestamp.isoformat()}",
        "timestamp": timestamp.isoformat(),
    }
    if active_label:
        entry["activeLabel"] = active_label
    if outcome_label:
        entry["outcomeLabel"] = outcome_label
    return entry


class TransitionKind(StrEnum):
    # Ordered by ascending priority: higher-priority transitions overwrite a
    # lower-priority pending one within the min-gap window.
    TOOL_STARTED = "tool_started"
    TOOL_IN_PROGRESS = "tool_in_progress"
    BLOCK_STARTED = "block_started"
    BLOCK_COMPLETED = "block_completed"
    NEW_TOOL_CLUSTER = "new_tool_cluster"
    BLOCK_FAILED = "block_failed"
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
    # Advances on every narrator task launch (success or failure) so a flaky
    # narrator path can't be re-fired every poll tick.
    last_attempted_at: float | None = None
    pending_activity: deque[_ToolActivityEntry] = field(default_factory=lambda: deque(maxlen=MAX_TOOL_ACTIVITY_BUFFER))
    in_flight_task: asyncio.Task[None] | None = None
    pending_transition: TransitionKind | None = None
    # Which iteration recorded pending_transition, so suppressing iteration N cannot
    # discard a transition an earlier typed-row-free iteration is still waiting on.
    pending_transition_iteration: int | None = None
    # The pending transition was tagged to a step other than the one it
    # described, so any outcome it names belongs to earlier work.
    pending_transition_reanchored: bool = False
    # Highest-priority transition a protected TOOL_STARTED displaced. Promoted
    # to pending once the intent narration is scheduled.
    deferred_transition: TransitionKind | None = None
    deferred_transition_iteration: int | None = None
    user_goal: str = ""
    # Tool whose tool_called arrived but tool_output hasn't yet. Cleared on
    # tool_output so post-tool transitions describe the finished action, not
    # the in-flight one.
    pending_tool_name: str | None = None
    current_iteration: int = 0
    # Narrator handler resolved once per stream so per-emission calls
    # don't re-hit PostHog.
    resolved_handler: Any = None
    # Persisted activity log routed to the running block (else design). The FE
    # routes each entry to the block whose state is "running"; we cache the
    # single running label here since copilot block runs are sequential.
    block_activity: dict[str, list[NarrativeActivityEntry]] = field(default_factory=dict)
    design_activity: list[NarrativeActivityEntry] = field(default_factory=list)
    running_block_label: str | None = None
    # tool_call_id -> the bucket its tool_call landed in, tracked for
    # _RUN_ACTIVITY_TOOLS only so the later tool_result rejoins the call's bucket.
    # An unmatched call (result never arrives) is bounded by this state's one-turn lifetime.
    run_tool_call_buckets: dict[str, str | None] = field(default_factory=dict)
    # Per-turn (NarratorState lives one turn); collapses repeated code-repair progress to one entry.
    emitted_progress_texts: set[str] = field(default_factory=set)

    def record_activity(self, entry: NarrativeActivityEntry | None) -> None:
        if entry is None:
            return
        label = self._activity_bucket_label(entry)
        if label is None:
            self.design_activity.append(entry)
            if len(self.design_activity) > MAX_DESIGN_ACTIVITY_ENTRIES:
                del self.design_activity[:-MAX_DESIGN_ACTIVITY_ENTRIES]
            return
        bucket = self.block_activity.setdefault(label, [])
        bucket.append(entry)
        if len(bucket) > MAX_BLOCK_ACTIVITY_ENTRIES:
            del bucket[:-MAX_BLOCK_ACTIVITY_ENTRIES]

    def _activity_bucket_label(self, entry: NarrativeActivityEntry) -> str | None:
        """Bucket an entry to a running block (its label) or design (None).

        A run tool's call is recorded before the run it triggers flips
        running_block_label, so routing its result live would split the
        call/result pair across buckets and the FE could never fold it. Pin the
        result to the call's bucket by tool_call_id; everything else routes live.
        """
        # Narration renders inside the design step it explains, so it stays in
        # design_activity even mid-run rather than becoming a row on the block card.
        if entry.get("kind") == "narration":
            return None
        entry_id = entry.get("id") or ""
        if entry.get("kind") == "tool_call" and entry.get("toolName") in _RUN_ACTIVITY_TOOLS:
            bucket = self.running_block_label
            self.run_tool_call_buckets[entry_id.removeprefix("tc-")] = bucket
            return bucket
        if entry.get("kind") == "tool_result":
            call_id = entry_id.removeprefix("tr-")
            if call_id in self.run_tool_call_buckets:
                return self.run_tool_call_buckets.pop(call_id)
        return self.running_block_label

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
        # Every transition meaning "the work returned" outranks TOOL_STARTED, so
        # without this a step's own completion always replaces the intent
        # narration it was about to get and the narrator only speaks in
        # hindsight. The loser is banked rather than dropped: it becomes pending
        # as soon as the intent is scheduled, so the step is still narrated
        # twice. Still-working transitions are not hindsight and pass through.
        if (
            self.pending_transition is TransitionKind.TOOL_STARTED
            and self.pending_transition_iteration == self.current_iteration
            and kind in _OUTCOME_KNOWN_TRANSITIONS
        ):
            if (
                self.deferred_transition is None
                or _TRANSITION_PRIORITY[kind] > _TRANSITION_PRIORITY[self.deferred_transition]
            ):
                self.deferred_transition = kind
                self.deferred_transition_iteration = self.current_iteration
            return
        if (
            self.pending_transition is None
            or _TRANSITION_PRIORITY[kind] > _TRANSITION_PRIORITY[self.pending_transition]
        ):
            self.pending_transition = kind
            self.pending_transition_iteration = self.current_iteration
            self.pending_transition_reanchored = False
        elif self.pending_transition_iteration is None:
            # Banked across a pass reset; re-anchor to this pass's first step.
            self.pending_transition_iteration = self.current_iteration
            self.pending_transition_reanchored = True


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
    # The transition was banked across a pass reset and re-anchored, so it
    # describes work from an earlier step than the one it now points at.
    reanchored: bool = False


def should_emit(state: NarratorState, now: float) -> bool:
    if state.pending_transition is None:
        return False
    if state.in_flight_task is not None and not state.in_flight_task.done():
        return False
    last_event = max(state.last_emitted_at or 0.0, state.last_attempted_at or 0.0)
    if last_event > 0.0 and (now - last_event) < MIN_NARRATION_GAP_SECONDS:
        return False
    return True


def schedule_narration(state: NarratorState, stream: EventSourceStream) -> None:
    """Kick off a background narration task if the gate allows. Fire-and-drop:
    errors, timeouts, and empty responses are swallowed inside the task."""
    # A transition banked across an enforcement pass loses its tag when the pass
    # resets it. Anchor it to the step running now rather than dropping it, so a
    # poll and a tool event reaching this point cannot disagree about the same
    # banked transition.
    if state.pending_transition is not None and state.pending_transition_iteration is None:
        state.pending_transition_iteration = state.current_iteration
        state.pending_transition_reanchored = True
    reanchored = state.pending_transition_reanchored

    now = time.monotonic()
    if not should_emit(state, now):
        return

    transition = state.pending_transition
    iteration = state.pending_transition_iteration
    if transition is None or iteration is None:
        return
    # A transition this step's intent displaced takes the slot it just freed.
    state.pending_transition = state.deferred_transition
    state.pending_transition_iteration = state.deferred_transition_iteration
    state.pending_transition_reanchored = False
    state.deferred_transition = None
    state.deferred_transition_iteration = None
    # Bound failure-path retries to the same gap window successes use; without
    # this, a flaky narrator re-fires every poll tick.
    state.last_attempted_at = now

    # Copy the deque at schedule time so the background task sees a stable
    # view while streaming_adapter keeps appending.
    prompt_ctx = _NarratorPromptContext(
        transition=transition,
        activity=list(state.pending_activity),
        user_goal=state.user_goal,
        pending_tool_name=state.pending_tool_name,
        reanchored=reanchored,
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
    handler = state.resolved_handler or _get_narrator_handler()
    try:
        try:
            narration = await _call_narrator_llm(prompt_ctx, handler)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("copilot narrator failed, dropping emission", error=str(exc), transition=transition_value)
            return

        if narration is None or not narration.reasoning:
            return

        narration_ts = datetime.now(timezone.utc)
        try:
            await stream.send(
                WorkflowCopilotNarrationUpdate(
                    type=WorkflowCopilotStreamMessageType.NARRATION,
                    narration=narration.reasoning,
                    active_label=narration.active_label,
                    outcome_label=narration.outcome_label,
                    iteration=iteration,
                    timestamp=narration_ts,
                )
            )
        except Exception as exc:
            LOG.warning("copilot narrator send failed", error=str(exc), transition=transition_value)
            return
        LOG.info("copilot_narration_emitted", iteration=iteration, transition=transition_value)
        state.record_activity(
            build_narration_activity(
                narration.reasoning,
                iteration,
                narration_ts,
                active_label=narration.active_label,
                outcome_label=narration.outcome_label,
            )
        )
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


async def _call_narrator_llm(prompt_ctx: _NarratorPromptContext, handler: Any) -> NarrationDraft | None:
    """Invoke a small/fast LLM for one unit of work's titles and reason.

    Returns None on timeout or when no handler is configured. Never raises;
    failures propagate as None so the narration is silently dropped.
    """
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

    draft = _extract_narration_draft(response)
    if draft is None:
        return None
    sanitized = _sanitize_narration(draft.reasoning)
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
    # A leaking label is dropped on its own: the row falls back to the tool
    # label, which is strictly better than losing the reasoning too.
    # A re-anchored transition describes an earlier step, so its outcome would
    # name work the step it now points at never did.
    outcome_known = prompt_ctx.transition in _OUTCOME_KNOWN_TRANSITIONS and not prompt_ctx.reanchored
    return NarrationDraft(
        reasoning=sanitized,
        active_label=_clean_label(draft.active_label),
        outcome_label=_clean_label(draft.outcome_label) if outcome_known else None,
    )


def _get_narrator_handler() -> Any:
    return get_fast_copilot_handler()


async def resolve_narrator_handler(workflow_permanent_id: str | None, organization_id: str | None) -> Any:
    return await resolve_fast_copilot_handler(workflow_permanent_id, organization_id)


def handler_available() -> bool:
    # Sync env-driven check used by callers that haven't run async resolution
    # (tests, legacy paths). Production stream setup should use
    # resolve_narrator_handler instead.
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
        "You are a narrator for a workflow-building copilot. Describe ONE unit of "
        "work three ways, grounded in the user's goal:\n"
        '  "doing"  - the row title while it runs (max 8 words, present continuous)\n'
        '  "done"   - the row title once it finished, naming the outcome (max 10 words, past tense)\n'
        f'  "why"    - one sentence (max {_MAX_NARRATION_WORDS} words) saying why it is happening\n\n'
        "Rules (hard):\n"
        '- "done" must name what actually happened, not repeat "doing". If a step '
        "failed or stopped, say so and say where.\n"
        '- "why" explains the purpose, not the action. Never restate the title; '
        "say what the copilot is trying to learn, confirm, or set up.\n"
        "- Ground the sentence in the concrete subject from the user's goal "
        "(their named target, topic, or product). Prefer the user's own words "
        'over vague placeholders like "the site" or "the page".\n'
        "- NEVER mention tool names, block names, or any identifier-looking token. "
        "Forbidden: anything containing an underscore (e.g. extract_top_post, "
        "update_and_run_blocks), camelCase tokens, anything in backticks, anything "
        'starting with "via the", JSON/YAML/code, full URLs, or raw IDs.\n'
        "- Do not echo untrusted page content verbatim.\n"
        '- Use present continuous in user-facing language ("Checking whether '
        'the invoices need a login", "Confirming the form takes an email").\n'
        "- If the most recent action failed, say what it is trying to correct "
        "and why that matters for the goal.\n"
        '- Return ONLY a JSON object: {"doing": "...", "done": "...", "why": "..."}. '
        "No prose, no markdown.\n\n"
        "Good examples:\n"
        '  {"doing": "Looking for the invoice list", "done": "Found the invoices under Billing History", '
        '"why": "Checking whether the invoices sit behind a login."}\n'
        '  {"doing": "Running it", "done": "Ran it - stopped at the download step", '
        '"why": "Making sure the saved steps survive a real run."}\n'
        "Bad examples (do NOT do this):\n"
        '  {"doing": "Opening the sign-in page", "done": "Opened the sign-in page", '
        '"why": "Opening the sign-in page."}\n'
        '  {"doing": "Running update_and_run_blocks", "done": "Ran parse_results", '
        '"why": "Extracting the values via the parse_results block."}\n\n'
        f"User goal: {goal_block}\n\n"
        f"Currently doing (do NOT restate this): {current_action_label}\n\n"
        f"Latest signal: {transition_label}\n\n"
        f"Recent activity (most recent last):\n{activity_block}\n\n"
        "JSON:"
    )


# Agent tool names get remapped before reaching the LLM so internal identifiers
# can't surface via prompt echo. Unknown tools fall back to a generic phrase.
_USER_FACING_TOOL_LABELS: dict[str, str] = {
    "update_workflow": "revising the workflow draft",
    "update_and_run_blocks": "revising and testing the workflow",
    "edit_block_and_run": "revising and testing one workflow step",
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
    "list_integrations": "checking connected integrations",
    "get_block_schema": "looking up workflow block options",
    "get_workflow_knowledge": "looking up workflow guidance",
    "validate_block": "checking workflow block configuration",
    "get_run_results": "checking results of a prior run",
    "block_started": "starting a step in the workflow",
    "block_completed": "completing a step in the workflow",
    "block_failed": "a step in the workflow failed",
}


_TRANSITION_LABELS: dict[TransitionKind, str] = {
    TransitionKind.TOOL_STARTED: "just started a new action",
    TransitionKind.TOOL_IN_PROGRESS: "still working through the requested task",
    TransitionKind.BLOCK_STARTED: "starting another step in the workflow",
    TransitionKind.BLOCK_COMPLETED: "just finished a step in the workflow",
    TransitionKind.NEW_TOOL_CLUSTER: "starting a different kind of work",
    TransitionKind.BLOCK_FAILED: "a step in the workflow failed",
    TransitionKind.ENFORCEMENT_RETRY: "course-correcting after a check",
    TransitionKind.NAVIGATION_COMPLETED: "just finished loading a page",
    TransitionKind.TEST_COMPLETED: "just finished a test of the workflow",
    TransitionKind.WORKFLOW_UPDATED: "just updated the workflow draft",
}


_MAX_DETAILS_CHARS = 240


def extract_tool_details(tool_name: str, parsed: dict[str, Any], *, success: bool | None = None) -> str:
    """Compact narrator-friendly excerpt from a tool's parsed payload.

    Intentionally narrow: counts, domains, and high-level statuses only.
    Raw labels (block names, field names, URL paths, page content) are excluded
    so they can't reach the narrator prompt and be echoed at the user.

    ``success`` lets a caller override the raw ``ok`` field (e.g. a precondition
    redirect that streaming_adapter has already reclassified as non-failure) so this
    detail line doesn't contradict the entry's own success status in the narrator prompt.
    """
    if not isinstance(parsed, dict):
        return ""
    ok = parsed.get("ok", True) if success is None else success
    if not ok:
        return "last action failed"

    data = parsed.get("data")
    if not isinstance(data, dict):
        return ""

    if tool_name == "update_workflow" or tool_name == "update_and_run_blocks":
        return _format_step_status(data.get("block_count"), data)

    if tool_name in {"run_blocks_and_collect_debug", "edit_block_and_run"}:
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


class NarrationDraft(NamedTuple):
    reasoning: str
    active_label: str | None = None
    outcome_label: str | None = None


def _first_str(response: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_narration_draft(response: Any) -> NarrationDraft | None:
    """Pull the narrator's three fields from whatever the LLM handler returned.

    A bare string, or a dict carrying only the old single-field shape, still
    yields a reasoning-only draft so a model that ignores the label contract
    degrades to today's behaviour instead of emitting nothing.
    """
    if isinstance(response, str):
        text = response.strip()
        return NarrationDraft(reasoning=text) if text else None
    if isinstance(response, dict):
        reasoning = _first_str(response, ("why", "narration", "sentence", "user_response", "content", "text"))
        if not reasoning:
            return None
        return NarrationDraft(
            reasoning=reasoning,
            active_label=_first_str(response, ("doing",)),
            outcome_label=_first_str(response, ("done",)),
        )
    return None


# A finished title is only honest once the step it describes has produced a
# result. Narration scheduled at tool_started runs before the tool returns, so
# any outcome the model names there is a guess and is discarded.
_OUTCOME_KNOWN_TRANSITIONS = frozenset(
    {
        TransitionKind.BLOCK_COMPLETED,
        TransitionKind.BLOCK_FAILED,
        TransitionKind.NAVIGATION_COMPLETED,
        TransitionKind.TEST_COMPLETED,
        TransitionKind.WORKFLOW_UPDATED,
        # Only detect_transitions raises this, and only from the tool_output
        # branch, so the work it follows has returned. It also outranks
        # BLOCK_COMPLETED, so excluding it would strip the outcome from a real
        # completion it displaced.
        TransitionKind.NEW_TOOL_CLUSTER,
    }
)


def _clean_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = _sanitize_narration(raw)
    if not cleaned or _narration_leaks_identifier(cleaned):
        return None
    return cleaned


# Narration sanitization: trim, strip trailing quotes/fences the model might
# have included, collapse whitespace, and bound the length in whole words.
_MAX_NARRATION_WORDS = 40
_NARRATION_DELIMITERS = ("```", '"', "'")


def _sanitize_narration(text: str) -> str:
    cleaned = text.strip()
    for delim in _NARRATION_DELIMITERS:
        if cleaned.startswith(delim):
            cleaned = cleaned[len(delim) :].lstrip()
        if cleaned.endswith(delim):
            cleaned = cleaned[: -len(delim)].rstrip()
    words = cleaned.split()
    if len(words) > _MAX_NARRATION_WORDS:
        return " ".join(words[:_MAX_NARRATION_WORDS]) + "..."
    return " ".join(words)


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


# `skipped` is benign-completed, not a failure.
_BLOCK_STATUS_TO_TRANSITION: dict[str, TransitionKind] = {
    "running": TransitionKind.BLOCK_STARTED,
    "completed": TransitionKind.BLOCK_COMPLETED,
    "skipped": TransitionKind.BLOCK_COMPLETED,
    "failed": TransitionKind.BLOCK_FAILED,
    "terminated": TransitionKind.BLOCK_FAILED,
    "timed_out": TransitionKind.BLOCK_FAILED,
    "canceled": TransitionKind.BLOCK_FAILED,
}

_TERMINAL_BLOCK_STATUSES: frozenset[str] = frozenset(
    {
        "completed",
        "failed",
        "terminated",
        "timed_out",
        "canceled",
        "skipped",
    }
)

_BLOCK_TRANSITION_TO_SYNTHETIC_TOOL: dict[TransitionKind, str] = {
    TransitionKind.BLOCK_STARTED: "block_started",
    TransitionKind.BLOCK_COMPLETED: "block_completed",
    TransitionKind.BLOCK_FAILED: "block_failed",
}


@dataclass(frozen=True)
class BlockProgressEvent:
    """One block status change detected by record_block_transitions."""

    block_id: str
    block_label: str
    block_type: str
    status: str
    kind: TransitionKind


def record_block_transitions(
    state: NarratorState,
    snapshot: list[tuple[str, str, str, str]],
    seen_state: dict[str, str],
    iteration: int,
) -> list[BlockProgressEvent]:
    """Record transitions for status changes since the last snapshot; returns the new events for further fan-out."""
    new_events: list[BlockProgressEvent] = []
    for block_id, block_label, block_type, status in snapshot:
        if not block_id:
            continue
        prior = seen_state.get(block_id)
        if prior == status:
            continue
        seen_state[block_id] = status
        kind = _BLOCK_STATUS_TO_TRANSITION.get(status)
        if kind is None:
            continue
        synthetic_tool = _BLOCK_TRANSITION_TO_SYNTHETIC_TOOL.get(kind)
        if synthetic_tool is None:
            # Defensive: a new TransitionKind added to the status map without
            # a matching synthetic-tool entry would otherwise KeyError here.
            continue
        state.record_tool(
            tool_name=synthetic_tool,
            summary=f"workflow step {status}",
            success=(kind == TransitionKind.BLOCK_COMPLETED),
            iteration=iteration,
        )
        state.record_transition(kind)
        new_events.append(
            BlockProgressEvent(
                block_id=block_id,
                block_label=block_label,
                block_type=block_type,
                status=status,
                kind=kind,
            )
        )
    return new_events


# Returns objects exposing workflow_run_block_id and status; helper reads only those two fields.
FetchBlockStatusesCallable = Callable[[], Awaitable[list[Any]]]


class NarratorPollTickResult(NamedTuple):
    """Updated bookkeeping the polling loop must thread into its next call."""

    prior_block_ts: datetime | None
    last_block_fetch_monotonic: float


async def narrator_poll_tick(
    state: NarratorState,
    *,
    current_block_ts: datetime | None,
    prior_block_ts: datetime | None,
    last_block_fetch_monotonic: float,
    seen_block_states: dict[str, str],
    fetch_block_statuses: FetchBlockStatusesCallable,
    stream: EventSourceStream,
    block_state_map: dict[str, str] | None = None,
    block_started_at_map: dict[str, str] | None = None,
    block_ended_at_map: dict[str, str] | None = None,
    block_run_identity_map: dict[str, BlockRunIdentity] | None = None,
    workflow_run_id: str | None = None,
) -> NarratorPollTickResult:
    """Per-tick narrator bookkeeping; returns updated (prior_block_ts, last_block_fetch_monotonic).

    `prior_block_ts` advances only on a successful fetch so rate-limited and failed ticks retry on the next call.
    """
    now = time.monotonic()
    block_changed = current_block_ts != prior_block_ts
    fetch_gate_open = (now - last_block_fetch_monotonic) >= MIN_BLOCK_STATUS_POLL_GAP_SECONDS

    next_prior_block_ts = prior_block_ts
    next_last_fetch = last_block_fetch_monotonic

    if block_changed and fetch_gate_open:
        next_last_fetch = now
        try:
            blocks = await fetch_block_statuses()
        except Exception:
            LOG.debug("copilot narrator block-status fetch failed", exc_info=True)
            blocks = None

        if blocks is not None:
            snapshot: list[tuple[str, str, str, str]] = []
            for block in blocks:
                block_id = getattr(block, "workflow_run_block_id", None)
                if not block_id:
                    continue
                raw_status = getattr(block, "status", None)
                if raw_status is None:
                    continue
                status = raw_status.value if hasattr(raw_status, "value") else str(raw_status)
                if not status:
                    continue
                block_label = getattr(block, "label", None) or ""
                raw_block_type = getattr(block, "block_type", None)
                if raw_block_type is None:
                    block_type = ""
                elif hasattr(raw_block_type, "value"):
                    block_type = raw_block_type.value
                elif hasattr(raw_block_type, "name"):
                    block_type = raw_block_type.name
                else:
                    block_type = str(raw_block_type)
                snapshot.append((block_id, block_label, block_type, status))
            # Repository returns DESC by created_at; reverse for chronological order.
            snapshot.reverse()
            new_events = record_block_transitions(state, snapshot, seen_block_states, state.current_iteration)
            next_prior_block_ts = current_block_ts
            for event in new_events:
                if not event.block_label:
                    # Without a label the FE has nothing readable to render; skip
                    # rather than ship empty bullets.
                    continue
                event_ts = datetime.now(timezone.utc)
                event_ts_iso = event_ts.isoformat()
                if block_state_map is not None:
                    block_state_map[event.block_label] = event.status
                if block_run_identity_map is not None:
                    block_run_identity_map[event.block_label] = BlockRunIdentity(
                        workflow_run_block_id=event.block_id,
                        iteration=state.current_iteration,
                    )
                if event.status == "running":
                    state.running_block_label = event.block_label
                elif event.status in _TERMINAL_BLOCK_STATUSES and state.running_block_label == event.block_label:
                    state.running_block_label = None
                if (
                    block_started_at_map is not None
                    and event.status == "running"
                    and event.block_label not in block_started_at_map
                ):
                    block_started_at_map[event.block_label] = event_ts_iso
                # Clear endedAt on retry-back-to-running; overwrite on terminal
                # events to keep latest-terminal semantics.
                if block_ended_at_map is not None and event.status == "running":
                    block_ended_at_map.pop(event.block_label, None)
                if block_ended_at_map is not None and event.status in _TERMINAL_BLOCK_STATUSES:
                    block_ended_at_map[event.block_label] = event_ts_iso
                try:
                    await stream.send(
                        WorkflowCopilotBlockProgressUpdate(
                            type=WorkflowCopilotStreamMessageType.BLOCK_PROGRESS,
                            workflow_run_block_id=event.block_id,
                            workflow_run_id=workflow_run_id,
                            block_label=event.block_label,
                            block_type=event.block_type,
                            status=event.status,
                            iteration=state.current_iteration,
                            timestamp=event_ts,
                        )
                    )
                except Exception:
                    LOG.debug("copilot block_progress send failed", exc_info=True)

    schedule_narration(state, stream)

    return NarratorPollTickResult(
        prior_block_ts=next_prior_block_ts,
        last_block_fetch_monotonic=next_last_fetch,
    )
