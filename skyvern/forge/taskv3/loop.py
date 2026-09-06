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
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, TypeVar

import structlog

from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

LOG = structlog.get_logger()

ToolStatus = Literal["ok", "error"]
FinishStatus = Literal["completed", "failed", "terminated"]


@dataclass
class ToolResult:
    status: ToolStatus
    content: str
    data: dict[str, Any] | None = None
    # Transient images the loop must show the model on the NEXT call only (the on-demand `look`
    # tool's annotated screenshot). Threaded into one .call()'s ephemeral screenshots= arg and never
    # appended to the transcript, so it costs one image on one turn and is gone the turn after.
    screenshots: list[bytes] | None = None

    @classmethod
    def ok(cls, content: str, data: dict[str, Any] | None = None, screenshots: list[bytes] | None = None) -> ToolResult:
        return cls("ok", content, data, screenshots)

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
    # A user-defined error code the MODEL chose when finishing, drawn from the task's
    # error_code_mapping. Only ever set on a deliberate finish; None means the model was offered codes
    # and picked none, or was never offered any (the two are distinguished by error_codes_offered).
    error_code: str | None = None
    # Whether the finish tool exposed the customer's codes at all. Lets the caller tell "the model
    # declined to name a code" from "the model was never asked", so a post-hoc detector can stay out
    # of the way of a deliberate choice without also going silent on tasks that never had one.
    error_codes_offered: bool = False
    # The raw budget-cap string (e.g. "max_turns (40) reached") that granted this run its one final
    # observed turn, carried on whatever outcome the final turn produces — a finish verdict or, if
    # the model didn't finish, the honest budget_exhausted exit. None for a run that never tripped a cap.
    cap_trip: str | None = None
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
    # The progress signals for the caller's one terminal record, set on every path out of the loop.
    # The None default only covers an outcome built by someone else; the caller still writes its
    # record and simply carries no progress fields on it.
    telemetry: TerminalTelemetry | None = None


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


def _extract_reasoning_summary(response: Any) -> str | None:
    """The provider's reasoning summary (message.reasoning_content), when litellm's chat->responses
    bridge returned one -- surfaces the provider's reasoning summary where available; a tool call
    with empty message.content on a continuation turn often has none either."""
    message = _extract_message(response)
    if message is None:
        return None
    return _get(message, "reasoning_content")


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
# 8, not 6: RAW repeats of one action key peak at 6 across 50 completed prod runs while stuck runs
# reach 36, so 6 sat on the completed population's edge. DO NOT LOWER THIS. The effective
# post-clearing counter — what this constant is actually compared against — was then measured by
# replaying the clearing rule over per-call telemetry, and NO SEPARATION WAS OBSERVED: two completed
# runs peaked at 2 and 4, four stuck runs at 1, 3, 3 and below (upper bounds, computed identically
# on both sides), and the highest observed value fell in a COMPLETED run. n=6 is far too small to
# establish inversion as a property of either population — that spread is also consistent with
# noise — but it is no evidence FOR a lower threshold either, and lowering a safety threshold
# requires positive evidence. 8 is above everything observed, which is why the change is safe; it
# is also why nothing in the sample fires. REVISIT if the effective distribution ever becomes
# measurable at population scale; do not lower it before then. Repeat count may not be a stuck-ness
# signal at all: SKY-15602 tracks the real problem, which is that the loop has no definition of
# goal progress, only of page change.
ACTION_LOOP_TERMINATE_AFTER = 8

# Facetable sibling of PERCEPTION_STALL_REASON_PREFIX; same dashboard contract.
ACTION_LOOP_REASON_PREFIX = "action_loop:"

# Progress-gated action-step budget extension (SKY-15264): a run that hits its action-step cap while
# the page is still demonstrably changing (a repeated probe returned fresh content, a navigation or
# download landed) earns an extension of half the original cap — the observed failure population is
# genuinely long multi-page forms dying mid-progress, while a stalled run must be refused exactly as
# before. Evidence must be at most this many action rounds old: staler change evidence says nothing
# about the run's current state.
ACTION_BUDGET_EXTENSION_EVIDENCE_WINDOW = 8
# A long form does not stop being long because one extension already fired: 41.4% of long-block
# step-cap deaths die at the ALREADY-EXTENDED cap (SKY-15666). So the grant repeats under the same
# evidence predicate, bounded two ways. Each grant is half the ORIGINAL cap, never half the current
# one: a run that has already spent 1.5x its budget must not draw a proportionally larger handout,
# and constant-size grants keep every extension priced the same and re-earned on fresh evidence.
# Total growth stops at this multiple of the original cap. The wall clock — the one cap that is not
# a function of the action-step budget — is the outer runaway stop and is never re-derived.
ACTION_BUDGET_EXTENSION_MAX_FACTOR = 3
# Facetable event names — both the grant and the refusal are queryable so the gate's decision
# precision is measurable on the canary; change only with the dashboards that read them.
ACTION_BUDGET_EXTENDED_EVENT = "taskv3 loop action budget extended"
ACTION_BUDGET_EXTENSION_REFUSED_EVENT = "taskv3 loop action budget extension refused"
# A wrap-up turn granted by a guard that a later budget extension raised past its trip; facetable
# so a released latch is distinguishable from one that never fired.
FINAL_TURN_RELEASED_EVENT = "taskv3 loop final turn grant released by budget extension"

# Final-turn grant (budget-exhaustion final turn): a budget cap trip buys one more unconstrained
# model turn instead of ending the run immediately, so a model mid-extraction can still call finish
# with what it has. NOT once per run since SKY-15666: an extension that relieves the latching guard
# releases the latch and re-arms it for a later trip, so this fires up to one more time than the run
# had grants. Facetable — change only with the dashboards that read it.
FINAL_TURN_GRANTED_EVENT = "taskv3 loop final turn granted"

# Page-state stall policy (SKY-15265): rounds of billable batches that left the page fingerprint
# byte-identical, with no page-change flag, mean the run is cycling on a frozen document in shapes
# the per-tool guards cannot see (varied probes never streak; scroll/wait carry no digest at all).
# Completions of the worst-affected canary block finish in <=9 rounds; the nudge re-plans the model
# once before the verdict.
PAGE_STATE_STALL_NUDGE_AFTER = 8
PAGE_STATE_STALL_TERMINATE_AFTER = 12
# The verdict is SHADOW-ONLY for now: the fingerprint is blind to work inside iframes (main-frame
# innerHTML only), so a live termination could kill healthy embedded-widget runs. The nudge ships
# live (benign direction); the shadow event measures the would-terminate precision on the canary,
# and promotion to a live verdict is a separate release decision on that data.
PAGE_STATE_STALL_SHADOW_EVENT = "taskv3 loop page state stall would terminate"
# Facetable sibling of PERCEPTION_STALL_REASON_PREFIX; reserved for the future live verdict.
PAGE_STATE_STALL_REASON_PREFIX = "page_state_stall:"

# Hard "the resource does not exist / is gone" HTTP statuses. A navigation landing on one of these is
# a genuine non-capability dead-end (a dead or removed posting), which v1 routes to `terminated`. Both
# the in-loop `navigate` tool and the pre-loop initial-URL navigation classify against this set. NARROW
# on purpose: auth (401/403), rate-limit (429) and transient server errors (5xx) are recoverable or
# capability failures, not dead-ends, and are left to the model / stay `failed`.
NAVIGATION_DEAD_END_STATUSES = frozenset({404, 410})

# Defined here (not tools.py) so the batch-dispatch poisoning check below can compare against it
# without an import cycle -- tools.py already imports ToolResult/ToolSpec from this module.
PAGE_UNAVAILABLE_ERROR = "browser page unavailable"

# A navigation landed on a hard dead-end (HTTP 404/410): the target posting does not exist or was
# removed, so the goal cannot be completed there. Ends the run as `terminated`, matching v1's terminate
# verdict for the same condition. Covers both the in-loop `navigate` tool and the pre-loop initial-URL
# navigation. Facetable sibling of the prefixes above.
NAV_DEAD_END_REASON_PREFIX = "navigation_dead_end:"
# A page-level handler kept asking for the page to be reloaded past the per-run cap: the page cannot
# be stabilized, and acting on it would mean acting on a page declared stale.
PAGE_REFRESH_EXHAUSTED_REASON_PREFIX = "page_refresh_exhausted:"

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
# An opaque-id alias attribute (tools._mask_aliases) is a handle of the same kind.
_TV3_MARKER_VALUE_RE = re.compile(r'data-tv3="t\d+(?:-\d+)?"|data-tv3-ref="(?:\d+|\?)"')

# get_html truncates to a fixed budget before the loop ever sees the content, so a marker the cut
# leaves open at the tail has no closing quote for the pattern above and its churning digits would
# be the one leak that survives canonicalization. The lookahead assumes the truncation notice itself
# carries no quote character, and this sub must run AFTER closed markers are rewritten to the
# quote-bearing placeholder — either broken silently brings the leak back.
_TV3_MARKER_CUT_RE = re.compile(r'data-tv3="t\d*(?:-\d*)?(?=[^"]*\Z)|data-tv3-ref="[\d?]*(?=[^"]*\Z)')


_PERCEPTION_URL_LINE_RE = re.compile(r"^url=\S+", flags=re.MULTILINE)


def _canonical_perception_content(content: str) -> str:
    closed = _TV3_MARKER_VALUE_RE.sub(lambda m: m.group(0).partition("=")[0] + '="*"', content)
    return _TV3_MARKER_CUT_RE.sub(lambda m: m.group(0).partition("=")[0] + '="*', closed)


def _content_only_perception(content: str) -> str:
    # The URL is a hint, not content: history.pushState moves it without changing the document. The
    # full canonicalization (URL included) keeps clearing the repeat guards — a wizard whose pages
    # differ only by URL must survive — but budget-extension evidence hashes THIS, so a URL flip
    # alone can never earn budget.
    return _PERCEPTION_URL_LINE_RE.sub("url=*", _canonical_perception_content(content))


# How many recent states a probe remembers. This length IS the longest oscillation period that can
# be recognised, so it is a detection limit and not a memory tuning knob.
PERCEPTION_RING = 8

# Run-scoped revisit memory (SKY-14998). The ring above is a DETECTION LIMIT: a run that cycles with
# a period longer than it returns to states the ring has already evicted, so every ring-bounded guard
# is structurally blind to the loop rather than judging it wrongly. Measured production case: a
# macro-cycle of period ~44 billable touches, against a ring of 8 and a canonical window of 10.
# This remembers perception states for the WHOLE run instead, which is what makes a long cycle
# visible at all. It is a memory, not a verdict: LOG-ONLY, nothing reads it for control flow.
PERCEPTION_REVISIT_EVENT = "taskv3 perception state revisited"
# Two returns to a state is a panel toggling; the third is the earliest a cycle is worth reporting.
PERCEPTION_REVISIT_LOG_AFTER = 3
# Bounded storage: one 64-char digest per DISTINCT perception state, so the worst case is ~32KB of digests per
# run. At the cap the memory stops admitting NEW states and keeps counting the ones it already
# holds — it degrades to a smaller memory rather than growing without bound, and every emission says
# whether it was capped so a truncated run is never read as a complete one.
PERCEPTION_REVISIT_CAP = 512


@dataclass
class _RevisitMemory:
    """How many times each perception state has been seen this run, unbounded in time and bounded in
    size. Distinct from _ProbeStreak's ring, which only remembers the last PERCEPTION_RING states and
    therefore cannot see a cycle longer than that."""

    cap: int = PERCEPTION_REVISIT_CAP
    # digest -> (times seen, novel-state count at its last sighting)
    _seen: dict[str, tuple[int, int]] = field(default_factory=dict)
    capped: bool = False
    distinct_states: int = 0
    peak_revisits: int = 0
    # Revisits that had fresh ground covered since the last sighting, so they were not reported.
    # Kept as a count so the healthy population stays measurable in aggregate without a line each.
    progressed_revisits: int = 0
    _novel_states: int = 0

    def record(self, digest: str) -> tuple[int, int]:
        """Returns (times this state has now been seen, distinct NEW states seen since it was last
        seen). Zero new states between two sightings is a replay; a drill-down that opens a fresh
        page between returns to its list is not, and that difference is what makes the signal
        filterable rather than firing on healthy and stuck runs alike.

        Novelty is measured against the WHOLE run, not the probe's ring: the ring is bounded at
        PERCEPTION_RING, so a cycle longer than it reads as fresh content there — the exact
        blindness this memory exists to lift, and reusing that derivation would reintroduce it.
        """
        entry = self._seen.get(digest)
        if entry is None:
            if len(self._seen) >= self.cap:
                # Refused, not evicted: dropping a known state to admit a new one would reset a
                # streak that is the whole point of the memory.
                self.capped = True
                return 0, 0
            self._novel_states += 1
            self._seen[digest] = (1, self._novel_states)
            self.distinct_states = len(self._seen)
            return 1, 0
        seen, novel_at_last_sighting = entry
        seen += 1
        new_states_since = self._novel_states - novel_at_last_sighting
        self._seen[digest] = (seen, self._novel_states)
        self.peak_revisits = max(self.peak_revisits, seen)
        if new_states_since:
            self.progressed_revisits += 1
        return seen, new_states_since


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
        self.content_only: dict[tuple[str, str], deque[str]] = {}
        self.shadow_reported = False
        self.suppressed_reported = False
        # Run-level, deliberately NOT cleared by reset(): the worst revisit streak the run ever
        # reached, and how many reads could have produced one. A first look at a probe key cannot
        # revisit anything, so it is not a chance — counting it would make a run of one-shot probes
        # read as "looked and found no looping".
        self.peak_probe_revisits = 0
        self.revisit_chances = 0

    def reset(self) -> None:
        """Forget every streak: the document they described is gone (a reload)."""
        self._probes.clear()
        self._tools.clear()
        self.content_only.clear()

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
        self.revisit_chances += 1
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
        self.peak_probe_revisits = max(self.peak_probe_revisits, probe.revisits)
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


