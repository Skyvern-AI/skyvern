"""Faithful Task V3 agent tool-loop.

A single persistent LLM conversation drives browser tools via native tool-calling:
the model emits ``tool_calls``, we execute them, thread the results back as ``tool``
messages, and repeat until the model calls a terminal tool (``finish``) or a budget
cap is hit. Perception is a tool the model chooses to call — nothing about the page
is injected automatically — which is what distinguishes this from the step engine's
scrape-every-step loop.

The loop itself is transport-agnostic: it depends only on an ``LLMCaller``-shaped
object and a list of ``ToolSpec``. Browser wiring lives in a separate module so this
core can be unit-tested with scripted fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import structlog

from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.core import skyvern_context

LOG = structlog.get_logger()

ToolStatus = Literal["ok", "error"]
FinishStatus = Literal["completed", "failed", "terminated"]


@dataclass
class ToolResult:
    status: ToolStatus
    content: str
    data: dict[str, Any] | None = None

    @classmethod
    def ok(cls, content: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls("ok", content, data)

    @classmethod
    def error(cls, content: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls("error", content, data)


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]

# A probe consulted after a billable/download-signaling tool result; a truthy return ends the run as
# completed with that reason, without the model ever calling finish. A blocker consulted from
# finish(completed) itself; a truthy return rejects that verdict with the message as the reason. Both
# receive the basenames tools staged into the downloads dir this run, to exclude from detection.
CompletionProbe = Callable[[frozenset[str]], Awaitable[str | None]]
CompletionBlocker = Callable[[frozenset[str]], Awaitable[str | None]]
# Consulted from finish(completed) like CompletionBlocker, but takes no arguments -- it gates on
# state the caller already tracks (e.g. a verification-code budget), not on staged downloads.
VerificationBlocker = Callable[[], Awaitable[str | None]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    terminal: bool = False
    billable: bool = False  # a page-mutating browser action that meters like a step-engine action
    recordable: bool = False  # persisted as an action row (with screenshot) but not billed/budgeted
    compactable: bool = False  # a large perception result safe to elide from the transcript once superseded

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class LoopOutcome:
    status: Literal["completed", "failed", "terminated", "budget_exhausted", "loop_error", "canceled"]
    reason: str
    extracted_output: Any = None
    turns: int = 0
    tool_calls: int = 0
    action_steps: int = 0
    # Wall-clock spent inside tool handlers, summed over the run. Serial by construction, so it is
    # directly comparable against the run's total duration.
    tool_seconds: float = 0.0
    # Turns where the model answered with prose instead of a tool call, costing a full round trip
    # plus the NO_TOOL_CALL_NUDGE recovery turn.
    no_tool_call_turns: int = 0
    # Whether tool_choice was still being sent when the run ended. Distinguishes a run that was
    # asked to force tool calls from one where the request was degraded away mid-run.
    tool_choice_in_effect: bool = False
    billable_actions: list[str] = field(default_factory=list)
    # Perception snapshots are compacted in place during the run, so superseded observe/get_html
    # content is already elided here — treat as lossy if ever persisted for audit.
    messages: list[dict[str, Any]] = field(default_factory=list)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    # raw_response=True returns a model_dump() dict, but test fakes and some
    # providers hand back objects — accept either shape.
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_message(response: Any) -> Any:
    choices = _get(response, "choices") or []
    if not choices:
        return None
    return _get(choices[0], "message")


def _extract_text(response: Any) -> str | None:
    message = _extract_message(response)
    if message is None:
        return None
    return _get(message, "content")


def _extract_tool_calls(response: Any) -> list[tuple[str, str, dict[str, Any]]]:
    message = _extract_message(response)
    if message is None:
        return []
    raw_tool_calls = _get(message, "tool_calls") or []
    tool_calls: list[tuple[str, str, dict[str, Any]]] = []
    for raw in raw_tool_calls:
        function = _get(raw, "function") or {}
        name = _get(function, "name")
        if not name:
            continue
        tool_call_id = _get(raw, "id") or f"call_{len(tool_calls)}"
        arguments = _get(function, "arguments")
        if isinstance(arguments, str):
            try:
                parsed_args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed_args = {}
        elif isinstance(arguments, dict):
            parsed_args = arguments
        else:
            parsed_args = {}
        tool_calls.append((tool_call_id, name, parsed_args))
    return tool_calls


NO_TOOL_CALL_NUDGE = (
    "You did not call a tool. Call a browser tool to make progress, or call "
    "finish(status, reason, extracted_output) if the goal is complete. Emit a tool call now."
)

# Perception-stall policy: N consecutive identical (marker-canonicalized) snapshots from the same (compactable) tool
# AND from the same probe (that tool with the same arguments) mean the page has stopped changing in
# response to actions, and a page gated by something the run cannot perceive or operate otherwise
# burns the whole budget on identical re-observes. The per-probe term is what keeps two different
# probes returning the same string from reading as one frozen page. Only compactable tools count:
# action tools legitimately return the same string every call ("waited"), so they can never witness
# "the page is unchanged".
PERCEPTION_STALL_NUDGE_AFTER = 6
PERCEPTION_STALL_TERMINATE_AFTER = 15

# Stable, facetable prefix for the stall verdict's reason — telemetry queries key on it to measure
# how often the policy fires; change it only with the dashboards that read it.
PERCEPTION_STALL_REASON_PREFIX = "perception_stall:"

# Action-loop policy: N repeated executions of the same billable action (same tool + same args)
# with no new evidence the page changed mean the run is re-trying against an unchanged outcome —
# the live shape is re-submitting into the same rejection banner, which the stall policy cannot
# see because interleaved actions and varied probes keep the perception stream changing while the
# SITUATION stays the same. Evidence of change is a REPEATED probe (same tool + same args)
# returning different content, or a download landing; a first-time probe has no baseline and is
# evidence of nothing, so varied-selector probing cannot launder repetition into "progress".
ACTION_LOOP_NUDGE_AFTER = 3
ACTION_LOOP_TERMINATE_AFTER = 6

# Facetable sibling of PERCEPTION_STALL_REASON_PREFIX; same dashboard contract.
ACTION_LOOP_REASON_PREFIX = "action_loop:"

# Emitted, never acted on, when the oscillation rule WOULD have terminated. The step engine's
# tripwires (skyvern/forge/sdk/fail_fast/shadow.py) earn the right to act by publishing this event
# first and deriving a decision precision from it; a rule that ADDS terminations gets the same
# treatment rather than being trusted because it looks right.
PERCEPTION_STALL_SHADOW_EVENT = "taskv3 loop perception stall would fire"

# Emitted once per run, where the argument-blind per-tool counter first reached the terminator and
# the per-probe term did not. It marks a verdict withheld, not a run spared: most such runs end on
# another guard, so read it joined to the final status.
PERCEPTION_STALL_SUPPRESSED_EVENT = "taskv3 loop perception stall suppressed_main_fire"

# Guard-attribution hashes: sha256 over a per-run secret salt, truncated to 64 bits — enough to join a
# firing to the value the guard compared within one run (cardinality ≤ max_tool_calls), while the salt
# keeps a low-entropy input (a short typed text, a selector) non-enumerable, which truncation alone does not.
TELEMETRY_HASH_HEX_LEN = 16


def telemetry_hash(salt: str, *parts: str) -> str:
    return hashlib.sha256("\x1f".join((salt, *parts)).encode()).hexdigest()[:TELEMETRY_HASH_HEX_LEN]


# The value shape observe()'s enrichment mints ('t' + monotonic counter, optional '-<n>'
# disambiguator — tools._OBSERVE_JS): identity handles, not page semantics, and a node-replacing
# framework re-mints them on every read, so hashed raw they hide a frozen page from the stall
# guard. Page-authored data-tv3 values (any other shape) are page content and stay significant.
_TV3_MARKER_VALUE_RE = re.compile(r'data-tv3="t\d+(?:-\d+)?"')

# get_html truncates to a fixed budget before the loop ever sees the content, so a marker the cut
# leaves open at the tail has no closing quote for the pattern above and its churning digits would
# be the one leak that survives canonicalization. The lookahead assumes the truncation notice itself
# carries no quote character, and this sub must run AFTER closed markers are rewritten to the
# quote-bearing placeholder — either broken silently brings the leak back.
_TV3_MARKER_CUT_RE = re.compile(r'data-tv3="t\d*(?:-\d*)?(?=[^"]*\Z)')


def _canonical_perception_content(content: str) -> str:
    return _TV3_MARKER_CUT_RE.sub('data-tv3="*', _TV3_MARKER_VALUE_RE.sub('data-tv3="*"', content))


# How many recent states a probe remembers. This length IS the longest oscillation period that can
# be recognised, so it is a detection limit and not a memory tuning knob.
PERCEPTION_RING = 8


@dataclass
class _ProbeStreak:
    history: deque[str]
    # Consecutive snapshots identical to the previous one; a match means two reads, so it opens at 2.
    identical: int = 0
    # Consecutive snapshots matching ANY state still in the ring (a superset of ``identical``).
    revisits: int = 0


@dataclass
class _Snapshot:
    """Streak readings after one snapshot; the verdict reads ``live``, the warning ``tool_identical``."""

    # Per-tool consecutive-identical count, argument-blind: the counter this guard shipped with.
    tool_identical: int
    probe_identical: int
    probe_revisits: int
    # A repeated probe returned different content: in-loop evidence that the page changed.
    progressed: bool

    @property
    def live(self) -> int:
        return min(self.tool_identical, self.probe_identical)


class _PerceptionLedger:
    """No-progress streaks of perception snapshots, kept per tool AND per probe (tool, canonical args).

    The loop acts only where BOTH agree: the tool has returned the same string N times running and
    the same probe has too. The per-probe term is what stops distinct probes that happen to return
    the same string (a form full of not-yet-chosen dropdowns) from reading as one frozen page; the
    per-tool term is what keeps firing a subset of what the argument-blind counter fired, so a
    frozen page interleaved with a live sibling probe — which content alone cannot tell from a live
    page whose static region is re-read — is never terminated where it was not before.

    Streaks are scoped to the probe that produced them. Progress in one probe says nothing about
    another: a sibling that ticks every read (a clock, a log tail) must not keep a frozen page
    alive to the budget cap, which is exactly what clearing on any probe's progress would do.

    Counts per RESULT, not per round: the thresholds are denominated in snapshots, and a turn that
    batches five identical probes has taken five of them.

    ``revisits`` additionally treats a return to any state still in the ring as no progress, which
    is what catches a control toggling open and shut. That is NEW firing, so it is only reported.
    """

    def __init__(self) -> None:
        self._probes: dict[tuple[str, str], _ProbeStreak] = {}
        self._tools: dict[str, tuple[str, int]] = {}
        self.shadow_reported = False
        self.suppressed_reported = False

    def first_time(self, key: tuple[str, str]) -> bool:
        return key not in self._probes

    def record(self, key: tuple[str, str], content_digest: str) -> _Snapshot:
        tool_name = key[0]
        prev = self._tools.get(tool_name)
        tool_identical = prev[1] + 1 if prev is not None and prev[0] == content_digest else 1
        self._tools[tool_name] = (content_digest, tool_identical)

        probe = self._probes.get(key)
        if probe is None:
            self._probes[key] = _ProbeStreak(deque([content_digest], maxlen=PERCEPTION_RING))
            return _Snapshot(tool_identical, 0, 0, False)
        progressed = content_digest != probe.history[-1]
        if progressed:
            probe.identical = 0
        else:
            probe.identical = probe.identical + 1 if probe.identical else 2
        if content_digest in probe.history:
            probe.revisits = probe.revisits + 1 if probe.revisits else 2
        else:
            probe.revisits = 0
        probe.history.append(content_digest)
        return _Snapshot(tool_identical, probe.identical, probe.revisits, progressed)

    def next_snapshot_can_trip(self, threshold: int) -> bool:
        """Whether ONE more read of some probe, returning what it last returned, reaches ``threshold``
        live. This is the trip's exact precondition, not an estimate of it: the next read continues
        the tool counter only if that content is also the tool's last content, so a probe that went
        dormant while its tool moved on to other content cannot be the one that trips."""
        for (tool_name, _), probe in self._probes.items():
            tool_last_content, tool_identical = self._tools[tool_name]
            if probe.history[-1] != tool_last_content:
                continue
            next_probe_identical = probe.identical + 1 if probe.identical else 2
            if min(tool_identical + 1, next_probe_identical) >= threshold:
                return True
        return False


# Failure-evidence gate: a finish(failed) issued shortly after a submit-class action or a
# solve_captcha attempt is held for ONE evidence turn, because submissions and captcha protocols
# complete asynchronously — the sampled false-negative verdicts fired 2-7s after the model's last
# look while the page went on to show the submission confirmation. Trigger tools are the ones whose
# page effects can land after their tool result; the window is in loop turns so intervening
# perception does NOT disarm it (the state can flip after the last observe while a protocol is in
# flight). The true verdict-to-flip latency is unmeasured in the sampled replays: the quiescence
# wait exits on the first stable fingerprint pair (so honest gated failures pay ~one sample), the
# cap only bounds a still-mutating page, and the effective evidence window is dominated by the
# deferral round-trip itself (one LLM turn + the observe).
# Completed-side settle deferrals. 0 disables that gate while leaving the failure-evidence gate
# (which shares the fingerprint sampler) intact.
DEFAULT_MAX_SETTLE_DEFERRALS = 2
FAILURE_EVIDENCE_WINDOW_TURNS = 5
FAILURE_EVIDENCE_SETTLE_MAX_SECONDS = 8.0
# A deferral needs room for its corrected cycle; without it the gate would convert an honest
# failure verdict into budget_exhausted (a budget cap landing mid-deferral). The worst-case cycle
# is wait + observe + re-finish — the deferral message invites an optional brief wait — so both
# the turn and tool-call reservations are 3, the latter read from a per-call refreshed counter.
# The deadline headroom must additionally fund the settle cap plus the cycle's LLM round trips.
FAILURE_EVIDENCE_MIN_DEADLINE_HEADROOM_SECONDS = 60.0
FAILURE_EVIDENCE_MIN_TOOL_CALLS = 3
FAILURE_EVIDENCE_MIN_TURNS = 3


def _arms_failure_evidence(tool_name: str, args: dict[str, Any], ok: bool) -> bool:
    """solve_captcha arms on ANY dispatch — its "not solved" error is exactly the verdict the async
    protocol can contradict. Other actions arm only when they reached the page AND in their
    submit-shaped form: any click, an Enter press, or a type that pressed Enter."""
    if tool_name == "solve_captcha":
        return True
    if not ok:
        return False
    if tool_name == "click":
        # Deliberately broad: the loop has no submit-classification for click targets, and
        # narrowing by label/selector text is the marker-parsing this design avoids.
        return True
    if tool_name == "press_key":
        return str(args.get("key", "")).strip().lower() in ("enter", "return", "numpadenter")
    if tool_name == "type":
        return bool(args.get("press_enter"))
    return False


@dataclass
class ActivityRecency:
    """Written by the tool loop each turn/action, read by the finish tool's failure-evidence gate."""

    turn: int = 0
    turns_remaining: int | None = None
    tool_calls_remaining: int | None = None
    tokens_remaining: int | None = None
    last_turn_tokens: int = 0
    last_trigger_turn: int | None = None
    # True while one more read of some probe, returning what it last returned, would trip the stall
    # terminator: a deferral-forced observe must never be the snapshot that trips it. KNOWN LIMIT: a
    # run that reaches the edge and then stops reading that tool altogether leaves this true for the
    # rest of the run, disabling the deferral (same as the argument-blind counter it shipped with).
    perception_stall_imminent: bool = False

    def armed(self, window: int = FAILURE_EVIDENCE_WINDOW_TURNS) -> bool:
        return self.last_trigger_turn is not None and (self.turn - self.last_trigger_turn) <= window


