"""Last-known action sub-step per run, readable from a sampler thread outside the event loop.

Markers only mutate an in-memory record; nothing is logged per marker. A memory sampler reads the
snapshot when it fires so a surge can be attributed to the sub-step that was active. The store is
bounded by run and per-run record counts (finished records also by age) and holds pages weakly. Only actions dispatched through
the action handler are tracked; the real-page AI path is not.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ActionPhase(StrEnum):
    ACTION_START = "action_start"
    SETUP = "setup"
    HANDLER = "handler"
    RESOLVE_ELEMENT = "resolve_element"
    PRE_CLICK_CHECKS = "pre_click_checks"
    FRAME_CREATE = "frame_create"
    OBSERVER_START = "observer_start"
    OBSERVER_LISTENING = "observer_listening"
    CLICK = "click"
    POST_CLICK = "post_click"
    INPUT = "input"
    AUTOCOMPLETE = "autocomplete"
    OBSERVER_STOP = "observer_stop"
    OBSERVER_STOPPED = "observer_stopped"
    TEARDOWN = "teardown"
    ACTION_DONE = "action_done"


MAX_TRACKED_RUNS = 16
MAX_RECORDS_PER_RUN = 4
SNAPSHOT_LIMIT = 4
# Applies to finished actions only: an in-flight action can legitimately outlast any fixed cutoff (the
# execution limit is extended for waits), and is bounded by the run/record caps and teardown instead.
STALE_AFTER_SECONDS = 900.0

_monotonic = time.monotonic


@dataclass(slots=True)
class _PhaseRecord:
    run_key: str
    action_type: str
    phase: ActionPhase
    seq: int
    action_started_at: float
    phase_started_at: float
    page_ref: weakref.ReferenceType[Any] | None
    frame_ref: weakref.ReferenceType[Any] | None
    aliases: frozenset[str]

    @property
    def in_flight(self) -> bool:
        return self.phase is not ActionPhase.ACTION_DONE


_lock = threading.Lock()
# Keyed by the immediate run; each run holds up to MAX_RECORDS_PER_RUN concurrent action records.
_slots: OrderedDict[str, list[_PhaseRecord]] = OrderedDict()
_current: ContextVar[_PhaseRecord | None] = ContextVar("skyvern_action_phase_record", default=None)


def _weak(target: Any) -> weakref.ReferenceType[Any] | None:
    if target is None:
        return None
    try:
        return weakref.ref(target)
    except TypeError:
        return None


def _drop_oldest(records: list[_PhaseRecord]) -> None:
    finished = [record for record in records if not record.in_flight]
    records.remove(finished[0] if finished else records[0])


def begin_action(
    run_key: str, action_type: str, page: Any, aliases: tuple[str | None, ...] = ()
) -> Token[_PhaseRecord | None]:
    """Start tracking an action for ``run_key``; returns a token for :func:`end_action`.

    ``aliases`` are the other run identities an activity may tear down under, so cleanup finds the record.
    """
    now = _monotonic()
    record = _PhaseRecord(
        run_key=run_key,
        action_type=action_type,
        phase=ActionPhase.ACTION_START,
        seq=1,
        action_started_at=now,
        phase_started_at=now,
        page_ref=_weak(page),
        frame_ref=None,
        aliases=frozenset(alias for alias in aliases if alias and alias != run_key),
    )
    with _lock:
        # A run's finished actions are superseded by its next one; concurrent in-flight ones are kept.
        records = [existing for existing in _slots.pop(run_key, []) if existing.in_flight]
        records.append(record)
        while len(records) > MAX_RECORDS_PER_RUN:
            _drop_oldest(records)
        _slots[run_key] = records
        while len(_slots) > MAX_TRACKED_RUNS:
            idle = next((key for key, held in _slots.items() if not any(r.in_flight for r in held)), None)
            _slots.pop(idle if idle is not None else next(iter(_slots)))
    return _current.set(record)


def mark_action_phase(phase: ActionPhase, *, observed_frame: Any = None) -> None:
    """Record the sub-step the current action entered. A no-op outside an action scope."""
    record = _current.get()
    if record is None:
        return
    with _lock:
        record.phase = phase
        record.seq += 1
        record.phase_started_at = _monotonic()
        if observed_frame is not None:
            record.frame_ref = _weak(observed_frame)


def end_action(token: Token[_PhaseRecord | None] | None) -> None:
    if token is None:
        return
    mark_action_phase(ActionPhase.ACTION_DONE)
    try:
        _current.reset(token)
    except ValueError:
        # Reset from a different context than the one that set it; drop the scope instead.
        _current.set(None)


def clear_action_phases(run_id: str) -> None:
    with _lock:
        for key in [key for key, held in _slots.items() if key == run_id or any(run_id in r.aliases for r in held)]:
            del _slots[key]


def _live_records(now: float) -> list[_PhaseRecord]:
    with _lock:
        records = [
            r
            for held in _slots.values()
            for r in held
            if r.in_flight or now - r.phase_started_at <= STALE_AFTER_SECONDS
        ]
    # An action still in flight is what a surge is attributed to, so it outranks a later-finished one.
    records.sort(key=lambda r: (r.in_flight, r.phase_started_at), reverse=True)
    return records


def snapshot_action_phases() -> list[dict[str, Any]]:
    """In-flight actions first, then by recency; at most ``SNAPSHOT_LIMIT`` fixed-key entries."""
    now = _monotonic()
    return [
        {
            "run_id": record.run_key,
            "action_type": record.action_type,
            "phase": record.phase.value,
            "phase_seq": record.seq,
            "phase_age_s": round(now - record.phase_started_at, 1),
            "action_age_s": round(now - record.action_started_at, 1),
        }
        for record in _live_records(now)[:SNAPSHOT_LIMIT]
    ]


def page_probe_target() -> tuple[Any, Any] | None:
    """The (page, observed frame) of the most relevant action whose page or frame is alive."""
    for record in _live_records(_monotonic()):
        page = record.page_ref() if record.page_ref is not None else None
        frame = record.frame_ref() if record.frame_ref is not None else None
        if page is not None or frame is not None:
            return page, frame
    return None


def _reset_for_tests() -> None:
    with _lock:
        while _slots:
            _slots.popitem()
    _current.set(None)