# Net-progress ledger (SKY-15020 Lever C). The repetition guards above ask whether the page or
# action REPEATED; this asks whether the run made NET PROGRESS. Real stuck-ness is often VARIED
# actions with zero net progress (SKY-14998: 21 input-timeouts on different selectors — varied
# actions, varied perception — so no repetition guard trips and the run oscillates to the cap). The
# ledger is SHADOW-ONLY and ADDITIVE: it emits a "would fail-fast" event but terminates nothing and
# leaves the three guards untouched. Adding live terminations is a release-posture change gated on
# this event's own decision precision plus operator sign-off.
PROGRESS_LEDGER_WINDOW = 8
# Facetable event names; the offline precision/survival metrics key on these — change only with the
# dashboards that read them.
# Canonical progress signal, Phase A (SKY-15379): ONE target-churn + compound-progress tracker,
# LOG-ONLY. The loop answers "is this run progressing?" five separate ways today; this tracker is
# the candidate replacement signal, emitting the parity/precision data any consolidation needs
# while changing no behavior. Its target key is the canonicalized SELECTOR, not tool+args — the
# incumbent action-loop key provably missed prod repeat-loops whose args varied on a fixed target.
CANONICAL_LOOP_EVENT = "taskv3 canonical progress loop detected"
CANONICAL_EXTEND_DELTA_EVENT = "taskv3 canonical progress extension delta"
# Log-only thresholds; the Phase-B intervention thresholds are tuned FROM this data, so the fire
# points are logged at every rung of the distribution rather than one cliff.
CANONICAL_LOOP_WINDOW = 10
CANONICAL_LOOP_FIRE_COUNTS = (3, 4, 6, 8)
# The rung that counts as "looping" for the extension-delta read — named so reordering the logging
# rungs above can never silently move the threshold Phase B tunes against.
CANONICAL_LOOP_TOUCHES = 4

PROGRESS_LEDGER_SHADOW_EVENT = "taskv3 loop progress ledger would fail-fast"


@dataclass
class _ProgressLedger:
    """Billable actions since the last net-progress signal, plus its per-run peak.

    The already-computed distance-to-done metric is the observe summary's ``invalid_fields`` count.
    Net progress is any of: a navigation or download landing (hard progress), the count reaching a
    NEW LOW (a real form advance), or the count RISING since the last look (a fresh page's required
    fields, or a submit that surfaced new errors — either way the context changed, so the prior
    no-progress streak is stale). Novelty is deliberately NOT progress: varied thrash produces novel
    perception, which is why the streak is counted against the invalid-field trend, not the page
    digest.

    Two invariants keep the over-termination direction safe:
    - The verdict is taken on an OBSERVE that CONFIRMS no progress, never on the raw action count: a
      run that batches several fixes before re-observing (markers stay valid until the page
      re-renders) has not yet shown a stalled look, so the streak withholds rather than fires.
    - ``form_armed`` reflects the CURRENT look, not a sticky earlier one, so a form-less page (a
      confirmation/extraction page, or a solved form) can never be judged stuck here.

    The design is intentionally biased toward PRECISION over recall — a shadow verdict headed for a
    future live terminator must not fire on a progressing run. A real page-transition signal (the click
    tool's ``page_transitioned``, from its already-computed url_before/url_after) closes ONE precision
    gap the rise heuristic left: a click that moves the URL re-baselines cleanly as hard progress, so a
    fresh page whose ``invalid_fields`` count coincidentally equals the prior page's no longer reads as a
    stalled look. Only the POSITIVE direction is used — a URL change proves a transition — because URL
    equality does NOT prove same-page (a URL-stable SPA multi-step form advances without moving the URL),
    so the ledger never suppresses a re-baseline on an unchanged URL. That leaves known false-NEGATIVES
    (the SAFE direction) the shadow numbers under-count: a run whose ``invalid_fields`` OSCILLATES or
    CREEPS still re-baselines on every up-swing (the rise heuristic cannot separate same-page thrash from
    a real advance without a stronger same-page oracle — the Lever C recall follow-up), and a form
    silently rejected while showing zero invalid fields never arms.

    Reads only what the loop already computed — no extra LLM turn, no re-observe.
    """

    window: int = PROGRESS_LEDGER_WINDOW
    actions_since_progress: int = 0
    peak_actions_since_progress: int = 0
    invalid_baseline: int | None = None  # floor for the current page/context; rebased on rise or new low
    last_invalid: int | None = None
    form_armed: bool = False
    ever_armed: bool = False  # a form was seen at least once — the runs the survival record is emitted for
    shadow_reported: bool = False

    def observe(self, invalid_fields: int) -> bool:
        """Record an observation; return True the first time one CONFIRMS window-length no-progress."""
        prev = self.last_invalid
        self.last_invalid = invalid_fields
        # Reflects THIS look only: a page with no invalid fields must not be judged stuck here.
        self.form_armed = invalid_fields > 0
        self.ever_armed = self.ever_armed or self.form_armed
        if self.invalid_baseline is None:
            self.invalid_baseline = invalid_fields
            return False
        if invalid_fields < self.invalid_baseline:
            # A new low: real net progress on the form. Reset the streak and re-baseline.
            self.actions_since_progress = 0
            self.invalid_baseline = invalid_fields
            return False
        if prev is not None and invalid_fields > prev:
            # The count rose since the last look: a new page's fresh required fields, or a submit that
            # surfaced new errors. Re-baseline to the new floor instead of measuring it against a
            # stale, lower one. A real click-driven transition instead arrives as hard_progress()
            # upstream (baseline cleared); this rise path is the fallback for same-page count changes
            # and for transitions no tool witnessed.
            self.actions_since_progress = 0
            self.invalid_baseline = invalid_fields
            return False
        # Flat, with no new low and no rise: this look confirms no net progress since the last one.
        if self.actions_since_progress >= self.window and self.form_armed and not self.shadow_reported:
            self.shadow_reported = True
            return True
        return False

    def hard_progress(self) -> None:
        # Navigation or a download landing: unambiguous progress. The old page's distance metric is
        # stale, so drop the baseline; the next observe re-arms from the new page.
        self.actions_since_progress = 0
        self.form_armed = False
        self.invalid_baseline = None

    def on_billable(self) -> None:
        """Count one billable action toward the no-progress streak (the verdict is taken on observe)."""
        self.actions_since_progress += 1
        self.peak_actions_since_progress = max(self.peak_actions_since_progress, self.actions_since_progress)


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


def _is_enter_submit(tool_name: str, args: dict[str, Any]) -> bool:
    """An Enter keypress or a type that pressed Enter: the submit shapes with no selector to key on."""
    if tool_name == "press_key":
        raw = str(args.get("key", ""))
        # Space on a focused button activates it exactly like a click; a literal " " would strip to "".
        if raw == " ":
            return True
        # Playwright accepts Modifier+Key chords, and Control/Meta+Enter is a real submit.
        key = raw.strip().lower().rsplit("+", 1)[-1]
        return key in ("enter", "return", "numpadenter", "space")
    if tool_name == "type":
        return bool(args.get("press_enter"))
    return False


def _call_selector(args: dict[str, Any]) -> str | None:
    selector = args.get("selector")
    if isinstance(selector, str) and selector:
        return selector
    mark = args.get("mark")
    return f"mark={mark}" if isinstance(mark, (int, str)) else None


_PAGE_PROBE_TIMEOUT_SECONDS = 10.0

_T = TypeVar("_T")


async def _bounded_probe(probe: Awaitable[_T], timeout: float | None = None) -> _T:
    """Every awaited page.evaluate probe goes through here: the loop's deadline and cancellation checks
    run BETWEEN awaits, so a hung renderer can only be interrupted by a bound on the await itself.
    A timeout raises (asyncio.TimeoutError) and each call site's raise handling decides what a
    missing reading means there. ``timeout`` defaults to the module-level cap read at CALL time (not
    baked in as a parameter default) so tests can still monkeypatch _PAGE_PROBE_TIMEOUT_SECONDS."""
    return await asyncio.wait_for(probe, timeout=_PAGE_PROBE_TIMEOUT_SECONDS if timeout is None else timeout)


async def _sample_probe(probe: Callable[[], Awaitable[str | None]], deadline_at: float | None = None) -> str | None:
    """A raising or hung probe is as uninformative as a None one: all mean "no reading," not "unchanged."
    Generic over what it samples -- the page_probe (document identity) and page_fingerprint (rendered
    content) callables share this exact shape. ``deadline_at`` caps the wait to whatever's left of the
    loop's own deadline (never longer than the default), so a hung probe cannot outlive it; a deadline
    already passed skips the probe call entirely and reads as a missing sample, same as a timeout."""
    timeout = _PAGE_PROBE_TIMEOUT_SECONDS
    if deadline_at is not None:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return None
        timeout = min(timeout, remaining)
    try:
        return await _bounded_probe(probe(), timeout=timeout)
    except Exception:
        return None


def _may_submit(tool_name: str, args: dict[str, Any]) -> bool:
    """A click, an Enter press, or a type that pressed Enter: the loop cannot tell a submit from any of them."""
    return tool_name == "click" or _is_enter_submit(tool_name, args)


def _is_finish(tool_name: str) -> bool:
    return tool_name == "finish"


def _arms_failure_evidence(tool_name: str, args: dict[str, Any], ok: bool) -> bool:
    """solve_captcha arms on ANY dispatch — its "not solved" error is exactly the verdict the async
    protocol can contradict. Other actions arm only when they reached the page AND in their
    submit-shaped form."""
    if tool_name == "solve_captcha":
        return True
    if not ok:
        return False
    return _may_submit(tool_name, args)


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
    # True from the moment the loop's granted final turn starts running. A hold asks for a retry
    # turn that no longer exists — honoring it would silently convert the model's verdict into
    # budget_exhausted, the exact conversion the hold headroom gates exist to prevent.
    final_turn_active: bool = False

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
        if activity.final_turn_active:
            return False
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


def _cap_trip_relieved(
    cap_trip: str | None,
    max_tokens: int | None,
    total_tokens: int,
    final_turn_token_reserve: int,
    max_turns: int,
    turns: int,
    max_tool_calls: int,
    total_tool_calls: int,
) -> bool:
    """Whether the guard that produced `cap_trip` has since been raised past its trip.

    Only the three guards a budget extension re-derives can be relieved. Mirrors the top-of-turn
    checks exactly, reserve included, so a released latch cannot re-trip on the very next turn.

    The two exclusions differ in kind. The wall clock is never re-derived, and its branch is
    unreachable in practice besides: the gate refuses every grant once the clock is blown
    ("insufficient_deadline_headroom"), so a deadline latch and a grant cannot coexist in one run.
    The action-step trip is excluded on purpose and IS reachable — a refusal can latch the wrap-up
    turn and the next turn's refreshed evidence can then earn a grant, which the spent-grant exit
    discards. That wastes an earned extension and is worth revisiting, but releasing on it is a
    behaviour change beyond raising the guards and is deliberately not made here."""
    if cap_trip is None:
        return False
    if cap_trip.startswith("max_tokens"):
        return max_tokens is not None and total_tokens < max(0, max_tokens - final_turn_token_reserve)
    if cap_trip.startswith("max_turns"):
        return turns < max_turns
    if cap_trip.startswith("max_tool_calls"):
        return total_tool_calls < max_tool_calls
    return False