def _names_submit_control(tool_name: str, args: dict[str, Any], ok: bool) -> str | None:
    """The selector of a control a successful action acted on directly, or None.

    Only `click`. An Enter press and a type-that-pressed-Enter submit through a control they do not
    name, and a captcha dispatch names none at all, so for all three "is that control still in
    flight" has no subject — and the selector they do carry is a text field, whose value is the
    model's own typed text."""
    if not ok or tool_name != "click":
        return None
    selector = args.get("selector")
    return selector if isinstance(selector, str) and selector else None


def _has_hold_headroom(activity: ActivityRecency | None, deadline_at: float | None) -> bool:
    """Whether a deferral has the budget to buy the re-verification turn it asks for.

    Without it the run ends budget_exhausted, which is unmapped and lands on failed -- turning an
    honest hold into the false failure it exists to avoid. Every axis the failure-evidence gate
    reserves, for the same reason: a token exhaustion and a stall streak one short of its terminator
    each convert the deferral into a verdict the gate did not choose."""
    if activity is not None:
        if activity.turns_remaining is not None and activity.turns_remaining < FAILURE_EVIDENCE_MIN_TURNS:
            return False
        if (
            activity.tool_calls_remaining is not None
            and activity.tool_calls_remaining < FAILURE_EVIDENCE_MIN_TOOL_CALLS
        ):
            return False
        if activity.tokens_remaining is not None and activity.tokens_remaining < FAILURE_EVIDENCE_MIN_TURNS * max(
            activity.last_turn_tokens, 1
        ):
            return False
        if activity.perception_stall_imminent:
            return False
    if deadline_at is not None and deadline_at - time.monotonic() < FAILURE_EVIDENCE_MIN_DEADLINE_HEADROOM_SECONDS:
        return False
    return True


