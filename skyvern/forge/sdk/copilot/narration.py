"""Call-owned activity recording and deterministic run progress for Copilot."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog

from skyvern.forge.sdk.copilot.ask_user import (
    ACCOUNT_GROUP_CANCEL_TOOL_NAME,
    ACCOUNT_GROUP_STATUS_TOOL_NAME,
    ACCOUNT_GROUP_SUBMIT_TOOL_NAME,
    CREDENTIAL_DELETE_TOOL_NAME,
)
from skyvern.forge.sdk.copilot.code_write_diff import CodeWriteDiff
from skyvern.forge.sdk.copilot.context import ActivityBucket, NarrativeBlockAttempt, upsert_narrative_block_attempt
from skyvern.forge.sdk.copilot.output_utils import sanitize_block_label_for_display
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotBlockProgressUpdate,
    WorkflowCopilotStreamMessageType,
)

if TYPE_CHECKING:
    from skyvern.forge.sdk.copilot.context import NarrativeActivityEntry, NarrativeScreenshot, NarrativeWorkPlan
    from skyvern.forge.sdk.core.event_source_stream import EventSourceStream

LOG = structlog.get_logger()
MIN_BLOCK_STATUS_POLL_GAP_SECONDS = 1.0
MAX_BLOCK_ACTIVITY_ENTRIES = 30
MAX_DESIGN_ACTIVITY_ENTRIES = 50
ACTIVITY_TOOL_DENYLIST = frozenset({"get_run_results", "get_browser_screenshot"})
CODE_REPAIR_PROGRESS_SURFACE_KIND = "code_repair_progress"
CODE_REPAIR_PROGRESS_TEXT = "Refining the workflow's code"

_TOOL_ACTIVITY_DISPLAY_LABELS = {
    # Mirror of the FE ACTIVITY_TOOL_DISPLAY_LABELS in narrativeState.ts.
    "update_workflow": "Updating workflow",
    "update_and_run_blocks": "Testing workflow",
    "edit_block_and_run": "Editing and testing block",
    "run_blocks_and_collect_debug": "Testing workflow",
    "test_workflow_from_blank_browser": "Testing workflow in a blank browser",
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
    "read_google_sheet": "Reading the Google Sheet",
    "get_organization_usage_quota": "Checking account usage",
    "inspect_current_workflow": "Inspecting workflow",
    "discover_workflow_entrypoint": "Finding the entry page",
    "search_web": "Searching the web",
    "inspect_page_for_composition": "Inspecting the page",
    "inspect_locator_matches": "Comparing locator candidates",
    "list_credentials": "Checking saved credentials",
    "validate_block": "Checking the block",
    "list_org_workflows": "Searching your saved workflows",
    "get_org_workflow": "Reading a saved workflow",
    "console_messages": "Reading the browser console",
    "wait_for_either_state": "Waiting for the page",
    "skyvern_frame_list": "Finding embedded pages",
    "skyvern_frame_switch": "Opening embedded page",
    "skyvern_frame_main": "Returning to main page",
    "skyvern_tab_list": "Listing browser tabs",
    "skyvern_tab_new": "Opening a new tab",
    "skyvern_tab_switch": "Switching tabs",
    "skyvern_tab_close": "Closing a tab",
    "list_workflow_schedules": "Checking this workflow's schedules",
    "list_workflow_runs": "Listing this workflow's runs",
    "get_workflow_schedule": "Reading a schedule",
    "create_workflow_schedule": "Creating a schedule",
    "update_workflow_schedule": "Updating a schedule",
    "enable_workflow_schedule": "Resuming a schedule",
    "disable_workflow_schedule": "Pausing a schedule",
    "cancel_workflow_schedule": "Canceling a schedule",
    "delete_workflow_schedule": "Deleting a schedule",
    "list_browser_profiles": "Checking saved browser profiles",
    "get_browser_profile": "Reading a browser profile",
    "create_browser_profile": "Saving a browser profile",
    "fill_credential_field": "Entering saved credentials",
    "solve_page_challenge": "Solving the page's verification challenge",
    "start_fresh_browser": "Starting a fresh browser",
    "extend_browser_session": "Extending the browser session",
    "upload_attached_file": "Attaching your file to the page",
    "run_browser_code": "Working in the browser",
    "edit_block": "Editing block",
    "add_block": "Adding block",
    "delete_block": "Deleting block",
    "request_credential": "Requesting a credential",
    "ask_user": "Asking you",
    ACCOUNT_GROUP_SUBMIT_TOOL_NAME: "Reviewing the accounts with you",
    ACCOUNT_GROUP_STATUS_TOOL_NAME: "Checking the account runs",
    ACCOUNT_GROUP_CANCEL_TOOL_NAME: "Reviewing a cancel with you",
    CREDENTIAL_DELETE_TOOL_NAME: "Reviewing a credential deletion with you",
    "set_work_plan": "Updating its plan",
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
    browser_steps: list[str] | None = None,
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
    if browser_steps:
        entry["browserSteps"] = browser_steps
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


@dataclass
class NarratorState:
    """Turn-local factual activity recorder; no model or cadence state."""

    current_iteration: int = 0
    block_activity: dict[str, list[NarrativeActivityEntry]] = field(default_factory=dict)
    design_activity: list[NarrativeActivityEntry] = field(default_factory=list)
    running_block_label: str | None = None
    running_block_id: str | None = None
    tool_call_buckets: dict[str, ActivityBucket] = field(default_factory=dict)
    emitted_progress_texts: set[str] = field(default_factory=set)
    last_tool_call_id: str | None = None
    work_plan: NarrativeWorkPlan | None = None
    screenshots: list[NarrativeScreenshot] = field(default_factory=list)

    def activity_bucket(self) -> ActivityBucket:
        if self.running_block_id is not None:
            return {"kind": "block", "workflow_run_block_id": self.running_block_id}
        return {"kind": "design"}

    def record_activity(self, entry: NarrativeActivityEntry | None) -> None:
        if entry is None:
            return
        if entry["kind"] in ("tool_call", "tool_result"):
            call_id = entry["id"][3:]
            self.last_tool_call_id = call_id
            route = entry.get("activityBucket")
            if route is None:
                route = self.tool_call_buckets.get(call_id)
            if route is None:
                route = self.activity_bucket() if entry["kind"] == "tool_call" else {"kind": "design"}
            self.tool_call_buckets.setdefault(call_id, route)
            entry["activityBucket"] = route
        else:
            route = {"kind": "design"}
        if route["kind"] == "block":
            bucket = self.block_activity.setdefault(route["workflow_run_block_id"], [])
            cap = MAX_BLOCK_ACTIVITY_ENTRIES
        else:
            bucket = self.design_activity
            cap = MAX_DESIGN_ACTIVITY_ENTRIES
        if not any(saved["id"] == entry["id"] for saved in bucket):
            bucket.append(entry)
        if len(bucket) > cap:
            del bucket[:-cap]


@dataclass(frozen=True)
class BlockProgressEvent:
    block_id: str
    block_label: str
    block_type: str
    status: str


_TERMINAL_BLOCK_STATUSES = frozenset({"completed", "failed", "canceled", "terminated", "timed_out", "skipped"})


def record_block_transitions(
    state: NarratorState,
    snapshot: list[tuple[str, str, str, str]],
    seen_state: dict[str, str],
    iteration: int,
) -> list[BlockProgressEvent]:
    events: list[BlockProgressEvent] = []
    for block_id, label, block_type, status in snapshot:
        if not block_id or seen_state.get(block_id) == status:
            continue
        seen_state[block_id] = status
        if status == "running" or status in _TERMINAL_BLOCK_STATUSES:
            events.append(BlockProgressEvent(block_id, label, block_type, status))
    return events


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
    narrative_block_attempts: dict[str, NarrativeBlockAttempt] | None = None,
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
                if narrative_block_attempts is not None:
                    upsert_narrative_block_attempt(
                        narrative_block_attempts,
                        workflow_run_block_id=event.block_id,
                        workflow_run_id=workflow_run_id,
                        label=event.block_label,
                        block_type=event.block_type,
                        status=event.status,
                        iteration=state.current_iteration,
                        started_at=event_ts_iso if event.status == "running" else None,
                        ended_at=event_ts_iso if event.status in _TERMINAL_BLOCK_STATUSES else None,
                    )
                if event.status == "running":
                    state.running_block_label = event.block_label
                    state.running_block_id = event.block_id
                elif event.status in _TERMINAL_BLOCK_STATUSES and state.running_block_id == event.block_id:
                    state.running_block_label = None
                    state.running_block_id = None
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

    return NarratorPollTickResult(
        prior_block_ts=next_prior_block_ts,
        last_block_fetch_monotonic=next_last_fetch,
    )