def _budget_extension_gate(
    action_steps: int,
    last_change_evidence_step: int | None,
    action_warned: set[tuple[str, str]],
    progress_stalled: bool,
    activity: ActivityRecency | None,
    deadline_at: float | None,
    extension: int,
    seconds_per_step: float | None = None,
    headroom_gain: tuple[int, int, int] = (0, 0, 0),
) -> tuple[bool, str]:
    """Whether an exhausted action-step budget may be extended, and the deciding reason.

    Progress is the property, not its absence-of-stall proxy: the gate requires POSITIVE recent
    evidence the page changed (a repeated probe returning fresh content, a navigation or download —
    the same events that clear the action-retry ledger), then vetoes on any live stall signal, and
    finally requires the turn/token/deadline headroom to actually fund the extension — granting
    budget the runaway guards would immediately revoke converts an honest exhaustion into a worse
    one. `headroom_gain` is the extra (turns, tool calls, tokens) that granting would itself create
    by re-deriving the runaway backstops from the extended cap, and is added to what the run has
    left: the question is whether the extension is fundable AFTER the grant, so judging it on
    pre-grant headroom would refuse extensions the grant pays for.

    Which of the three checks stay live depends on that gain. Under the standard sizing policy the
    turn and tool-call gains exceed anything one extension can consume, so those two become
    satisfied by construction — correctly, since a guard re-derived from the new cap provably
    cannot revoke it — and they still bind for a caller passing no `backstops_for_cap` (the
    default) or its own guards. TOKENS are the live discriminator: the gain per step is that
    guard's own per-step allowance, so a run burning more than that per turn — the re-read-the-page
    spiral the backstop exists for — still fails, and so does one whose gain is zero because the
    guard has clamped at its ceiling.

    The checks are a funding FLOOR sized off the run's own observed burn (turns per step so far,
    last turn's tokens), not a guarantee the extension completes — the facetable grant/refusal
    events measure that on the canary."""
    extra_turns, extra_tool_calls, extra_tokens = headroom_gain
    if (
        last_change_evidence_step is None
        or action_steps - last_change_evidence_step > ACTION_BUDGET_EXTENSION_EVIDENCE_WINDOW
    ):
        return False, "no_recent_page_change_evidence"
    if action_warned:
        return False, "warned_action_retry_streak"
    if progress_stalled:
        return False, "no_net_progress_window"
    if activity is not None and activity.turns_remaining is not None:
        turns_remaining = activity.turns_remaining + extra_turns
        # Fractional comparison (cross-multiplied): floor division would read 1.9 observed
        # turns-per-step as 1 and fund an extension the remaining turns cannot run.
        if turns_remaining < extension or turns_remaining * max(action_steps, 1) < extension * activity.turn:
            return False, "insufficient_turn_headroom"
    if activity is not None and activity.tool_calls_remaining is not None:
        # +1 reserves the terminal finish call: funding only the actions trades one budget
        # exhaustion for another at the very last call.
        if activity.tool_calls_remaining + extra_tool_calls < extension + 1:
            return False, "insufficient_tool_call_headroom"
    if (
        activity is not None
        and activity.tokens_remaining is not None
        and activity.tokens_remaining + extra_tokens < extension * max(activity.last_turn_tokens, 1)
    ):
        return False, "insufficient_token_headroom"
    if activity is not None and activity.perception_stall_imminent:
        return False, "perception_stall_imminent"
    if deadline_at is not None:
        # Fund the extension in wall-clock at the run's own observed pace, never below the flat
        # minimum the deferral gates use.
        required = FAILURE_EVIDENCE_MIN_DEADLINE_HEADROOM_SECONDS
        if seconds_per_step is not None:
            required = max(required, extension * seconds_per_step)
        if deadline_at - time.monotonic() < required:
            return False, "insufficient_deadline_headroom"
    return True, "recent_page_change_evidence"


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


@dataclass
class SemanticCommitStats:
    """Tier-1 semantic commit reads and how many accepted. One instance per run, shared by every
    tool call, so a per-run accept rate is a division and not a join."""

    opportunities: int = 0
    accepts: int = 0

    def log_fields(self) -> dict[str, int]:
        return {"semantic_commit_opportunities": self.opportunities, "semantic_commit_accepts": self.accepts}


@dataclass
class LedgerTerminalFields:
    """The ledger's end-of-run numbers, present only for a run that ever armed a form."""

    peak_actions_since_progress: int
    actions_since_progress: int
    # The CURRENT look, cleared by progress — not the partition. See `TerminalTelemetry.form_ever_armed`.
    form_armed: bool
    would_fire: bool

    def log_fields(self) -> dict[str, int | bool]:
        return {
            "peak_actions_since_progress": self.peak_actions_since_progress,
            "actions_since_progress": self.actions_since_progress,
            "form_armed": self.form_armed,
            "would_fire": self.would_fire,
        }


@dataclass
class TerminalTelemetry:
    """The run's progress signals, assembled where their state lives and emitted by the caller on the
    one terminal record it already writes per run. Every optional member is OMITTED rather than zeroed
    when its tier could not have fired, because a 0 would read as "fired nothing" instead of "never
    ran" and would drag an offline rate's denominator."""

    # Sticky, and the partition. `form_armed` on the same record is the current look and answers a
    # different question, so filter populations on this one.
    form_ever_armed: bool
    survival: dict[str, int]
    ledger: LedgerTerminalFields | None = None
    peak_page_state_stall_rounds: int | None = None
    peak_probe_revisits: int | None = None
    semantic_commit: SemanticCommitStats | None = None

    def log_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"form_ever_armed": self.form_ever_armed, **self.survival}
        if self.ledger is not None:
            fields.update(self.ledger.log_fields())
        if self.peak_page_state_stall_rounds is not None:
            fields["peak_page_state_stall_rounds"] = self.peak_page_state_stall_rounds
        if self.peak_probe_revisits is not None:
            fields["peak_probe_revisits"] = self.peak_probe_revisits
        if self.semantic_commit is not None:
            fields.update(self.semantic_commit.log_fields())
        return fields


def _unblocker_options(available_tools: set[str]) -> list[str]:
    options = []
    if "solve_captcha" in available_tools:
        options.append("if the page may be waiting on a verification widget, call solve_captcha")
    if "get_html" in available_tools:
        options.append("take ONE targeted get_html look at the region that should be changing")
    if "look" in available_tools:
        options.append("if you can't tell what's on the page or why an action isn't taking, call look to see it")
    options.append("if the goal is already met, call finish(status=completed)")
    options.append("if genuinely blocked, call finish(status=terminated) naming the blocker as the reason")
    return options


def _page_state_nudge_text(rounds: int) -> str:
    return (
        f"Your last {rounds} action rounds left the page's rendered content completely unchanged — "
        "whatever you are trying is not affecting this page. Stop repeating the current approach: "
        "re-plan from a fresh observe, try a genuinely different control or path, or finish honestly "
        "(status=failed or terminated) naming what is blocking you."
    )


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


def _reload_failed_nudge_text() -> str:
    return (
        "A page-level handler asked for the page to be reloaded, but the reload failed, so the page may be "
        "stale or unresponsive. Re-observe before acting, and do not re-submit a form unless the fresh page "
        "shows it was not already submitted."
    )


def _refresh_nudge_text() -> str:
    """A page-level handler reloaded the page outside the model's own tool calls, so nothing else
    in the transcript tells it the last observation is now stale."""
    return (
        "The page was refreshed by a page-level handler after the last tool call. Its state may have "
        "changed: re-observe before acting, and do not re-submit a form until the fresh page shows it "
        "was not already submitted."
    )


def _budget_extended_observation(cap: str, recency: ActivityRecency | None) -> str:
    """The retraction of a `_budget_exhausted_observation` whose cap has since been raised.

    APPENDED, never popped: `snapshot_indices` stores absolute message indices, so deleting the
    stale message would silently re-anchor compaction onto the wrong ones. Without this the model
    keeps reading "this is the final turn" for the rest of the run and wraps up early — which spends
    the extension the release exists to preserve, through the prompt instead of through a counter."""
    payload = {
        "budget_exhausted": False,
        "cap": cap,
        "headroom": {
            "tool_calls": recency.tool_calls_remaining if recency is not None else None,
            "tokens": recency.tokens_remaining if recency is not None else None,
        },
    }
    json_line = json.dumps(payload, separators=(",", ":"))
    return (
        f"{json_line}\nThat budget was extended: the earlier final-turn notice no longer applies and "
        "the run continues. Keep working toward the goal."
    )


def _budget_exhausted_observation(cap: str, recency: ActivityRecency | None) -> str:
    """A typed, model-facing fact (not an instruction) that this is the run's last granted turn.
    Deliberately silent on what to do next -- finishing with a partial result, retrying, or reporting
    failure are all legitimate depending on what the model has."""
    payload = {
        "budget_exhausted": True,
        "cap": cap,
        "headroom": {
            "tool_calls": recency.tool_calls_remaining if recency is not None else None,
            "tokens": recency.tokens_remaining if recency is not None else None,
        },
    }
    json_line = json.dumps(payload, separators=(",", ":"))
    return f"{json_line}\nThe run's budget cap has tripped; this is the final turn before the run ends."


def _budget_exhausted_reason(cap_trip: str) -> str:
    """Human sentence for a no-finish budget exit. Never includes the raw cap literal (that lives
    only in `cap_trip` and the logs) so a status/reason readout doesn't leak an internal counter."""
    if "deadline" in cap_trip:
        axis = "time"
    elif "max_tokens" in cap_trip:
        axis = "token"
    elif "max_turns" in cap_trip:
        axis = "turn"
    elif "max_tool_calls" in cap_trip:
        axis = "tool-call"
    elif "maximum steps" in cap_trip:
        axis = "step"
    else:
        axis = "run"
    return f"The run reached its {axis} budget before the model finished; the recorded output may be partial."


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


class _ProgressEvidence(str, Enum):
    """Why the canonical ring was cleared. Every clear names the positive evidence class that
    justified it — an "assume changed" fallback (a missing sample read charitably) may never clear."""

    REFRESH_RELOAD = "refresh_reload"
    FRESH_DOWNLOAD_OR_NAVIGATION = "fresh_download_or_navigation"
    PAGE_TRANSITIONED = "page_transitioned"
    INVALID_FIELDS_BASELINE_MOVE = "invalid_fields_baseline_move"
    PERCEPTION_DIGEST = "perception_digest"
    CROSS_BATCH_MOVEMENT = "cross_batch_movement"
    PROBE_MISMATCH = "probe_mismatch"
    PAGE_STATE_VERDICT = "page_state_verdict"
    TERMINAL_BATCH_FINGERPRINT = "terminal_batch_fingerprint"