@dataclass
class SubmitWatch:
    """The control a click last acted on, and whether a completed verdict has already been held once
    against it.

    Deliberately NOT part of `ActivityRecency`. That record arms failure evidence, where a broad
    trigger (any click, any captcha dispatch) and a decaying turn window are both correct. Neither is
    correct here: a captcha dispatch carries no selector and would erase the control, and a turn
    window expires while the run is doing the waiting this gate asked for. There is no window — the
    probe is the arbiter, because a control that resolves and still reads as in flight IS the
    question, where elapsed turns are only a proxy for it."""

    selector: str | None = None
    deferred: bool = False

    def record(self, selector: str) -> None:
        self.selector = selector
        self.deferred = False

    def clear(self) -> None:
        self.selector = None
        self.deferred = False


def _unblocker_options(available_tools: set[str]) -> list[str]:
    options = []
    if "solve_captcha" in available_tools:
        options.append("if the page may be waiting on a verification widget, call solve_captcha")
    if "get_html" in available_tools:
        options.append("take ONE targeted get_html look at the region that should be changing")
    options.append("if the goal is already met, call finish(status=completed)")
    options.append("if genuinely blocked, call finish(status=terminated) naming the blocker as the reason")
    return options


def _stall_nudge_text(stalled: list[tuple[str, int]], available_tools: set[str]) -> str:
    """One warning naming every stalled perception tool and the unblockers this run actually has —
    a model that cannot see the gate won't reach for solve_captcha unless the symptom names it."""
    symptoms = "; ".join(f"{name} has returned identical output {count} times in a row" for name, count in stalled)
    return (
        f"The page is not changing: {symptoms}, despite your actions (transient element-marker ids are "
        "ignored when comparing). Do not keep re-observing, "
        "waiting, or repeating the same action. Your options: " + "; ".join(_unblocker_options(available_tools)) + "."
    )


def _action_target(args: dict[str, Any]) -> str:
    return str(args.get("selector") or args.get("url") or args.get("key") or "the same target")


def _action_nudge_text(repeats: list[tuple[str, dict[str, Any], int]], available_tools: set[str]) -> str:
    """The transcript cannot show the model its own repetition (superseded snapshots are compacted
    away), so the warning carries that memory: which action, how many times, and that the observed
    state did not change."""
    symptoms = "; ".join(
        f"you have called {name} on {_action_target(args)} {count} times" for name, args, count in repeats
    )
    return (
        f"You are repeating the same action without effect: {symptoms}, and the page state you "
        "last observed is unchanged since before the first attempt. A message inviting you to "
        "retry (e.g. 'please submit again') is not an instruction to loop — at most one retry, "
        "then report the outcome honestly. Your options: " + "; ".join(_unblocker_options(available_tools)) + "."
    )


_TOOL_CALL_RECORD_FIELDS = frozenset(
    {
        "tool",
        "tool_status",
        "duration_seconds",
        "result_chars",
        "selector_present",
        "billable",
        "turn",
        "batch_size",
        "batch_index",
        "action_key_hash",
        "snapshot_digest",
        "probe_first_time",
    }
)


def _observe_summary_fields(result: ToolResult) -> dict[str, int]:
    """Counts only: the summary is built by the tool, but an indexed field is re-checked here."""
    summary = (result.data or {}).get("summary")
    if not isinstance(summary, dict):
        return {}
    return {
        key: value
        for key, value in summary.items()
        if key not in _TOOL_CALL_RECORD_FIELDS and isinstance(value, int) and not isinstance(value, bool)
    }


def _append_skipped_tool_results(
    messages: list[dict[str, Any]], remaining: list[tuple[str, str, dict[str, Any]]], reason: str
) -> None:
    """Answer tool_calls we stopped before executing, so every id in the assistant turn has a
    matching tool result. An unanswered tool_call is an invalid transcript for the next call."""
    for tool_call_id, tool_name, _args in remaining:
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": f"skipped: {reason}"}
        )


def make_finish_tool(
    page_fingerprint: Callable[[], Awaitable[str | None]] | None = None,
    max_settle_deferrals: int = DEFAULT_MAX_SETTLE_DEFERRALS,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    deadline_at: float | None = None,
    settle_wait_seconds: float = 0.7,
    activity: ActivityRecency | None = None,
    max_failure_deferrals: int = 1,
    failure_settle_max_seconds: float = FAILURE_EVIDENCE_SETTLE_MAX_SECONDS,
    pending_marker: Callable[[str], Awaitable[str | None]] | None = None,
    submit_watch: SubmitWatch | None = None,
    completion_blocker: CompletionBlocker | None = None,
    staged_downloads: set[str] | None = None,
    verification_blocker: VerificationBlocker | None = None,
) -> ToolSpec:
    """`page_fingerprint` samples an opaque fingerprint of the page's rendered content (None when no
    page is available). A finish(completed) is deferred (bounded by `max_settle_deferrals`, then
    accepted) unless two samples `settle_wait_seconds` apart match, so the model re-verifies against
    the settled state instead of a mid-render shell — delayed loads otherwise produce stochastic
    false completions. A sampling error is unknown, not settled: it defers. The wait between samples
    is capped at `deadline_at` (time.monotonic clock) and abandoned once `should_cancel` reports
    True, so probing cannot outlive the loop's own bounds.

    The symmetric failure side: when `activity` reports recent submit-class/captcha activity, a
    finish(failed) is held for ONE evidence turn (`max_failure_deferrals`, per run like the
    completed-side cap, not per verdict attempt) — a quiescence wait
    bounded by `failure_settle_max_seconds`, then a deferral asking the model to re-observe —
    because async submissions and captcha protocols otherwise produce false-negative verdicts.
    terminated is never gated on either side.

    `pending_marker` reports the text the page still shows the control in `submit_watch` as in
    flight with, or None. A settled page is not a submitted one -- a submit frozen mid-flight is
    maximally stable, so the settle probe is satisfied by exactly the state it should refuse. A
    completed verdict is held ONCE against that marker, with the re-observe it asks for budgeted; if
    the model insists a second time its verdict stands, so a run that declares completion on a
    still-pending control remains possible after this gate. Deliberately a POSITIVE observation -- a
    probe that fails reports nothing, and nothing is not evidence of pending, so it accepts rather
    than holding a run on probe flakiness."""
    deferrals = 0
    failure_deferrals = 0

    async def _quiesced() -> bool:
        """Bounded wait for the page to stop mutating before the failure verdict's evidence turn.
        Returns False when there is no page to observe (a deferral would burn a turn for nothing)."""
        assert page_fingerprint is not None  # gated by the caller's None check
        prev = await page_fingerprint()
        if prev is None:
            return False
        cap_at = time.monotonic() + failure_settle_max_seconds
        while True:
            wait = min(settle_wait_seconds, cap_at - time.monotonic())
            if deadline_at is not None:
                wait = min(wait, deadline_at - time.monotonic())
            if wait <= 0:
                return True
            await asyncio.sleep(wait)
            if should_cancel is not None and await should_cancel():
                return True  # defer: the loop's cancellation check ends the run before another turn
            current = await page_fingerprint()
            if current is None:
                return False
            if current == prev:
                return True
            prev = current

    async def _settled() -> bool:
        assert page_fingerprint is not None  # gated by the caller's None check
        first = await page_fingerprint()
        if first is None:
            return True  # no page to sample (non-recovering peek): accept the verdict as-is
        wait = settle_wait_seconds
        if deadline_at is not None:
            wait = min(wait, deadline_at - time.monotonic())
        if wait > 0:
            await asyncio.sleep(wait)
        if should_cancel is not None and await should_cancel():
            return False  # defer: the loop's cancellation check ends the run before another turn
        return first == await page_fingerprint()

    async def handler(args: dict[str, Any]) -> ToolResult:
        nonlocal deferrals, failure_deferrals
        status = args.get("status")
        if status not in ("completed", "failed", "terminated"):
            return ToolResult.error(
                f"invalid finish status: {status!r}; call finish again with status=completed|failed|terminated"
            )
        if (
            status == "completed"
            and pending_marker is not None
            and submit_watch is not None
            and submit_watch.selector
            and not submit_watch.deferred
            # Holding costs a turn, the tool calls of the re-observe it asks for, and deadline
            # seconds. Without the headroom to spend them the run ends budget_exhausted, which is
            # unmapped and lands on failed -- turning an honest hold into a false failure.
            and _has_hold_headroom(activity, deadline_at)
        ):
            try:
                marker = await pending_marker(submit_watch.selector)
            except Exception:
                # Warning, not debug: the only way here is a broken probe, and a silently disabled
                # gate reads exactly like a page that was never pending.
                LOG.warning("taskv3 pending-marker probe failed; not treating it as pending", exc_info=True)
                marker = None
            if marker:
                submit_watch.deferred = True
                LOG.info("taskv3 completed verdict held: submission still in flight", marker=marker)
                return ToolResult.error(
                    f"the page still shows a submission in flight: {marker}. That is not a "
                    "settled failure OR a confirmation -- wait for it to resolve and re-observe. "
                    "Finish with status=completed only once the page shows the submission "
                    "landed; if it never resolves, say so with status=terminated."
                )
        if status == "completed" and completion_blocker is not None:
            try:
                blocker_message = await completion_blocker(frozenset(staged_downloads or ()))
            except Exception:
                # Fail closed: a download-gated task must not complete on a storage hiccup with no file.
                LOG.warning("taskv3 completion_blocker failed; failing closed", exc_info=True)
                return ToolResult.error(
                    "Could not verify that a file download has finished; retry finish(status=completed) "
                    "once the download has landed, or finish with status=failed or status=terminated."
                )
            if blocker_message:
                return ToolResult.error(blocker_message)
        if status == "completed" and verification_blocker is not None:
            try:
                verification_message = await verification_blocker()
            except Exception:
                # Fail closed: an exception here must not let a blank verification step read as done.
                LOG.warning("taskv3 verification_blocker failed; failing closed", exc_info=True)
                return ToolResult.error(
                    "Could not verify that the verification-code step completed cleanly; retry "
                    "finish(status=completed) once verified, or finish with status=failed or "
                    "status=terminated."
                )
            if verification_message:
                return ToolResult.error(verification_message)
        if status == "completed" and page_fingerprint is not None and deferrals < max_settle_deferrals:
            try:
                settled = await _settled()
            except Exception:
                # Fail closed: an exception while probing is evidence of nothing, so the verdict is
                # deferred for re-verification rather than validated. The deferral cap still bounds it.
                settled = False
            if not settled:
                deferrals += 1
                return ToolResult.error(
                    "the page was still rendering, or could not be verified as settled, when you "
                    "called finish. Wait for it to settle, re-observe, confirm the goal's effect is "
                    "present in the loaded content (not a loading indicator or empty container), "
                    "then finish again."
                )
        if (
            status == "failed"
            and activity is not None
            and page_fingerprint is not None
            and failure_deferrals < max_failure_deferrals
            and activity.armed()
            # The corrected cycle needs budget for its worst case (wait + observe + re-finish);
            # without headroom on every budget axis a deferral would convert an honest failure
            # into budget_exhausted (or, for a stall-streak one short of the terminator, into a
            # generic stall termination that replaces the model's accurate reason).
            and (activity.turns_remaining is None or activity.turns_remaining >= FAILURE_EVIDENCE_MIN_TURNS)
            and (
                activity.tool_calls_remaining is None
                or activity.tool_calls_remaining >= FAILURE_EVIDENCE_MIN_TOOL_CALLS
            )
            # The token margin is deliberately approximate: sized off the triggering turn, while
            # the deferral turns carry a slightly larger transcript.
            and (
                activity.tokens_remaining is None
                or activity.tokens_remaining >= FAILURE_EVIDENCE_MIN_TURNS * max(activity.last_turn_tokens, 1)
            )
            and not activity.perception_stall_imminent
            and (
                deadline_at is None or deadline_at - time.monotonic() >= FAILURE_EVIDENCE_MIN_DEADLINE_HEADROOM_SECONDS
            )
        ):
            should_defer = True
            try:
                # False only when there is no page to observe; cancellation mid-wait still defers
                # (the loop's own cancel check ends the run first).
                should_defer = await _quiesced()
            except Exception:
                pass  # unknown page state still defers: the model's re-observe is the evidence step
            if should_defer:
                failure_deferrals += 1
                LOG.info("taskv3 finish failure deferred for evidence", turn=activity.turn)
                return ToolResult.error(
                    "failure verdict held for one evidence check: it follows recent page actions or "
                    "a captcha attempt whose effects can land after your last look — submissions and "
                    "captcha protocols often complete asynchronously, so the page may no longer show "
                    "the state this verdict was based on. Re-observe the page once (waiting briefly "
                    "first if it may still be processing): only a positive confirmation of the goal "
                    "(e.g. a submission confirmation banner) justifies finishing with "
                    "status=completed; if it still shows the blocked or failed state, or shows no "
                    "positive confirmation at all, finish with status=failed again and the verdict "
                    "will stand."
                )
        return ToolResult.ok(
            content="Task attempt ended. No further actions are permitted.",
            data={
                "status": status,
                "reason": args.get("reason") or "",
                "extracted_output": args.get("extracted_output"),
            },
        )

    return ToolSpec(
        name="finish",
        description=(
            "End the task and report whether the browser goal was completed. Call this only when "
            "the goal is met (status=completed) or is impossible/blocked (failed/terminated)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["completed", "failed", "terminated"]},
                "reason": {"type": "string", "maxLength": 2000},
                "extracted_output": {"description": "Structured output requested by the goal, if any."},
            },
            "required": ["status", "reason"],
        },
        handler=handler,
        terminal=True,
    )