@dataclass
class _CanonicalProgressTracker:
    """Touches on canonicalized targets since the last compound-progress event (SKY-15379, Phase A).

    A touch is one billable tool dispatch, keyed by its selector (tool name when selector-less).
    Progress — any of the ledger's hard-progress events, an invalid-fields new-low, or a confirmed
    page change — clears the ring, so a repeat streak can only accumulate across a genuinely
    unchanged situation; a progressing run is untrippable by construction. Log-only: the counts
    feed telemetry, nothing reads them for control flow.
    """

    window: int = CANONICAL_LOOP_WINDOW
    _touches: list[tuple[str, bool]] = field(default_factory=list)
    # Bumped on every clear: a pending loop event minted under an older generation was completed
    # by (or followed by) absorbed progress and must not be emitted.
    gen: int = 0
    # Rungs already fired this generation. Once the window saturates, same-target counts can PARK
    # on a fire rung while churn evicts other keys — a rung is one threshold crossing, not one
    # event per touch spent at it.
    _fired: set[tuple[str, int]] = field(default_factory=set)
    # Run-level high-water marks and per-evidence clear tallies, deliberately NOT cleared by
    # progress(): the survival record needs the worst churn the run ever reached and every clear it
    # ever made, the same way the ledger keeps peak_actions_since_progress across its own clears.
    # Clearing these with the ring would report the last streak, not the peak.
    peak_same_touches: int = 0
    peak_same_errors: int = 0
    _evidence_counts: dict[str, int] = field(default_factory=dict)

    def record_touch(self, target_key: str, is_error: bool) -> tuple[int, int]:
        """Record one billable touch; returns (same-target touches, same-target errors) in-window."""
        self._touches.append((target_key, is_error))
        if len(self._touches) > self.window:
            self._touches.pop(0)
        same = [err for key, err in self._touches if key == target_key]
        # A rung re-arms once the in-window count dips below it: a fresh streak after full eviction
        # is a genuine re-crossing, distinct from a saturated window PARKED on a fire count.
        self._fired = {entry for entry in self._fired if entry[0] != target_key or entry[1] <= len(same)}
        same_errors = sum(1 for err in same if err)
        self.peak_same_touches = max(self.peak_same_touches, len(same))
        self.peak_same_errors = max(self.peak_same_errors, same_errors)
        return len(same), same_errors

    def progress(self, evidence: _ProgressEvidence) -> None:
        """The one clear choke point: a caller must name its evidence class, so no site can wipe
        the ring on a local, untyped notion of progress."""
        self._touches.clear()
        self._fired.clear()
        self.gen += 1
        self._evidence_counts[evidence.value] = self._evidence_counts.get(evidence.value, 0) + 1
        LOG.debug("taskv3 canonical progress clear", evidence=evidence.value)

    def claim_rung(self, target_key: str, count: int) -> bool:
        """A rung is one threshold crossing per generation: the first claim wins, and a repeat
        claim while a saturated window PARKS on a fire count is refused."""
        if (target_key, count) in self._fired:
            return False
        self._fired.add((target_key, count))
        return True

    def invalidate_marks(self) -> None:
        """A look renumbered the manifest, so every mark=N key now names an arbitrary element: drop
        them rather than let four different controls alias into one false streak. A surviving mark
        streak is therefore always within a single manifest generation. Selector keys stay — their
        identity outlives the renumbering."""
        self._touches = [touch for touch in self._touches if not touch[0].startswith("mark=")]
        self._fired = {entry for entry in self._fired if not entry[0].startswith("mark=")}

    def survival_fields(self) -> dict[str, int]:
        """The per-run survival numbers, read through the class for the same reason progress() is
        the one write choke point: the ring's state has no references outside this body. Every
        evidence class carries an explicit 0, so a class that never fired stays distinguishable
        from one this record forgot to emit."""
        return {
            "peak_same_touches": self.peak_same_touches,
            "peak_same_errors": self.peak_same_errors,
            "looping_targets": self.looping_targets(),
            **{f"clear_{member.value}": self._evidence_counts.get(member.value, 0) for member in _ProgressEvidence},
        }

    def looping_targets(self) -> int:
        counts: dict[str, list[bool]] = {}
        for key, err in self._touches:
            counts.setdefault(key, []).append(err)
        return sum(
            1
            for errs in counts.values()
            if len(errs) >= CANONICAL_LOOP_TOUCHES and sum(errs) >= CANONICAL_LOOP_TOUCHES - 1
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
    error_code_mapping: dict[str, str] | None = None,
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

    async def _bounded_fingerprint() -> str | None:
        """page_fingerprint(), timed out against whatever is left of the run's deadline instead of
        the flat default cap -- so a HANGING sampler can no longer run the full default timeout once
        the deadline is nearly gone. Mirrors _sample_probe's own convention once the deadline has
        fully elapsed: the call is skipped entirely (never even invoked, so a hanging implementation
        is never awaited) and reads as a missing sample, same as a genuinely absent page. That is
        deliberately NOT the same as an exception -- a real sampling error must still propagate and
        defer (see _settled/_quiesced's docstrings), and a fast probe that would have answered
        instantly must not be denied the chance just because the deadline's clock already read zero;
        only a positive-but-reduced timeout, not an outright skip, can preserve that."""
        assert page_fingerprint is not None  # gated by each call site's own None check
        timeout = _PAGE_PROBE_TIMEOUT_SECONDS
        if deadline_at is not None:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                return None
            timeout = min(timeout, remaining)
        return await _bounded_probe(page_fingerprint(), timeout=timeout)

    async def _quiesced() -> bool:
        """Bounded wait for the page to stop mutating before the failure verdict's evidence turn.
        Returns False when there is no page to observe (a deferral would burn a turn for nothing)."""
        prev = await _bounded_fingerprint()
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
            current = await _bounded_fingerprint()
            if current is None:
                return False
            if current == prev:
                return True
            prev = current

    async def _settled() -> bool:
        first = await _bounded_fingerprint()
        if first is None:
            return True  # no page to sample (non-recovering peek): accept the verdict as-is
        wait = settle_wait_seconds
        if deadline_at is not None:
            wait = min(wait, deadline_at - time.monotonic())
        if wait > 0:
            await asyncio.sleep(wait)
        if should_cancel is not None and await should_cancel():
            return False  # defer: the loop's cancellation check ends the run before another turn
        return first == await _bounded_fingerprint()

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
                marker = await _bounded_probe(pending_marker(submit_watch.selector))
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
        if (
            status == "completed"
            and page_fingerprint is not None
            and deferrals < max_settle_deferrals
            # On the granted final turn the re-verification turn a deferral asks for no longer
            # exists: holding would convert the verdict into budget_exhausted instead.
            and not (activity is not None and activity.final_turn_active)
        ):
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
            # Same rule as the completed-side holds: no retry turn exists on the granted final turn.
            and not activity.final_turn_active
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
        raw_code = args.get("error_code")
        # isinstance first: a model can answer with a non-string (a list, a number), and `in` against
        # a dict would raise TypeError on an unhashable one and take the whole finish down.
        chosen_code = raw_code if isinstance(raw_code, str) and raw_code in (error_code_mapping or {}) else None
        if error_code_mapping:
            # The A/B and the ramp read need to split "was a code offered" from "did the model take
            # one", and a dropped invented code must not vanish silently -- filter_to_user_defined_codes
            # returns its dropped set for exactly this reason and every other caller logs it.
            LOG.info(
                "taskv3 finish user-defined error code",
                status=status,
                chosen_error_code=chosen_code,
                declined=chosen_code is None and raw_code is None,
                dropped_error_code=raw_code if chosen_code is None and raw_code is not None else None,
            )
        return ToolResult.ok(
            content="Task attempt ended. No further actions are permitted.",
            data={
                "status": status,
                "reason": args.get("reason") or "",
                "extracted_output": args.get("extracted_output"),
                "error_code": chosen_code,
                "error_codes_offered": bool(error_code_mapping),
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
                # Offered ONLY when the task configured codes, and enum-bound to those keys: a task
                # without a mapping keeps today's exact finish schema, and a model with a mapping
                # cannot invent a code the customer never defined.
                **(
                    {
                        "error_code": {
                            "type": "string",
                            "enum": sorted(error_code_mapping),
                            "description": (
                                "Set when one of these descriptions is what actually happened, on "
                                "whatever finish status is honest — choose the status on its own "
                                "merits, never to make a code fit. Never use a code to describe a "
                                "failure of you or the browser: being stuck, losing track of which "
                                "page you are on, running out of steps, or simply not managing the "
                                "task are reported without a code. A problem with the SITE may take "
                                "a code when the user defined one whose description names that "
                                "problem."
                            ),
                        }
                    }
                    if error_code_mapping
                    else {}
                ),
            },
            "required": ["status", "reason"],
        },
        handler=handler,
        terminal=True,
    )


_COMPACTED_PREFIX = "[superseded "