_COMPACTED_PREFIX = "[superseded "


def _compact_transcript(messages: list[dict[str, Any]], snapshot_indices: set[int]) -> None:
    """Bound the persistent conversation by eliding stale perception snapshots.

    The full transcript is re-sent every turn, so large perception outputs (an `observe` snapshot the
    agent has already acted past, or a 20k-char `get_html` dump) otherwise pile up until the token
    backstop trips on perception-heavy pages. `snapshot_indices` holds the message indices of the
    *successful* perception results (recorded as they are appended); keep the newest of each such tool
    and replace older ones' content with a short placeholder. Two things are deliberately protected:

    - The most-recent round (results after the last assistant message) is never touched — a single turn
      can batch several perception calls, and compaction runs *before* the model has seen that round's
      results, so eliding any of them would drop data the model requested but never read.
    - Only a successful snapshot is ever a candidate: a skip/error result is never recorded in
      `snapshot_indices`, so it can neither be elided nor shadow the real snapshot and leave the agent
      with no usable page view — regardless of content length (a verbose provider error included).

    Only a `tool` message's content is shrunk, never removed, so every tool_call keeps a matching result
    and the transcript stays valid. Eliding also drops the index, so re-running is a no-op and an elided
    placeholder can never re-anchor as the live snapshot."""
    if not snapshot_indices:
        return
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_assistant_idx = i
            break
    seen: set[str] = set()
    for i in sorted(snapshot_indices, reverse=True):
        name = messages[i]["name"]
        if i > last_assistant_idx or name not in seen:
            seen.add(name)  # the still-unread latest round, or the newest snapshot of this tool — keep
            continue
        messages[i]["content"] = f"{_COMPACTED_PREFIX}{name} output elided to bound context]"
        snapshot_indices.discard(i)