def _compact_transcript(
    messages: list[dict[str, Any]],
    snapshot_indices: set[int],
) -> None:
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
        cls = messages[i]["name"]
        if i > last_assistant_idx or cls not in seen:
            seen.add(cls)  # the still-unread latest round, or the newest snapshot of this class — keep
            continue
        messages[i]["content"] = f"{_COMPACTED_PREFIX}{cls} output elided to bound context]"
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
    max_action_steps_ceiling: int | None = None,
    prompt_name: str = "taskv3-agent-loop",
    organization_id: str | None = None,
    call_kwargs: dict[str, Any] | None = None,
    should_cancel: Callable[[], Awaitable[bool]] | None = None,
    on_action_round: Callable[[list[tuple[str, dict[str, Any], bool]], str | None], Awaitable[None]] | None = None,
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
    progress_window: int | None = PROGRESS_LEDGER_WINDOW,
    activity: ActivityRecency | None = None,
    submit_watch: SubmitWatch | None = None,
    telemetry_salt: str | None = None,
    completion_probe: CompletionProbe | None = None,
    staged_downloads: set[str] | None = None,
    initial_navigation_status: int | None = None,
    page_probe: Callable[[], Awaitable[str | None]] | None = None,
    reload_page: Callable[[], Awaitable[None]] | None = None,
    max_refresh_cycles: int = 3,
    page_fingerprint: Callable[[], Awaitable[str | None]] | None = None,
    # One model call's worth of token headroom, reserved so the granted final turn (below) is
    # actually fundable rather than being immediately re-tripped by the same max_tokens check it
    # was granted under. 0 (the default) reproduces today's unreserved check.
    final_turn_token_reserve: int = 0,
    # The caller's policy for sizing the runaway guards off an action-step budget — the same
    # function that produced the max_turns/max_tool_calls/max_tokens above. Supplied so a granted
    # budget extension can RE-DERIVE them from the extended cap instead of running on the leftover
    # headroom of the pre-extension one; without it a bigger action budget merely converts a
    # step-cap death into a token-cap death. None keeps the guards fixed for the whole run.
    backstops_for_cap: Callable[[int], tuple[int, int, int]] | None = None,
    semantic_commit_stats: SemanticCommitStats | None = None,
) -> LoopOutcome:
    tool_by_name = {tool.name: tool for tool in tools}
    outcome: LoopOutcome | None = None
    pending_nav_dead_end: int | None = None
    stall_nudges_due: list[tuple[str, int]] = []
    budget_extended_notice: str | None = None
    # Page-state stall detector (SKY-15265): consecutive billable rounds on a byte-identical
    # fingerprint, whether the one re-plan nudge went out, and whether one is due this turn.
    trailing_page_state_stall_rounds = 0
    peak_page_state_stall_rounds = 0
    # Whether the detector ever reached a verdict. A wired sampler is not a taken sample: it is only
    # read behind a billable call, so a run that lands none never judges the page at all.
    page_state_ever_judged = False
    page_state_nudge_delivered = False
    page_state_nudge_due = False
    # The last fingerprint sample from the PREVIOUS batch: a delayed render can land between one
    # batch's after-sample and the next batch's before-sample, so movement is checked across
    # batches, not only within them.
    page_state_prev_fp: str | None = None
    refresh_cycles = 0
    refresh_nudge_due = False
    reload_failed_nudge_due = False
    pending_screenshots: list[bytes] = []
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
    # Net-progress ledger (additive shadow); None disables it, mirroring the guard's *_after knobs.
    progress = _ProgressLedger(window=progress_window) if progress_window is not None else None
    canonical = _CanonicalProgressTracker()
    revisit_memory = _RevisitMemory()
    # The action-loop counter: (repeat count, first turn of the streak) per billable action
    # identity, cleared whenever evidence of page change arrives. action_warned holds the streaks
    # whose warning was actually DELIVERED — termination is gated on it, so the model always gets
    # the warning (and a chance to self-correct) at least one turn before the verdict.
    action_counts: dict[tuple[str, str], tuple[int, int]] = {}
    action_warned: set[tuple[str, str]] = set()
    # Progress-gated budget extension (SKY-15264, SKY-15666): the action round of the latest
    # positive page-change evidence, how many extensions have been granted, and the cap they are
    # all sized and bounded against — captured before the first grant so growth stays linear.
    last_change_evidence_step: int | None = None
    budget_extensions_granted = 0
    original_action_steps = max_action_steps
    token_clamp_reported = False
    # Final-turn grant (budget-exhaustion final turn): mirrors budget_extension_granted's shape. A
    # budget-cap trip anywhere in the loop sets this and buys one more unconstrained model turn. One
    # grant per latch, but an extension that relieves the latching guard releases it and re-arms the
    # latch for a later trip; cap_trip_pending remembers which cap granted it, so a finish() outcome
    # produced on that turn can carry the fact forward even though finish is a different code path.
    # final_turn_started distinguishes "granted, turn not run yet" from "granted turn already ran": a
    # mid-batch grant (max_tool_calls, the action-step gate) leaves the SAME counter tripped for the
    # very next top-of-turn check, one code site removed from the grant with no turn boundary between
    # them -- without this flag that check would end the run before the granted turn ever started.
    final_turn_granted = False
    final_turn_started = False
    cap_trip_pending: str | None = None
    # Extraction carried by a finish the granted turn staged but never got honored (skipped behind a
    # failed or refused call, or refused by a fail-closed blocker). The verdict itself stays
    # unhonored — its premise didn't hold — but the data rides the spent-grant exit.
    final_turn_staged_output: Any = None

    def _clear_action_state() -> None:
        action_counts.clear()
        action_warned.clear()

    def _note_page_change_evidence() -> None:
        nonlocal last_change_evidence_step
        last_change_evidence_step = action_steps

    async def _consume_refresh_signal(
        ctx: SkyvernContext, tool_name: str, remaining: list[Any], round_actions: list[Any], *, drop: bool
    ) -> bool:
        """Clear the page-refresh signal and, unless dropped or past the cap, reload and void `remaining`."""
        nonlocal refresh_cycles, refresh_nudge_due, reload_failed_nudge_due, pending_screenshots, outcome
        nonlocal pending_nav_dead_end, stall_nudges_due, last_change_evidence_step
        nonlocal trailing_page_state_stall_rounds, page_state_nudge_delivered, page_state_prev_fp
        ctx.refresh_working_page = False
        refresh_cycles += 1
        if drop:
            LOG.info("taskv3 loop refresh signal dropped", tool=tool_name, turn=turns)
            return False
        if refresh_cycles > max_refresh_cycles:
            # The queued calls were chosen on a page declared stale, so they are voided rather than
            # run; a page that keeps demanding a reload cannot be stabilized, and the run ends there.
            LOG.warning("taskv3 loop refresh signal past cap", tool=tool_name, turn=turns)
            _append_skipped_tool_results(messages, remaining, "the page could not be stabilized")
            outcome = LoopOutcome(
                "terminated",
                f"{PAGE_REFRESH_EXHAUSTED_REASON_PREFIX} a page-level handler requested a page reload "
                f"{refresh_cycles} times — the page cannot be stabilized, so the goal cannot progress on it",
            )
            return True
        if reload_page is not None:
            reload_record = ("reload_page", {"reason": "a page-level handler requested a refresh"})
            try:
                await reload_page()
            except Exception:
                # The page did not change, so nothing is re-baselined; the queued calls are still
                # voided (they were chosen on a page declared stale), the signal is re-armed for
                # another attempt (bounded by the cap), and the model is told the reload failed.
                LOG.warning("taskv3 loop page reload failed after refresh signal", tool=tool_name, exc_info=True)
                round_actions.append((*reload_record, False))
                ctx.refresh_working_page = True
                _append_skipped_tool_results(messages, remaining, "a page reload was requested but failed")
                reload_failed_nudge_due = True
                return True
            round_actions.append((*reload_record, True))
        LOG.info("taskv3 loop honored page refresh signal", tool=tool_name, turn=turns)
        # The reloaded document is a new baseline for every ledger that described the old one, and a
        # look taken before it would hand the model marks that no longer exist. That includes the
        # budget-extension evidence stamp: pre-reload progress says nothing about the fresh document,
        # so the run must re-demonstrate progress before it can earn an extension.
        _clear_action_state()
        last_change_evidence_step = None
        trailing_page_state_stall_rounds = 0
        page_state_nudge_delivered = False
        page_state_prev_fp = None
        perception.reset()
        if activity is not None:
            activity.perception_stall_imminent = False
        pending_screenshots = []
        pending_nav_dead_end = None
        stall_nudges_due = []
        if progress is not None:
            progress.hard_progress()
        canonical.progress(_ProgressEvidence.REFRESH_RELOAD)
        if submit_watch is not None:
            submit_watch.clear()
        _append_skipped_tool_results(messages, remaining, "the page was refreshed")
        refresh_nudge_due = True
        return True

    def _progress_observe_shadow(
        observe_summary: dict[str, int], tool_name: str, attribution: dict[str, Any], baseline_before: int | None
    ) -> None:
        """Feeds a model-issued no-arg observe's invalid-fields count into the net-progress ledger."""
        if progress is None or not observe_summary:
            return
        invalid_fields = observe_summary.get("invalid_fields")
        # The ledger's True return is the SHADOW STALL verdict, not progress — the canonical clear
        # keys on the ledger re-baselining (a new low, or a rise re-baseline: fresh context), which
        # is visible as its baseline moving under an already-set baseline. The baseline is captured
        # BEFORE _absorb_result_data runs: a replayed download notice hard-progresses the shadow
        # ledger (nulling the baseline) on the same result whose summary carries the new low, and
        # reading the post-absorb value would leave this clear dead for the rest of the page.
        stalled = invalid_fields is not None and progress.observe(invalid_fields)
        if baseline_before is not None and progress.invalid_baseline != baseline_before:
            canonical.progress(_ProgressEvidence.INVALID_FIELDS_BASELINE_MOVE)
        if stalled:
            LOG.info(
                PROGRESS_LEDGER_SHADOW_EVENT,
                actions=progress.actions_since_progress,
                invalid_fields=progress.last_invalid,
                form_armed=progress.form_armed,
                tool=tool_name,
                turn=turns,
                **attribution,
            )

    def _absorb_result_data(tool_name: str, spec: ToolSpec | None, result_data: dict[str, Any]) -> bool:
        """Absorbs a download/page-change signal in a tool's result.data (staged_downloads,
        action-state clear, progress hard_progress). Returns whether marks were renumbered."""
        if staged_downloads is not None and result_data.get("staged_download"):
            staged_downloads.add(result_data["staged_download"])
        if spec is not None and (result_data.get("download_notice") or result_data.get("page_state_changed")):
            # A download landing or a navigation is progress no matter which tool witnessed it or
            # whether that call itself errored: re-clicking the button that produces a file (a
            # "download next" flow), or re-trying after navigating to a fresh page, is a healthy
            # loop, not a stuck one. A same-URL reload is the exception: it resets the retry ledger
            # like any reload but is a state WIPE, not progress — it clears the extension evidence
            # exactly like the refresh-signal path.
            _clear_action_state()
            if result_data.get("same_url_reload") or result_data.get("nav_revisit"):
                # A reload destroys the observed document and a revisit replaces it with a fresh
                # instance of known territory: re-baseline the perception ledger (as the refresh
                # path does) so the first post-navigation look cannot diff against a pre-navigation
                # digest and read as progress — and clear the evidence stamp in both cases, since
                # navigation is non-billable and a surviving stamp would stay maximally recent
                # through any amount of oscillation.
                perception.reset()
                nonlocal last_change_evidence_step
                last_change_evidence_step = None
                if activity is not None:
                    activity.perception_stall_imminent = False
            elif result_data.get("download_new") or result_data.get("page_state_changed"):
                # Only a download detected on THIS call is evidence — a compactable tool replaying
                # a retained notice re-clears the retry ledger but earns no budget.
                _note_page_change_evidence()
                # The old document's perception streak cannot speak for the fresh page: clear the
                # imminent flag exactly as the refresh path does.
                if activity is not None:
                    activity.perception_stall_imminent = False
            if progress is not None:
                progress.hard_progress()
            if (
                result_data.get("download_new")
                or result_data.get("page_state_changed")
                or result_data.get("same_url_reload")
                or result_data.get("nav_revisit")
            ):
                # A REPLAYED notice (download_notice without download_new) re-clears the retry
                # ledger but is not fresh progress: it must not keep wiping the canonical ring, or
                # a post-download loop could never accumulate enough touches to emit telemetry.
                canonical.progress(_ProgressEvidence.FRESH_DOWNLOAD_OR_NAVIGATION)
        # A click that moved the URL is a real page transition (H1 hard progress) for the shadow
        # ledger, but URL equality does NOT prove same-page (a URL-stable SPA form advance) — so only
        # the positive direction is acted on, and kept OUT of the branch above so it never clears the
        # action-loop guard's state; this signal is shadow-only and additive. It is a URL-only HINT
        # (history.pushState moves the URL without changing the document), so it never stamps
        # budget-extension evidence either — the content-confirmed signals are that bar.
        if result_data.get("page_transitioned") is True:
            if progress is not None:
                progress.hard_progress()
            canonical.progress(_ProgressEvidence.PAGE_TRANSITIONED)
        return tool_name == "look" and bool(result_data.get("marks_renumbered"))

    async def _completion_probe_outcome(
        tool_name: str, spec: ToolSpec | None, result_data: dict[str, Any]
    ) -> LoopOutcome | None:
        """Consults the completion probe after a billable or download-signaling tool result."""
        if not (
            completion_probe is not None
            and spec is not None
            and (spec.billable or result_data.get("download_notice"))
            # file_upload stages an http(s) source file into the same downloads dir; that landed
            # file is not the run's OWN download unless the wrapper also flagged download_notice.
            and not (result_data.get("staged_download") and not result_data.get("download_notice"))
        ):
            return None
        try:
            completion_reason = await completion_probe(frozenset(staged_downloads or ()))
        except Exception:
            LOG.warning("taskv3 completion_probe failed; not treating it as complete", exc_info=True)
            return None
        if not completion_reason:
            return None
        LOG.info("taskv3 loop completion probe fired", tool=tool_name, turn=turns)
        # No tracker-wide clear here: the probe firing is download progress for the COMPLETING
        # touch only, and its call site drops that one target's pending rungs — a whole-generation
        # bump would erase a sibling target's true-positive rung along with it.
        return LoopOutcome("completed", completion_reason)

    def _perception_stall_check(
        ledger: _PerceptionLedger,
        content_digest: str,
        action_key: tuple[str, str],
        tool_name: str,
        attribution: dict[str, Any],
        *,
        content_only_digest: str | None = None,
        refresh_pending: bool = False,
    ) -> tuple[LoopOutcome | None, list[tuple[str, int]]]:
        """Trips the nudge/terminate thresholds off ``ledger``'s own streak."""
        stall_nudges: list[tuple[str, int]] = []
        snap = ledger.record(action_key, content_digest)
        if snap.progressed:
            # This probe saw the page change since it last looked — fresh evidence of progress, so
            # repeat counts for actions taken against the old state are stale. A first-time probe has
            # no baseline and proves nothing, which is what keeps varied-selector probing from
            # laundering repetition into progress. A multi-page wizard that clicks the same selector
            # (e.g. "next") on every page relies on THIS clear to survive — page_transitioned alone
            # deliberately does not clear the action-loop guard (see below), so only a progressed
            # snapshot does.
            ring = ledger.content_only.get(action_key) if content_only_digest is not None else None
            # INVARIANT: this test and the budget-evidence test below must both refuse a return to
            # the ring. They answer one question — did the run reach ground it has not already
            # covered? — and relaxing either alone silently reopens SKY-14998: a page that CYCLES
            # moves on every probe, so `progressed` holds every round, and an unconditional clear
            # let the action DRIVING the oscillation reset its own counter forever (one production
            # key ran 11 times against a threshold of 6 while the nudge fired once).
            returned_to_known_ground = ring is not None and content_only_digest in ring
            if not returned_to_known_ground:
                _clear_action_state()
            # Two landed digests that differ are positive evidence, exactly like a fingerprint
            # mismatch — and the only movement evidence there is when page_fingerprint is absent.
            canonical.progress(_ProgressEvidence.PERCEPTION_DIGEST)
            # Evidence requires NEW content: a URL-only flip (history.pushState) still clears the
            # repeat guards above but earns no budget, and neither does a return to a content state
            # in the probe's recent ring (a panel toggling open and shut).
            if ring and content_only_digest not in ring:
                _note_page_change_evidence()
        if content_only_digest is not None:
            ring = ledger.content_only.get(action_key)
            if ring is None:
                ring = ledger.content_only.setdefault(action_key, deque(maxlen=PERCEPTION_RING))
            ring.append(content_only_digest)
        if activity is not None and stall_terminate_after is not None:
            activity.perception_stall_imminent = ledger.next_snapshot_can_trip(stall_terminate_after)
        # A refresh about to be honored re-baselines this ledger anyway, so a stall verdict raised on
        # the stale page it is replacing would be wrong the instant the reload lands.
        if stall_terminate_after is not None and snap.live >= stall_terminate_after and not refresh_pending:
            LOG.info(
                "taskv3 loop perception stalled",
                tool=tool_name,
                identical_count=snap.live,
                turn=turns,
                **attribution,
            )
            return (
                LoopOutcome(
                    "terminated",
                    f"{PERCEPTION_STALL_REASON_PREFIX} {snap.live} consecutive identical snapshots from "
                    f"one {tool_name} probe — the page stopped changing in response to actions, so the goal "
                    "cannot progress (commonly a blocker the run cannot perceive or operate, e.g. inside a "
                    "cross-origin frame)",
                ),
                stall_nudges,
            )
        if (
            stall_terminate_after is not None
            and snap.tool_identical == stall_terminate_after
            and not ledger.suppressed_reported
        ):
            ledger.suppressed_reported = True
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
            and not ledger.shadow_reported
        ):
            ledger.shadow_reported = True
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
            stall_nudges.append((tool_name, snap.tool_identical))
        return None, stall_nudges

    # Mutable for the run: a provider that rejects tool_choice rejects it every turn, so a drop
    # made once must stick.
    active_call_kwargs = dict(call_kwargs or {})

    def _degrade_tool_choice(exc: BaseException) -> bool:
        """Drop the optional call parameters (tool_choice, the reasoning-summary dict) and report
        whether the turn is worth re-issuing.

        Called only when the turn is otherwise about to end the run, so the cost is one extra call
        on a run that was already failing. A context-window overflow is excluded because dropping a
        parameter provably cannot fix it. Dropping reasoning_effort reverts to the config's own
        value, so a provider that rejects the dict form cannot end the run on turn 1.
        """
        if isinstance(exc, SkyvernContextWindowExceededError):
            return False
        # One parameter per degrade, least-proven first: a provider that rejects only the summary
        # dict keeps its independently supported tool_choice; a second rejection drops that too.
        for key in ("reasoning_effort", "tool_choice"):
            if active_call_kwargs.pop(key, None) is not None:
                LOG.warning("taskv3 loop retrying without optional call param", dropped=key, turn=turns, exc_info=True)
                return True
        return False

    turns = 0
    no_tool_call_turns = 0
    total_tool_calls = 0
    tool_seconds = 0.0
    total_tokens = 0
    billable_actions: list[str] = []
    action_steps = 0
    # Images produced by an on-demand `look` this turn, to show the model on the NEXT call only. Passed
    # as the transient screenshots= arg once, then cleared, so a look costs one image on one turn and
    # never enters `messages` (the transcript re-seeds message_history each turn, so it's structurally
    # gone the turn after).
    started_at = time.monotonic()
    deadline_at = started_at + deadline_seconds if deadline_seconds is not None else None

    # The task's starting URL is navigated during browser setup, before this loop runs, so a dead/removed
    # starting posting never routes through the in-loop `navigate` tool — the model just observes the dead
    # page and finishes (defaulting to failed). Classify that pre-loop navigation here so the dominant
    # dead-posting case ends `terminated`, matching v1, without waiting on the model's finish discretion.
    # Cancellation is checked first, exactly as the first loop turn would: a run canceled during setup must
    # persist as `canceled` (and stay unbilled), not be pre-empted into `terminated` by this fast path.
    if outcome is None and initial_navigation_status in NAVIGATION_DEAD_END_STATUSES:
        if should_cancel is not None and await should_cancel():
            outcome = LoopOutcome("canceled", "run canceled")
        else:
            LOG.info("taskv3 loop initial navigation dead end", http_status=initial_navigation_status)
            outcome = LoopOutcome(
                "terminated",
                f"{NAV_DEAD_END_REASON_PREFIX} the task's starting URL returned HTTP {initial_navigation_status} "
                "— the target no longer exists or has been removed, so the goal cannot be completed there",
            )

    while outcome is None:
        if should_cancel is not None and await should_cancel():
            outcome = LoopOutcome("canceled", "run canceled")
            break
        # The final-turn grant (see final_turn_granted above): a budget trip here buys one more
        # turn instead of ending the run, so the checks below are elif'd
        # (only the first tripped cap matters this iteration). A spent grant ends the run BEFORE any
        # counter is consulted: the granted turn may have been granted at a gate no top-of-turn
        # check re-reads (the action-step gate), so waiting for a counter to re-trip would let a
        # finish-less granted turn keep looping on other budgets — and the cap that granted the
        # turn, not whichever counter happens to re-trip first, is the honest fact to report.
        # final_turn_started (set below, right before the turn actually runs) distinguishes a spent
        # grant from a mid-batch grant whose turn has not run yet. Cancellation above is exempt: it
        # is not a budget cap.
        if final_turn_granted and final_turn_started:
            outcome = LoopOutcome(
                "budget_exhausted",
                _budget_exhausted_reason(cap_trip_pending or "budget"),
                cap_trip=cap_trip_pending,
                extracted_output=final_turn_staged_output,
            )
            break
        # Once the grant fired no top-of-turn cap can trip again, so the checks only run pre-grant.
        top_of_turn_trip: str | None = None
        if not final_turn_granted:
            if deadline_seconds is not None and time.monotonic() - started_at > deadline_seconds:
                top_of_turn_trip = f"deadline ({deadline_seconds:.0f}s) reached"
            elif max_tokens is not None and total_tokens >= max(0, max_tokens - final_turn_token_reserve):
                top_of_turn_trip = f"max_tokens ({max_tokens}) reached"
            elif turns >= max_turns:
                top_of_turn_trip = f"max_turns ({max_turns}) reached"
            elif total_tool_calls >= max_tool_calls:
                top_of_turn_trip = f"max_tool_calls ({max_tool_calls}) reached"
        if top_of_turn_trip is not None:
            final_turn_granted = True
            cap_trip_pending = top_of_turn_trip
            LOG.info(
                FINAL_TURN_GRANTED_EVENT,
                cap=top_of_turn_trip,
                turn=turns,
                tool_calls_remaining=None if activity is None else activity.tool_calls_remaining,
                tokens_remaining=None if activity is None else activity.tokens_remaining,
            )
            messages.append({"role": "user", "content": _budget_exhausted_observation(top_of_turn_trip, activity)})
        if final_turn_granted:
            final_turn_started = True
            if activity is not None:
                # Read by the finish tool's hold gates: a hold's retry turn no longer exists.
                activity.final_turn_active = True
        turns += 1
        if activity is not None:
            activity.turn = turns
            activity.turns_remaining = max_turns - turns
            activity.tool_calls_remaining = max_tool_calls - total_tool_calls

        # Elide superseded perception results before re-sending the transcript, so a perception-heavy
        # run can't balloon the context to the token backstop (the pre-compaction runaway mode).
        _compact_transcript(messages, snapshot_indices)
        llm_caller.message_history = list(messages)
        # Consume any pending look image into THIS call only, then clear: the image rides one request
        # and is never appended to `messages`, so the turn after carries zero image blocks.
        screenshots_for_call = pending_screenshots or None
        pending_screenshots = []
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
                    screenshots=screenshots_for_call,
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
        reasoning_summary = _extract_reasoning_summary(response)
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
        stall_nudges_due = []
        refresh_nudge_due = False
        budget_extended_notice = None
        reload_failed_nudge_due = False
        action_nudges_due: list[tuple[str, dict[str, Any], int]] = []
        round_actions: list[tuple[str, dict[str, Any], bool]] = []
        # A hard 404/410 from an in-loop navigate, applied only AFTER the batch so a same-turn fallback
        # navigate can clear it — the model is told to batch aggressively, and terminating on the first
        # of a batched [navigate(dead), navigate(live)] would discard the recovery it planned.
        pending_nav_dead_end = None
        # A same-selector dependent of a failed page-action call is skipped; any later click, Enter-shaped
        # submit, or finish in the batch is skipped too -- the loop cannot tell a submit from the first two,
        # and a verdict written before the failure was seen may be wrong or mis-reasoned.
        failed_selectors: set[str] = set()
        batch_had_failure = False
        marks_stale = False
        # Loop events minted this batch, emitted only after every progress signal the batch can
        # produce has been absorbed (see the end-of-batch emission below).
        pending_canonical_fires: list[dict[str, Any]] = []
        batch_page_change_reason: str | None = None
        batch_fp_before: str | None = None
        # Sample only when the batch can actually land a billable action -- the end-of-batch check
        # below gates on turn_did_action, so a finish-only or perception-only batch has no use for
        # this baseline and shouldn't pay its round-trip.
        batch_has_billable_call = any(
            tool_by_name.get(tool_name) is not None and tool_by_name[tool_name].billable
            for _, tool_name, _ in tool_calls
        )
        # A cancellation that already landed makes this batch's baseline dead work: the per-call
        # check below (before the first dispatch) ends the batch before anything it'd inform runs.
        batch_will_sample_baseline = batch_has_billable_call and page_fingerprint is not None
        batch_cancelled = batch_will_sample_baseline and should_cancel is not None and await should_cancel()
        # The page-state stall detector (SKY-15265) reads the before/after fingerprint pair for
        # every billable batch.
        if page_fingerprint is not None and batch_has_billable_call and not batch_cancelled:
            batch_fp_before = await _sample_probe(page_fingerprint, deadline_at=deadline_at)
            if page_state_prev_fp is not None and batch_fp_before is not None and batch_fp_before != page_state_prev_fp:
                # The page moved BETWEEN batches (a delayed render landing after the prior
                # after-sample): the touches the old samples described are stale, and this batch's
                # dispatch and extension decisions must not read them. Canonical-only — the
                # incumbent stall counters keep their end-of-batch turn_did_action gate.
                canonical.progress(_ProgressEvidence.CROSS_BATCH_MOVEMENT)
        batch_fp_after: str | None = None
        for idx, (tool_call_id, tool_name, args) in enumerate(tool_calls):
            # Enforce the cap per tool call so one batched turn cannot overrun it, and honor a
            # cancellation that arrives mid-batch before the next click/type/submit runs. Neither
            # this call nor the rest of the batch executes, so answer them as skipped.
            # Gated on `not final_turn_granted`: once the final turn is granted this same counter is
            # already at (or past) the cap by construction, so re-enforcing it here would block the
            # granted turn's very first call (including finish) before it ever ran. The top-of-turn
            # check is what actually ends the run if this granted turn doesn't finish either.
            if not final_turn_granted and total_tool_calls >= max_tool_calls:
                mid_batch_trip = f"max_tool_calls ({max_tool_calls}) reached"
                final_turn_granted = True
                cap_trip_pending = mid_batch_trip
                LOG.info(
                    FINAL_TURN_GRANTED_EVENT,
                    cap=mid_batch_trip,
                    turn=turns,
                    tool_calls_remaining=None if activity is None else activity.tool_calls_remaining,
                    tokens_remaining=None if activity is None else activity.tokens_remaining,
                )
                _append_skipped_tool_results(messages, tool_calls[idx:], "tool-call budget reached")
                messages.append({"role": "user", "content": _budget_exhausted_observation(mid_batch_trip, activity)})
                break
            if should_cancel is not None and await should_cancel():
                outcome = LoopOutcome("canceled", "run canceled")
                _append_skipped_tool_results(messages, tool_calls[idx:], "run canceled")
                break
            spec = tool_by_name.get(tool_name)
            call_selector = _call_selector(args)
            if marks_stale and call_selector is not None and call_selector.startswith("mark="):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": (
                            "skipped: an earlier look in this batch renumbered the marks, so this mark was chosen "
                            "from an old screenshot; pick it again from the new one"
                        ),
                    }
                )
                continue
            if call_selector is not None and call_selector in failed_selectors:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": f"skipped: depends on an earlier call in this batch on {call_selector} that failed",
                    }
                )
                continue
            if batch_had_failure and _is_finish(tool_name):
                # Any verdict queued behind the failure was written before the model saw it: a completed
                # one may be false, and a failed/terminated one carries a reason that predates the error.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": (
                            "skipped: a field in this batch failed before this verdict was reached; "
                            "re-observe, then finish with a status that reflects the failure"
                        ),
                    }
                )
                continue
            if batch_had_failure and _may_submit(tool_name, args):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": (
                            "skipped: a field in this batch failed and this call may submit the form; "
                            "fix the field, then re-queue it"
                        ),
                    }
                )
                continue
            # Once the action-step budget is spent, refuse a further page action — terminate, mirroring
            # the step engine's max-steps stop — but let perception/finish through, since the cap bounds
            # new action rounds, not the separate re-observe/finish turn the system prompt asks for.
            # A run with recent positive page-change evidence earns an extension of half the original
            # cap first (SKY-15264): the observed exhaustion population splits into genuinely long
            # multi-page forms dying mid-progress (the extension's target) and stalled runs the gate
            # refuses so they fail exactly as before. The grant repeats under that same predicate
            # while the evidence keeps arriving (SKY-15666) — a form that was long enough to need one
            # extension is routinely long enough to need another — up to a hard multiple of the
            # original cap.
            if spec is not None and spec.billable and max_action_steps is not None and action_steps >= max_action_steps:
                base_cap = original_action_steps if original_action_steps else max_action_steps
                raw_extension = base_cap // 2
                extension_limit = base_cap * ACTION_BUDGET_EXTENSION_MAX_FACTOR
                extension = min(raw_extension, max(0, extension_limit - max_action_steps))
                if max_action_steps_ceiling is not None:
                    # The org's workflow-run-wide step pool is a HARD ceiling the extension must
                    # never breach: truncate the grant to what the pool can fund.
                    extension = min(extension, max_action_steps_ceiling - max_action_steps)
                # Re-derive the runaway guards from the cap this grant would produce, and never
                # below their live values. This is the whole difference between converting a
                # step-cap death into a completion and merely relocating it onto the token guard:
                # the guards were sized as functions of the action-step budget, so moving that
                # budget without moving them leaves the extension running on leftovers.
                grant_max_turns, grant_max_tool_calls, grant_max_tokens = max_turns, max_tool_calls, max_tokens
                headroom_gain = (0, 0, 0)
                token_clamped = False
                if backstops_for_cap is not None and extension > 0:
                    next_turns, next_tool_calls, next_tokens = backstops_for_cap(max_action_steps + extension)
                    grant_max_turns = max(max_turns, next_turns)
                    grant_max_tool_calls = max(max_tool_calls, next_tool_calls)
                    token_gain = 0
                    if max_tokens is not None:
                        grant_max_tokens = max(max_tokens, next_tokens)
                        token_gain = grant_max_tokens - max_tokens
                    headroom_gain = (
                        grant_max_turns - max_turns,
                        grant_max_tool_calls - max_tool_calls,
                        token_gain,
                    )
                    # Turns still scale with the cap but tokens do not: the token guard is at its
                    # ceiling, and a cap raised past that point silently stops buying tokens.
                    # Reported from the grant branch below, never here — a refused extension leaves
                    # the cap where it was, so reporting on it would name a cap never in effect and
                    # would spend the once-per-run latch on a non-event.
                    token_clamped = max_tokens is not None and token_gain == 0 and grant_max_turns > max_turns
                refresh_ctx = skyvern_context.current()
                if max_action_steps >= extension_limit:
                    allowed, gate_reason = False, "extension_limit_reached"
                elif raw_extension <= 0:
                    allowed, gate_reason = False, "cap_too_small"
                elif extension <= 0:
                    allowed, gate_reason = False, "hard_step_ceiling"
                elif refresh_ctx is not None and refresh_ctx.refresh_working_page:
                    # A pending refresh voids this very action and re-baselines the page: the grant
                    # must not race it and spend the extension on pre-reload evidence.
                    allowed, gate_reason = False, "refresh_pending"
                else:
                    allowed, gate_reason = _budget_extension_gate(
                        action_steps,
                        last_change_evidence_step,
                        action_warned,
                        # CURRENT confirmed stalled-ness by the ledger's own rules: form_armed
                        # (the latest look showed a form) plus a window of fruitless actions. Not
                        # the one-shot telemetry latch, and never a bare counter — a form-less page
                        # increments the counter but must not be judged stuck by it.
                        progress is not None
                        and progress.form_armed
                        and progress.actions_since_progress >= progress.window,
                        activity,
                        deadline_at,
                        extension,
                        seconds_per_step=(time.monotonic() - started_at) / max(action_steps, 1),
                        headroom_gain=headroom_gain,
                    )
                # This read cannot see unabsorbed in-batch movement: action_steps is frozen during
                # a batch, so the cap check trips on the batch's FIRST billable call — before any
                # page action dispatches — and between-batch movement was absorbed when
                # batch_fp_before was sampled.
                looping_now = canonical.looping_targets()
                LOG.info(
                    CANONICAL_EXTEND_DELTA_EVENT,
                    current_decision=allowed,
                    gate_reason=gate_reason,
                    canonical_looping_targets=looping_now,
                    would_block_extension=bool(allowed and looping_now),
                )
                if allowed:
                    original_cap = max_action_steps
                    budget_extensions_granted += 1
                    max_action_steps += extension
                    max_turns, max_tool_calls, max_tokens = grant_max_turns, grant_max_tool_calls, grant_max_tokens
                    if activity is not None:
                        # The guards moved mid-turn; the failure-evidence gates read these same
                        # fields later in this turn and would otherwise judge the run on the
                        # headroom it had before the grant.
                        activity.turns_remaining = max_turns - turns
                        activity.tool_calls_remaining = max_tool_calls - total_tool_calls
                        activity.tokens_remaining = None if max_tokens is None else max_tokens - total_tokens
                    if token_clamped and not token_clamp_reported:
                        token_clamp_reported = True
                        LOG.info(
                            "task_v3 token backstop clamped at its ceiling",
                            log_code="taskv3_token_backstop_clamped",
                            step_cap=max_action_steps,
                            max_tokens=max_tokens,
                            extensions_granted=budget_extensions_granted,
                        )
                    if final_turn_granted and _cap_trip_relieved(
                        cap_trip_pending,
                        max_tokens,
                        total_tokens,
                        final_turn_token_reserve,
                        max_turns,
                        turns,
                        max_tool_calls,
                        total_tool_calls,
                    ):
                        # This run was granted its single wrap-up turn by a guard the grant above just
                        # raised past the trip. Before SKY-15666 no cap could be relieved mid-run, so
                        # the latch was permanent and reporting the remembered cap was honest; now it
                        # would end the run naming a budget it is nowhere near, with the extension it
                        # just earned unspent. Release it and let the top-of-turn checks resume.
                        LOG.info(
                            FINAL_TURN_RELEASED_EVENT,
                            cap=cap_trip_pending,
                            turn=turns,
                            extensions_granted=budget_extensions_granted,
                        )
                        released_cap = cap_trip_pending
                        final_turn_granted = False
                        final_turn_started = False
                        cap_trip_pending = None
                        # Staged under a latch that no longer holds: leaving it would stamp a stale
                        # extraction onto a terminal produced by some LATER cap trip.
                        final_turn_staged_output = None
                        if activity is not None:
                            activity.final_turn_active = False
                        # Staged, not appended: a user message may not sit between an assistant
                        # turn's tool calls and their results, and this fires mid-batch. It rides
                        # out with the other end-of-batch notes.
                        budget_extended_notice = _budget_extended_observation(released_cap or "budget", activity)
                    LOG.info(
                        ACTION_BUDGET_EXTENDED_EVENT,
                        original_cap=original_cap,
                        extension=extension,
                        extensions_granted=budget_extensions_granted,
                        base_cap=base_cap,
                        max_turns=max_turns,
                        max_tool_calls=max_tool_calls,
                        max_tokens=max_tokens,
                        action_steps=action_steps,
                        turn=turns,
                        tool=tool_name,
                    )
                else:
                    LOG.info(
                        ACTION_BUDGET_EXTENSION_REFUSED_EVENT,
                        gate_reason=gate_reason,
                        max_action_steps=max_action_steps,
                        extensions_granted=budget_extensions_granted,
                        base_cap=base_cap,
                        action_steps=action_steps,
                        turn=turns,
                        tool=tool_name,
                    )
                    step_cap_trip = f"Reached the maximum steps ({max_action_steps})"
                    _append_skipped_tool_results(messages, tool_calls[idx:], "action-step budget reached")
                    # Unlike the mid-batch max_tool_calls check above, the step gate is NOT special-cased
                    # away once the final turn is granted: a billable dispatch on the granted turn still
                    # hits it, which is the honest exit the grant exists to produce.
                    if final_turn_granted:
                        # A finish staged later in this skipped batch: its VERDICT is not honored
                        # (it presumed the refused action would run — accepting a completed there
                        # could claim a success that never happened), but the extraction it was
                        # already carrying is salvaged onto the honest exit.
                        staged_finish_output = next(
                            (
                                t_args.get("extracted_output")
                                for _t_id, t_name, t_args in tool_calls[idx:]
                                if t_name == "finish" and isinstance(t_args, dict)
                            ),
                            None,
                        )
                        # The cap that granted the final turn is the honest fact to report — the
                        # step gate merely happens to be where the spent grant gets caught.
                        outcome = LoopOutcome(
                            "budget_exhausted",
                            _budget_exhausted_reason(cap_trip_pending or step_cap_trip),
                            cap_trip=cap_trip_pending or step_cap_trip,
                            extracted_output=staged_finish_output,
                        )
                    else:
                        final_turn_granted = True
                        cap_trip_pending = step_cap_trip
                        LOG.info(
                            FINAL_TURN_GRANTED_EVENT,
                            cap=step_cap_trip,
                            turn=turns,
                            tool_calls_remaining=None if activity is None else activity.tool_calls_remaining,
                            tokens_remaining=None if activity is None else activity.tokens_remaining,
                        )
                        messages.append(
                            {"role": "user", "content": _budget_exhausted_observation(step_cap_trip, activity)}
                        )
                    break
            # Submit-shaped actions (the failure-evidence predicate, minus captcha) are reported BEFORE
            # dispatch, since after it the page may be the confirmation page. A failure here never fails
            # the action, and the time is not billed to the tool.
            # Consumed before the side-effecting pre-submit capture, so a call chosen on a page that is
            # gone leaves no artifacts; the recheck right before the handler covers the awaits below.
            hook_ctx = skyvern_context.current()
            if hook_ctx is not None and hook_ctx.refresh_working_page:
                if await _consume_refresh_signal(
                    hook_ctx, tool_name, tool_calls[idx:], round_actions, drop=reload_page is None
                ):
                    break
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
            # Sampled before dispatch so an error below can be checked for having moved the page even
            # when the tool set no flag (a raised exception carries none). Not billed to the tool's timing.
            probe_before: str | None = None
            if spec is not None and page_probe is not None:
                probe_before = await _sample_probe(page_probe, deadline_at=deadline_at)
            # A signal raised between calls (a route handler finishing during the model's turn, or
            # during the pre-dispatch probes above) is honored right before this call runs, as the
            # legacy per-action check does; the model chose it on a page that is gone.
            pre_ctx = skyvern_context.current()
            if pre_ctx is not None and pre_ctx.refresh_working_page:
                if await _consume_refresh_signal(
                    pre_ctx, tool_name, tool_calls[idx:], round_actions, drop=reload_page is None
                ):
                    break
            # Charged only once the call is really dispatched: a call voided by a refresh spent nothing.
            total_tool_calls += 1
            if activity is not None:
                # Refreshed per call, not per turn: a batched action+finish turn must not defer on
                # a stale turn-start snapshot (the conversion the headroom guard exists to prevent).
                activity.tool_calls_remaining = max_tool_calls - total_tool_calls
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
                # Emitted on its own record, never folded into the one above: the tool-call record's
                # fields are a stable contract and this signal must not perturb them.
                seen_count, new_states_since = revisit_memory.record(content_digest)
                # A revisit with fresh ground covered in between is a drill-down returning to its
                # list, not a replay. Reporting those too would fire the signal identically on
                # healthy and stuck runs and leave the precision read it exists to feed unable to
                # tell them apart.
                if seen_count >= PERCEPTION_REVISIT_LOG_AFTER and not new_states_since:
                    LOG.info(
                        PERCEPTION_REVISIT_EVENT,
                        revisit_count=seen_count,
                        distinct_states=revisit_memory.distinct_states,
                        peak_revisits=revisit_memory.peak_revisits,
                        progressed_revisits=revisit_memory.progressed_revisits,
                        capped=revisit_memory.capped,
                        tool=tool_name,
                        turn=turns,
                        **attribution,
                    )
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
            if spec is not None and spec.billable:
                target_key = _call_selector(args) or f"tool:{tool_name}"
                same_count, same_errors = canonical.record_touch(target_key, result.status == "error")
                # Every rung of the distribution is logged (not one cliff): the Phase-B block
                # threshold is tuned from this data.
                if (
                    same_count in CANONICAL_LOOP_FIRE_COUNTS
                    and same_errors >= same_count - 1
                    and canonical.claim_rung(target_key, same_count)
                ):
                    pending_canonical_fires.append(
                        {
                            "gen": canonical.gen,
                            "target_key_hash": telemetry_hash(telemetry_salt, target_key),
                            "tool": tool_name,
                            "repeat_count": same_count,
                            "repeat_errors": same_errors,
                            "turn": turns,
                        }
                    )

            if spec is not None and spec.compactable and result.status == "ok":
                snapshot_indices.add(len(messages))  # index this successful snapshot will occupy, pre-append
            model_facing_content = result.content
            skyvern_ctx = skyvern_context.current()
            if skyvern_ctx is not None:
                model_facing_content = skyvern_ctx.hide_from_model(model_facing_content)
            # A refresh raised by this call re-baselines the stall and repeat ledgers below, so neither
            # guard may end the run on it first.
            refresh_pending = skyvern_ctx is not None and skyvern_ctx.refresh_working_page
            messages.append(
                {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": model_facing_content}
            )
            # A look's annotated screenshot is shown to the model on the next call only, never stored in
            # the transcript. Consumed and cleared at the top of the next turn. Only the LATEST snapshot
            # survives: a second look in the same turn supersedes the first (its marks replace the prior
            # ones), so re-sending the stale image would just hand the model a dead numbering.
            if result.screenshots:
                pending_screenshots = list(result.screenshots)
            result_data = result.data or {}
            # The page-state stall detector (SKY-15265) re-baselines on these flags. Keeps the FIRST
            # qualifying signal this batch -- the reason field is diagnostic (telemetry), not the
            # decision itself, so a later call's signal never overwrites it.
            if batch_page_change_reason is None or batch_page_change_reason == "page_transitioned":
                # page_transitioned checks LAST and can be superseded: it is a URL-only hint the
                # stall detector must not reset on, so a stronger same-batch signal outranks it.
                if result_data.get("page_state_changed"):
                    batch_page_change_reason = "page_state_changed"
                elif result_data.get("download_new"):
                    # A freshly detected download is progress even when the DOM never moves (a
                    # download-next flow); a replayed notice deliberately does not qualify.
                    batch_page_change_reason = "download_new"
                elif tool_name == "hover" and result.status == "ok":
                    # A hover's only purpose is to reveal state (submenus, tooltips) that a
                    # CSS-only change leaves invisible to the innerHTML fingerprint and the
                    # document-identity probe alike -- always treat it as a change signal.
                    batch_page_change_reason = "hover"
                elif tool_name == "navigate" and result.status == "ok":
                    batch_page_change_reason = "navigate"
                elif batch_page_change_reason is None and result_data.get("page_transitioned") is True:
                    batch_page_change_reason = "page_transitioned"
            shadow_baseline_before = progress.invalid_baseline if progress is not None else None
            if _absorb_result_data(tool_name, spec, result_data):
                # Every mark=N still queued in this batch was chosen before this look renumbered the
                # marks, so it now names an arbitrary element; a look refused before rebuilding its
                # manifest leaves the old marks (and their failed keys) live.
                marks_stale = True
                canonical.invalidate_marks()
            _progress_observe_shadow(observe_summary, tool_name, attribution, shadow_baseline_before)
            if content_digest is not None:
                stall_outcome, nudges_due = _perception_stall_check(
                    perception,
                    content_digest,
                    action_key,
                    tool_name,
                    attribution,
                    content_only_digest=hashlib.sha256(_content_only_perception(result.content).encode()).hexdigest(),
                    refresh_pending=refresh_pending,
                )
                stall_nudges_due.extend(nudges_due)
                if stall_outcome is not None:
                    outcome = stall_outcome
                    _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "perception stalled")
                    break
            if spec is not None and spec.billable:
                if progress is not None:
                    progress.on_billable()
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
                    and not refresh_pending
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

            # A page-level handler (e.g. an anti-bot bypass that exhausted its retries) can reload the
            # page out from under any tool call, billable or not; honored after the call's own bookkeeping,
            # never over a verdict the call itself just produced, mirroring the legacy per-action check.
            if skyvern_ctx is not None and skyvern_ctx.refresh_working_page:
                # Consumed whether or not it can be acted on: the context outlives this run, and a
                # signal left set would fire on the first action of the next block.
                verdict_stands = spec is not None and spec.terminal and result.status == "ok"
                if await _consume_refresh_signal(
                    skyvern_ctx,
                    tool_name,
                    tool_calls[idx + 1 :],
                    round_actions,
                    drop=reload_page is None or verdict_stands,
                ):
                    break

            completion_outcome = await _completion_probe_outcome(tool_name, spec, result_data)
            if completion_outcome is not None:
                outcome = completion_outcome
                if spec is not None and spec.billable:
                    # The download landed while this call's probe waited, so this call's touch is
                    # the progressing one: drop ITS pending rungs only, never a sibling target's.
                    completing_hash = telemetry_hash(telemetry_salt, _call_selector(args) or f"tool:{tool_name}")
                    pending_canonical_fires[:] = [
                        fire for fire in pending_canonical_fires if fire["target_key_hash"] != completing_hash
                    ]
                _append_skipped_tool_results(messages, tool_calls[idx + 1 :], "completion probe fired")
                break

            dead_end_status = result_data.get("navigation_dead_end")
            if dead_end_status is not None:
                # A hard 404/410 landing is a non-capability dead-end (a dead/removed posting). Remember
                # it but do NOT break the batch: a later navigate in the same turn can land the run on a
                # live page and clear it below. Applied once the batch settles (after this for-loop).
                pending_nav_dead_end = dead_end_status
            elif result_data.get("page_state_changed"):
                # A successful navigate moved the run off any dead page seen earlier this batch.
                pending_nav_dead_end = None

            if spec is not None and spec.terminal and result.status == "ok":
                data = result.data or {}
                outcome = LoopOutcome(
                    status=data.get("status", "completed"),
                    reason=data.get("reason", ""),
                    extracted_output=data.get("extracted_output"),
                    error_code=data.get("error_code"),
                    error_codes_offered=bool(data.get("error_codes_offered")),
                    # The model's own verdict wins whether or not it landed on the granted final turn;
                    # cap_trip just records the fact that a cap forced this to be the last turn.
                    cap_trip=cap_trip_pending if final_turn_granted else None,
                )
                break

            if result.status == "error":
                # The rest of the batch is skipped only when the failed call moved the page: the tool's
                # own signal, or the probe sampled before dispatch (a missing reading counts as moved).
                poisoned = (
                    tool_name == "navigate"
                    or result.content == PAGE_UNAVAILABLE_ERROR
                    or bool(
                        result_data.get("page_transitioned")
                        or result_data.get("page_state_changed")
                        or result_data.get("navigation_dead_end")
                    )
                )
                # Whether the page moved is independent of the tool's kind: a wait that timed out because
                # the site navigated poisons the batch just as a failed click would.
                if not poisoned and spec is not None and page_probe is not None:
                    probe_after = await _sample_probe(page_probe, deadline_at=deadline_at)
                    poisoned = probe_before is None or probe_after is None or probe_after != probe_before
                    if probe_before is not None and probe_after is not None and probe_after != probe_before:
                        # Two landed identity samples that differ: the failed call still moved the
                        # document, and the fingerprint may render identically on the new one (a
                        # same-template step) — absorb it here or the rung survives that blindness.
                        canonical.progress(_ProgressEvidence.PROBE_MISMATCH)
                if poisoned:
                    _append_skipped_tool_results(
                        messages,
                        tool_calls[idx + 1 :],
                        "earlier tool call in this batch failed and changed the page — re-observe before "
                        "re-queuing these",
                    )
                    break
                if spec is not None and (spec.billable or spec.recordable):
                    batch_had_failure = True
                    if call_selector is not None:
                        failed_selectors.add(call_selector)

        # Whatever skipped or refused a finish once a cap tripped — behind the very call that
        # tripped it in the GRANTING batch, behind a failed call or fail-closed blocker on the
        # granted turn, or under a guard that terminated the batch — the extraction it was already
        # carrying is remembered; the post-loop stamp rides it out on whatever terminal the run
        # ends with. Last write wins, so a granted-turn restatement supersedes the older capture.
        if final_turn_granted:
            staged = next(
                (
                    t_args.get("extracted_output")
                    for _t_id, t_name, t_args in tool_calls
                    if t_name == "finish" and isinstance(t_args, dict) and t_args.get("extracted_output") is not None
                ),
                None,
            )
            if staged is not None:
                final_turn_staged_output = staged

        # The batch settled on a dead page (an in-loop navigate hit a hard 404/410 and no later navigate
        # recovered): end the run as terminated deterministically, matching v1, rather than leaving the
        # failed/terminated choice to the model's finish tool (which does not converge on this class).
        if outcome is None and pending_nav_dead_end is not None:
            LOG.info("taskv3 loop navigation dead end", http_status=pending_nav_dead_end, turn=turns)
            outcome = LoopOutcome(
                "terminated",
                f"{NAV_DEAD_END_REASON_PREFIX} navigate landed on a dead page (HTTP {pending_nav_dead_end}) — "
                "the target no longer exists or has been removed, so the goal cannot be completed there",
            )

        # Page-state stall detector (SKY-15265): tool-independent — any batch of billable work that
        # leaves the rendered document byte-identical ticks the counter, whatever tools produced it.
        # A missing sample is no evidence either way; any page-change flag or fingerprint movement
        # re-baselines.
        if outcome is None and turn_did_action and page_fingerprint is not None and batch_fp_before is not None:
            if page_state_prev_fp is not None and batch_fp_before != page_state_prev_fp:
                # The page moved BETWEEN batches (a delayed render landing after the prior
                # after-sample): the streak the old samples described is stale.
                trailing_page_state_stall_rounds = 0
                page_state_nudge_delivered = False
            page_state_changed: bool | None
            if batch_page_change_reason is not None and batch_page_change_reason != "page_transitioned":
                page_state_changed = True
            else:
                if batch_fp_after is None and not (deadline_at is not None and deadline_at - time.monotonic() <= 0):
                    batch_fp_after = await _sample_probe(page_fingerprint, deadline_at=deadline_at)
                page_state_changed = None if batch_fp_after is None else batch_fp_after != batch_fp_before
            page_state_prev_fp = batch_fp_after if batch_fp_after is not None else batch_fp_before
            page_state_ever_judged = page_state_ever_judged or page_state_changed is not None
            if page_state_changed is True:
                trailing_page_state_stall_rounds = 0
                page_state_nudge_delivered = False
                canonical.progress(_ProgressEvidence.PAGE_STATE_VERDICT)
            elif page_state_changed is False:
                trailing_page_state_stall_rounds += 1
                peak_page_state_stall_rounds = max(peak_page_state_stall_rounds, trailing_page_state_stall_rounds)
                if trailing_page_state_stall_rounds == PAGE_STATE_STALL_TERMINATE_AFTER and page_state_nudge_delivered:
                    # Shadow-only verdict: measured, not enforced (see PAGE_STATE_STALL_SHADOW_EVENT).
                    LOG.info(
                        PAGE_STATE_STALL_SHADOW_EVENT,
                        rounds=trailing_page_state_stall_rounds,
                        turn=turns,
                    )
                elif (
                    trailing_page_state_stall_rounds >= PAGE_STATE_STALL_NUDGE_AFTER and not page_state_nudge_delivered
                ):
                    page_state_nudge_due = True

        if (
            outcome is not None
            # An acknowledged cancellation must not wait on a possibly-hung renderer just to
            # decide telemetry; and a batch whose pending fires are all generation-stale already
            # has nothing left to absorb for.
            and outcome.status != "canceled"
            and page_fingerprint is not None
            and batch_fp_before is not None
            and any(fire["gen"] == canonical.gen for fire in pending_canonical_fires)
        ):
            # A terminal outcome mid-batch (a finish, a fired completion probe) skips the detector
            # above, so the batch's own movement is unabsorbed here: take the after-sample now and
            # clear on a positive mismatch only, before deciding the pending events below.
            if batch_fp_after is None:
                batch_fp_after = await _sample_probe(page_fingerprint, deadline_at=deadline_at)
            if batch_fp_after is not None and batch_fp_after != batch_fp_before:
                canonical.progress(_ProgressEvidence.TERMINAL_BATCH_FINGERPRINT)
        # A rung completed by — or followed in this batch by — absorbed progress describes a
        # progressing run, not a loop: only events whose generation survived every clear above
        # (same-call result data, invalid-fields baseline, batch fingerprint verdict) are emitted.
        for pending_fire in pending_canonical_fires:
            if pending_fire.pop("gen") == canonical.gen:
                LOG.info(CANONICAL_LOOP_EVENT, **pending_fire)
        pending_canonical_fires.clear()

        # Warn only after the batch completes: a user message may not sit between an assistant
        # turn's tool results, and the model reads it with the snapshot that tripped it. Every note
        # due this turn shares ONE user message so the transcript keeps alternating roles.
        nudge_parts: list[str] = []
        if budget_extended_notice is not None:
            # First: it retracts a standing claim that the run is ending, which every other note
            # this turn is written as if untrue.
            nudge_parts.append(budget_extended_notice)
        if outcome is None and refresh_nudge_due:
            nudge_parts.append(_refresh_nudge_text())
        elif outcome is None and reload_failed_nudge_due:
            nudge_parts.append(_reload_failed_nudge_text())
        if outcome is None and stall_nudges_due:
            nudge_parts.append(_stall_nudge_text(stall_nudges_due, set(tool_by_name)))
        if outcome is None and page_state_nudge_due:
            page_state_nudge_due = False
            page_state_nudge_delivered = True
            LOG.info("taskv3 loop page state stall nudged", rounds=trailing_page_state_stall_rounds, turn=turns)
            nudge_parts.append(_page_state_nudge_text(trailing_page_state_stall_rounds))
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
                nudge_parts.append(_action_nudge_text(still_stuck, set(tool_by_name)))
        if nudge_parts:
            messages.append({"role": "user", "content": "\n\n".join(nudge_parts)})

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
                await on_action_round(round_actions, text or reasoning_summary or None)
            except Exception:
                LOG.warning("taskv3 on_action_round callback failed", turn=turns, exc_info=True)

    if outcome is None:
        outcome = LoopOutcome("loop_error", "loop exited without an outcome")

    # Every model- or loop-produced terminal once the cap tripped — a finish verdict, a guard
    # termination (even in the granting batch itself), a stall exit, the spent-grant exit —
    # happened UNDER the cap: it carries the cap fact, and a missing extraction is filled from
    # what a skipped finish had staged (the model's own earlier data), so no exit path can
    # re-discard the partial output. A completed verdict that didn't restate its output would
    # otherwise be demoted for missing extraction.
    if final_turn_granted and outcome.status in ("completed", "failed", "terminated", "budget_exhausted", "loop_error"):
        if outcome.cap_trip is None:
            outcome.cap_trip = cap_trip_pending
        if outcome.extracted_output is None:
            outcome.extracted_output = final_turn_staged_output

    ledger_fields: LedgerTerminalFields | None = None
    if progress is not None and progress.ever_armed:
        # The ledger is precision-biased (see _ProgressLedger), so read its fire precision as
        # trustworthy but its recall as a FLOOR: few fires is not few stuck runs.
        ledger_fields = LedgerTerminalFields(
            peak_actions_since_progress=progress.peak_actions_since_progress,
            actions_since_progress=progress.actions_since_progress,
            form_armed=progress.form_armed,
            would_fire=progress.shadow_reported,
        )
    outcome.telemetry = TerminalTelemetry(
        # The sticky flag, not `form_armed`: the latter is the CURRENT look and is cleared by
        # progress, so a run that saw a form early and lost it by the end reads False on it. This is
        # the partition the two collapsed records used to encode by which one of them fired.
        form_ever_armed=progress is not None and progress.ever_armed,
        survival=canonical.survival_fields(),
        ledger=ledger_fields,
        # Present only where the detector actually judged the page at least once, so the field means
        # "this was the worst streak" and never "nothing ever looked".
        peak_page_state_stall_rounds=peak_page_state_stall_rounds if page_state_ever_judged else None,
        # Present for any run that RE-READ a probe, which is the population the counter is defined
        # on. It ships unconditionally there because it is the calibration input for a pending
        # threshold: the shadow line fires only above the current cutoff, so nothing below it is
        # observable from logs today.
        peak_probe_revisits=perception.peak_probe_revisits if perception.revisit_chances else None,
        semantic_commit=semantic_commit_stats,
    )

    outcome.turns = turns
    outcome.no_tool_call_turns = no_tool_call_turns
    outcome.tool_choice_in_effect = "tool_choice" in active_call_kwargs
    outcome.tool_calls = total_tool_calls
    outcome.tool_seconds = tool_seconds
    outcome.action_steps = action_steps
    outcome.billable_actions = billable_actions
    outcome.messages = messages
    return outcome