async def run_agent_tool_loop(
    *,
    llm_caller: Any,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolSpec],
    max_turns: int,
    max_tool_calls: int,
    max_action_steps: int | None = None,
    prompt_name: str = "taskv3-agent-loop",
    organization_id: str | None = None,
    call_kwargs: dict[str, Any] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    on_action_round: Callable[[list[tuple[str, dict[str, Any], bool]]], Awaitable[None]] | None = None,
    on_pre_action: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    max_tokens: int | None = None,
    deadline_seconds: float | None = None,
    retryable_call_exceptions: tuple[type[BaseException], ...] = (),
    max_call_retries: int = 0,
    call_retry_base_delay: float = 1.0,
    stall_nudge_after: int | None = PERCEPTION_STALL_NUDGE_AFTER,
    stall_terminate_after: int | None = PERCEPTION_STALL_TERMINATE_AFTER,
    action_nudge_after: int | None = ACTION_LOOP_NUDGE_AFTER,
    action_terminate_after: int | None = ACTION_LOOP_TERMINATE_AFTER,
    activity: ActivityRecency | None = None,
    submit_watch: SubmitWatch | None = None,
    telemetry_salt: str | None = None,
    completion_probe: CompletionProbe | None = None,
    staged_downloads: set[str] | None = None,
) -> LoopOutcome:
    tool_by_name = {tool.name: tool for tool in tools}
    # Per run, never logged: the hashes it keys are stable within this run (the only scope any guard
    # decision spans) and uncorrelatable across runs, so page content and arguments cannot be
    # fingerprinted across tenants from telemetry.
    if telemetry_salt is None:
        telemetry_salt = secrets.token_hex(16)
    openai_tools = [tool.to_openai_tool() for tool in tools]

    # We own the message array and assign it to the caller's message_history before
    # each call, passing prompt=None: LLMCaller.use_message_history never appends the
    # assistant reply or tool results itself, so multi-turn tool use must be threaded here.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # Indices into `messages` of successful perception results, recorded as they are appended so
    # compaction can keep only the newest of each without inferring "real snapshot" from content size.
    snapshot_indices: set[int] = set()
    perception = _PerceptionLedger()
    # The action-loop counter: (repeat count, first turn of the streak) per billable action
    # identity, cleared whenever evidence of page change arrives. action_warned holds the streaks
    # whose warning was actually DELIVERED — termination is gated on it, so the model always gets
    # the warning (and a chance to self-correct) at least one turn before the verdict.
    action_counts: dict[tuple[str, str], tuple[int, int]] = {}
    action_warned: set[tuple[str, str]] = set()

    def _clear_action_state() -> None:
        action_counts.clear()
        action_warned.clear()

    outcome: LoopOutcome | None = None
    # Mutable for the run: a provider that rejects tool_choice rejects it every turn, so a drop
    # made once must stick.
    active_call_kwargs = dict(call_kwargs or {})

    def _degrade_tool_choice(exc: BaseException) -> bool:
        """Drop tool_choice and report whether the turn is worth re-issuing.

        Called only when the turn is otherwise about to end the run, so the cost is one extra call
        on a run that was already failing. A context-window overflow is excluded because dropping a
        parameter provably cannot fix it.
        """
        if isinstance(exc, SkyvernContextWindowExceededError):
            return False
        if active_call_kwargs.pop("tool_choice", None) is None:
            return False
        LOG.warning("taskv3 loop retrying without tool_choice", turn=turns, exc_info=True)
        return True

    turns = 0
    no_tool_call_turns = 0
    total_tool_calls = 0
    tool_seconds = 0.0
    total_tokens = 0
    billable_actions: list[str] = []
    action_steps = 0
    started_at = time.monotonic()

    while outcome is None:
        if should_cancel is not None and await should_cancel():
            outcome = LoopOutcome("canceled", "run canceled")
            break
        if deadline_seconds is not None and time.monotonic() - started_at > deadline_seconds:
            outcome = LoopOutcome("budget_exhausted", f"deadline ({deadline_seconds:.0f}s) reached")
            break
        if max_tokens is not None and total_tokens >= max_tokens:
            outcome = LoopOutcome("budget_exhausted", f"max_tokens ({max_tokens}) reached")
            break
        if turns >= max_turns:
            outcome = LoopOutcome("budget_exhausted", f"max_turns ({max_turns}) reached")
            break
        if total_tool_calls >= max_tool_calls:
            outcome = LoopOutcome("budget_exhausted", f"max_tool_calls ({max_tool_calls}) reached")
            break
        turns += 1
        if activity is not None:
            activity.turn = turns
            activity.turns_remaining = max_turns - turns
            activity.tool_calls_remaining = max_tool_calls - total_tool_calls

        # Elide superseded perception results before re-sending the transcript, so a perception-heavy
        # run can't balloon the context to the token backstop (the pre-compaction runaway mode).
        _compact_transcript(messages, snapshot_indices)
        llm_caller.message_history = list(messages)
        # Retry only the LLM call on transient provider errors. No browser tool has run this
        # turn, so re-issuing the same call is side-effect-free — unlike a whole-task retry,
        # which would re-execute prior clicks/types. This restores the step engine's transient
        # resilience, which v3 otherwise loses by running as one non-retried unit.
        response = None
        call_attempt = 0
        while True:
            try:
                response = await llm_caller.call(
                    prompt=None,
                    prompt_name=prompt_name,
                    organization_id=organization_id,
                    tools=openai_tools,
                    use_message_history=True,
                    raw_response=True,
                    **active_call_kwargs,
                )
                break
            except retryable_call_exceptions as exc:
                call_attempt += 1
                if call_attempt > max_call_retries:
                    # A provider rejecting the parameter surfaces here, not in the generic handler
                    # below: litellm's 400s subclass openai.APIError, which the LLM layer maps to
                    # the retryable type. Degrading only after the transient budget is spent keeps
                    # a passing blip from disabling the lever for the rest of the run.
                    if _degrade_tool_choice(exc):
                        # Spend the transient budget once, not once per parameter set: the degraded
                        # turn gets a single shot, which is what "last resort" is worth.
                        call_attempt = max_call_retries
                        continue
                    LOG.warning(
                        "taskv3 loop LLM call failed after retries", turn=turns, attempts=call_attempt, exc_info=True
                    )
                    outcome = LoopOutcome("loop_error", f"llm_call_failed: {type(exc).__name__}: {exc}")
                    break
                LOG.info("taskv3 loop retrying transient LLM error", turn=turns, attempt=call_attempt)
                await asyncio.sleep(call_retry_base_delay * (2 ** (call_attempt - 1)))
            except Exception as exc:
                if _degrade_tool_choice(exc):
                    continue
                LOG.warning("taskv3 loop LLM call failed", turn=turns, exc_info=True)
                outcome = LoopOutcome("loop_error", f"llm_call_failed: {type(exc).__name__}: {exc}")
                break
        if outcome is not None:
            break

        usage = _get(response, "usage") or {}
        turn_tokens = _get(usage, "total_tokens")
        if not turn_tokens:
            turn_tokens = (_get(usage, "prompt_tokens") or 0) + (_get(usage, "completion_tokens") or 0)
        total_tokens += int(turn_tokens or 0)
        if activity is not None:
            activity.last_turn_tokens = int(turn_tokens or 0)
            activity.tokens_remaining = None if max_tokens is None else max_tokens - total_tokens

        text = _extract_text(response)
        tool_calls = _extract_tool_calls(response)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {"id": tool_call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for tool_call_id, name, args in tool_calls
            ]
        messages.append(assistant_message)

        if not tool_calls:
            no_tool_call_turns += 1
            LOG.info("taskv3 loop turn produced no tool call", turn=turns)
            messages.append({"role": "user", "content": NO_TOOL_CALL_NUDGE})
            continue

        turn_did_action = False
        stall_nudges_due: list[tuple[str, int]] = []
        action_nudges_due: list[tuple[str, dict[str, Any], int]] = []
        round_actions: list[tuple[str, dict[str, Any], bool]] = []
        for idx, (tool_call_id, tool_name, args) in enumerate(tool_calls):
            # Enforce the cap per tool call so one batched turn cannot overrun it, and honor a
            # cancellation that arrives mid-batch before the next click/type/submit runs. Neither
            # this call nor the rest of the batch executes, so answer them as skipped.
            if total_tool_calls >= max_tool_calls:
                outcome = LoopOutcome("budget_exhausted", f"max_tool_calls ({max_tool_calls}) reached")
                _append_skipped_tool_results(messages, tool_calls[idx:], "tool-call budget reached")
                break
            if should_cancel is not None and await should_cancel():
                outcome = LoopOutcome("canceled", "run canceled")
                _append_skipped_tool_results(messages, tool_calls[idx:], "run canceled")
                break
            spec = tool_by_name.get(tool_name)
            # Once the action-step budget is spent, refuse a further page action — terminate, mirroring
            # the step engine's max-steps stop — but let perception/finish through, since the cap bounds
            # new action rounds, not the separate re-observe/finish turn the system prompt asks for.
            if spec is not None and spec.billable and max_action_steps is not None and action_steps >= max_action_steps:
                outcome = LoopOutcome("budget_exhausted", f"Reached the maximum steps ({max_action_steps})")
                _append_skipped_tool_results(messages, tool_calls[idx:], "action-step budget reached")
                break
            total_tool_calls += 1
            if activity is not None:
                # Refreshed per call, not per turn: a batched action+finish turn must not defer on
                # a stale turn-start snapshot (the conversion the headroom guard exists to prevent).
                activity.tool_calls_remaining = max_tool_calls - total_tool_calls
            # Submit-shaped actions (the failure-evidence predicate, minus captcha) are reported BEFORE
            # dispatch, since after it the page may be the confirmation page. A failure here never fails
            # the action, and the time is not billed to the tool.
            if (
                spec is not None
                and on_pre_action is not None
                and tool_name != "solve_captcha"
                and _arms_failure_evidence(tool_name, args, True)
            ):
                try:
                    await on_pre_action(tool_name, args)
                except Exception:
                    LOG.warning("taskv3 on_pre_action callback failed", tool=tool_name, exc_info=True)
            tool_started_at = time.monotonic()
            if spec is None:
                result = ToolResult.error(f"unknown_tool: {tool_name}")
            else:
                if spec.billable:
                    # A dispatched page action consumes a step even if it errors (it may mutate before
                    # failing); billing below counts successes only.
                    turn_did_action = True
                try:
                    result = await spec.handler(args)
                except Exception as exc:
                    LOG.warning("taskv3 tool handler raised", tool=tool_name, exc_info=True)
                    result = ToolResult.error(f"tool_error: {type(exc).__name__}: {exc}")
            tool_duration_seconds = time.monotonic() - tool_started_at
            tool_seconds += tool_duration_seconds
            # Observe's summary counters are the only trace a perception change leaves on this
            # record; its content is deliberately never logged. Gated on the tool, not the payload,
            # so every other tool's record keeps exactly today's fields.
            observe_summary = _observe_summary_fields(result) if tool_name == "observe" else {}
            # The action-loop guard's key and the perception ledger's digest, computed here (pure) so
            # their hashes ride the record below; the ledger itself is updated further down, unchanged.
            action_key = (tool_name, json.dumps(args, sort_keys=True, default=str))
            attribution: dict[str, Any] = {"action_key_hash": telemetry_hash(telemetry_salt, *action_key)}
            content_digest: str | None = None
            if spec is not None and spec.compactable and result.status == "ok":
                content_digest = hashlib.sha256(_canonical_perception_content(result.content).encode()).hexdigest()
                attribution["snapshot_digest"] = telemetry_hash(telemetry_salt, content_digest)
                attribution["probe_first_time"] = perception.first_time(action_key)
            # The only per-tool-call timing the engine has: tool execution is the majority of a v3
            # run's wall-clock and otherwise emits nothing at all. Names, sizes and booleans only —
            # argument values and result content carry end-user data and must not be logged.
            LOG.info(
                "taskv3 tool call finished",
                # A hallucinated name would otherwise put unbounded model output into an indexed
                # field on every call; the name itself stays in the tool result the model reads.
                tool=tool_name if spec is not None else "unknown_tool",
                tool_status=result.status,
                duration_seconds=tool_duration_seconds,
                result_chars=len(result.content),
                # Truthiness, not presence: the tools treat a null or empty selector as absent and
                # fall back to scanning the whole page, which is the case this field exists to find.
                selector_present=bool(args.get("selector")),
                billable=bool(spec is not None and spec.billable),
                turn=turns,
                batch_size=len(tool_calls),
                batch_index=idx,
                **observe_summary,
                **attribution,
            )

            if spec is not None and spec.compactable and result.status == "ok":
                snapshot_indices.add(len(messages))  # index this successful snapshot will occupy, pre-append
            model_facing_content = result.content
            skyvern_ctx = skyvern_context.current()
            if skyvern_ctx is not None:
                model_facing_content = skyvern_ctx.hide_from_model(model_facing_content)
            messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": model_facing_content}
            )
            result_data = result.data or {}
            if staged_downloads is not None and result_data.get("staged_download"):
                staged_downloads.add(result_data["staged_download"])
            if spec is not None and (result_data.get("download_notice") or result_data.get("page_state_changed")):
                # A download landing or a navigation is progress no matter which tool witnessed it
                # or whether that call itself errored: re-clicking the button that produces a file
                # (a "download next" flow), or re-trying after navigating to a fresh page, is a
                # healthy loop, not a stuck one.
                _clear_action_state()
            if content_digest is not None:
                snap = perception.record(action_key, content_digest)
                if snap.progressed:
                    # This probe saw the page change since it last looked — fresh evidence of
                    # progress, so repeat counts for actions taken against the old state are stale.
                    # A first-time probe has no baseline and proves nothing, which is what keeps
                    # varied-selector probing from laundering repetition into progress.
                    _clear_action_state()
                if activity is not None and stall_terminate_after is not None:
                    activity.perception_stall_imminent = perception.next_snapshot_can_trip(stall_terminate_after)
                if stall_terminate_after is not None and snap.live >= stall_terminate_after:
                    LOG.info(
                        "taskv3 loop perception stalled",
                        tool=tool_name,
                        identical_count=snap.live,
                        turn=turns,
                        **attribution,
                    )
                    outcome = LoopOutcome(
                        "terminated",
                        f"{PERCEPTION_STALL_REASON_PREFIX} {snap.live} consecutive identical snapshots from "
                        f"one {tool_name} probe — the page stopped changing in response to actions, so the goal cannot "
                        "progress (commonly a blocker the run cannot perceive or operate, e.g. inside a "
                        "cross-origin frame)",
                    )
                    _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "perception stalled")
                    break
                if (
                    stall_terminate_after is not None
                    and snap.tool_identical == stall_terminate_after
                    and not perception.suppressed_reported
                ):
                    perception.suppressed_reported = True
                    LOG.info(
                        PERCEPTION_STALL_SUPPRESSED_EVENT,
                        tool=tool_name,
                        identical_count=snap.tool_identical,
                        turn=turns,
                        **attribution,
                    )
                if (
                    stall_terminate_after is not None
                    and snap.probe_revisits >= stall_terminate_after
                    and not perception.shadow_reported
                ):
                    perception.shadow_reported = True
                    LOG.info(
                        PERCEPTION_STALL_SHADOW_EVENT,
                        snapshots=snap.probe_revisits,
                        tool=tool_name,
                        turn=turns,
                        **attribution,
                    )
                if stall_nudge_after is not None and snap.tool_identical == stall_nudge_after:
                    # Warn off the per-tool counter, not ``live``: it moves by one per read, so it
                    # crosses the threshold exactly once per streak and before any live verdict.
                    stall_nudges_due.append((tool_name, snap.tool_identical))
            if spec is not None and spec.billable:
                # Errored dispatches count too: a failed attempt consumed a step (see the action-step
                # accounting above) and a repeat-failing action is the same no-progress pathology.
                repeat_count, first_turn = action_counts.get(action_key, (0, turns))
                repeat_count += 1
                action_counts[action_key] = (repeat_count, first_turn)
                # Terminate only when the streak spans more than one turn AND its warning was
                # delivered: the system prompt commands batching identical clicks (steppers,
                # arrows), so a single-batch streak has had no chance to see feedback yet, and a
                # verdict must never arrive before the model saw the warning it could have acted on.
                if (
                    action_terminate_after is not None
                    and repeat_count >= action_terminate_after
                    and first_turn < turns
                    and (action_nudge_after is None or action_key in action_warned)
                ):
                    LOG.info(
                        "taskv3 loop action repeated",
                        tool=tool_name,
                        repeat_count=repeat_count,
                        turn=turns,
                        **attribution,
                    )
                    outcome = LoopOutcome(
                        "terminated",
                        f"{ACTION_LOOP_REASON_PREFIX} {repeat_count} repeated {tool_name} attempts on "
                        f"{_action_target(args)} with no observed page change between attempts — the same "
                        "action against an unchanged outcome (commonly re-submitting into the same "
                        "rejection banner) cannot progress the goal",
                    )
                    _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "action loop")
                    break
                if (
                    action_nudge_after is not None
                    and repeat_count >= action_nudge_after
                    and action_key not in action_warned
                ):
                    action_nudges_due.append((tool_name, args, repeat_count))
            if submit_watch is not None and tool_name == "navigate" and result.status == "ok":
                # Outside the billable/recordable branch on purpose: navigate is neither, so a clear
                # placed in there never runs. The run left the page; the control it clicked went too.
                submit_watch.clear()
            if spec is not None and (spec.billable or spec.recordable):
                # Dispatched page actions enter the round with their outcome: a failed billable round
                # still consumed budget and must persist (else later blocks undercount the run
                # budget); recordable tools persist for artifact parity without billing/budget.
                round_actions.append((tool_name, args, result.status == "ok"))
                if spec.billable and result.status == "ok":
                    billable_actions.append(tool_name)
                if activity is not None and _arms_failure_evidence(tool_name, args, result.status == "ok"):
                    activity.last_trigger_turn = turns
                if submit_watch is not None:
                    submit_selector = _names_submit_control(tool_name, args, result.status == "ok")
                    if submit_selector is not None:
                        submit_watch.record(submit_selector)

            if (
                completion_probe is not None
                and spec is not None
                and (spec.billable or result_data.get("download_notice"))
                # file_upload stages an http(s) source file into the same downloads dir; that landed
                # file is not the run's OWN download unless the wrapper also flagged download_notice.
                and not (result_data.get("staged_download") and not result_data.get("download_notice"))
            ):
                try:
                    completion_reason = await completion_probe(frozenset(staged_downloads or ()))
                except Exception:
                    LOG.warning("taskv3 completion_probe failed; not treating it as complete", exc_info=True)
                    completion_reason = None
                if completion_reason:
                    LOG.info("taskv3 loop completion probe fired", tool=tool_name, turn=turns)
                    outcome = LoopOutcome("completed", completion_reason)
                    _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "completion probe fired")
                    break

            if spec is not None and spec.terminal and result.status == "ok":
                data = result.data or {}
                outcome = LoopOutcome(
                    status=data.get("status", "completed"),
                    reason=data.get("reason", ""),
                    extracted_output=data.get("extracted_output"),
                )
                break

            if result.status == "error":
                # A failed call can leave the page in a state the rest of this batch was not planned
                # against (e.g. a write after a failed navigate). Stop and skip the remaining calls so
                # the model re-plans next turn from the error rather than acting on a stale assumption.
                _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "earlier tool call in this batch failed")
                break

        # Warn only after the batch completes: a user message may not sit between an assistant
        # turn's tool results, and the model reads it with the snapshot that tripped it.
        if outcome is None and stall_nudges_due:
            messages.append({"role": "user", "content": _stall_nudge_text(stall_nudges_due, set(tool_by_name))})
        if outcome is None and action_nudges_due:
            # Deliver only warnings whose streak survived the batch AND spans turns: a later call in
            # the same batch (an observe showing the page changed, a download) may have cleared it,
            # and a streak born entirely this turn has had no feedback yet — the message's "the
            # state you last observed is unchanged" would be false for it. An undelivered warning
            # stays unmarked, so it re-queues (and termination stays blocked) until the model has
            # actually seen it. Counts read live, not the threshold-crossing snapshot, and logged
            # here so the warn-then-recovered metric counts only warnings the model saw.
            still_stuck = []
            for name, warn_args, _count in action_nudges_due:
                key = (name, json.dumps(warn_args, sort_keys=True, default=str))
                entry = action_counts.get(key)
                if entry is not None and entry[1] < turns and key not in action_warned:
                    action_warned.add(key)
                    still_stuck.append((name, warn_args, entry[0]))
            if still_stuck:
                for name, _warn_args, count in still_stuck:
                    LOG.info("taskv3 loop action repeat nudged", tool=name, repeat_count=count, turn=turns)
                messages.append({"role": "user", "content": _action_nudge_text(still_stuck, set(tool_by_name))})
        # A "step" is one action round: a turn that ran >=1 page-mutating action. Perception-only
        # turns (observe/get_html) don't consume the caller's step budget — the step engine bundles
        # perception into each step, so counting v3's perception rounds against the same budget
        # under-counts equivalent work.
        if turn_did_action:
            action_steps += 1
        # Hand the round's executed actions to the caller so it can persist per-action artifacts
        # (screenshot, DB rows) — kept out of this transport-agnostic core, like should_cancel. A
        # persistence hiccup must not abort an otherwise-good run, so failures are contained here.
        if round_actions and on_action_round is not None:
            try:
                await on_action_round(round_actions)
            except Exception:
                LOG.warning("taskv3 on_action_round callback failed", turn=turns, exc_info=True)

    if outcome is None:
        outcome = LoopOutcome("loop_error", "loop exited without an outcome")

    outcome.turns = turns
    outcome.no_tool_call_turns = no_tool_call_turns
    outcome.tool_choice_in_effect = "tool_choice" in active_call_kwargs
    outcome.tool_calls = total_tool_calls
    outcome.tool_seconds = tool_seconds
    outcome.action_steps = action_steps
    outcome.billable_actions = billable_actions
    outcome.messages = messages
    return outcome
