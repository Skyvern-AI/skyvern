"""Names the operation a copilot turn is waiting on, for the log emitters only. Nothing branches on this state."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import structlog

LOG = structlog.get_logger()

PENDING_OPERATION_LOG_THRESHOLD_SECONDS = 30.0

CORRELATION_ATTRS = ("workflow_permanent_id", "turn_id", "workflow_copilot_chat_id")


@dataclass(frozen=True)
class PendingOperation:
    name: str
    started_monotonic: float
    state: str
    duration: float | None = None
    span: bool = False

    def outstanding_seconds(self, now: float) -> float:
        """How long this operation ran, or has been running — never the age of a finished record."""
        return self.duration if self.duration is not None else now - self.started_monotonic


@dataclass
class _TurnOperations:
    open: dict[int, PendingOperation] = field(default_factory=dict)
    last_unwound: PendingOperation | None = None
    log_fields: dict[str, str] = field(default_factory=dict)


_turn_operations: contextvars.ContextVar[_TurnOperations | None] = contextvars.ContextVar(
    "_copilot_turn_operations",
    default=None,
)

_operation_ids = itertools.count()


def install_pending_operation_slot(ctx: Any = None) -> None:
    """Must run in the task that starts the turn: the agents SDK runs model and tool calls in child
    tasks, which copy the context but share this object, so a slot installed deeper is invisible to
    the turn's own log emitters."""
    try:
        log_fields = _correlation_fields(ctx)
    except Exception:
        # An uncorrelated slot beats inheriting the previous turn's, which would file this turn's
        # scopes under the previous turn's ids.
        log_fields = {}
        LOG.warning("copilot_pending_operation_correlation_failed")
    _turn_operations.set(_TurnOperations(log_fields=log_fields))


def _correlation_fields(ctx: Any) -> dict[str, str]:
    """The ids that join a fingerprint to its request, read once per turn. Non-string values are
    dropped: a bare ``MagicMock`` ctx returns a Mock from ``getattr`` rather than the default, and a
    Mock is not a correlation key."""
    fields: dict[str, str] = {}
    for attr in CORRELATION_ATTRS:
        value = getattr(ctx, attr, None)
        if isinstance(value, str) and value:
            fields[attr] = value
    return fields


@contextlib.contextmanager
def pending_operation(name: str, *, span: bool = False) -> Iterator[None]:
    """Records ``name`` as the operation being awaited for the duration of the block.

    ``span=True`` marks a scope that wraps a whole turn iteration rather than one operation. Such a
    scope is always outstanding and always over the threshold, so it neither logs a slow line nor
    claims the fingerprint while any real operation is known.
    """
    operations = _turn_operations.get()
    if operations is None:
        yield
        return
    if span:
        _discard_abandoned(operations)
    operation_id = next(_operation_ids)
    operations.open[operation_id] = PendingOperation(
        name=name,
        started_monotonic=time.monotonic(),
        state="open",
        span=span,
    )
    try:
        yield
    except asyncio.CancelledError:
        _retire(operations, operation_id, "unwound_by_cancellation")
        raise
    except BaseException:
        _retire(operations, operation_id, "unwound_by_error")
        raise
    _retire(operations, operation_id, "returned")


def _discard_abandoned(operations: _TurnOperations) -> None:
    """Drops leaf scopes left open by a previous iteration.

    A scope wrapping an async generator's ``yield`` never exits if the consumer walks away without
    finalising it — ``LitellmModel.stream_response`` is one — and the record would then outrank
    every later operation for the rest of the turn, naming a call that stopped minutes earlier as
    the one still running. Leaves live inside an iteration, so any still open when the next one
    starts was abandoned.
    """
    for operation_id, record in list(operations.open.items()):
        if not record.span:
            del operations.open[operation_id]


def _retire(operations: _TurnOperations, operation_id: int, state: str) -> None:
    """Moves a finished operation out of ``open`` and into the fallback slot the emitters read when
    nothing is open — ``wait_for`` unwinds every inner scope before the deadline emitter runs, and
    the blocked one unwinds last, so the most recently retired scope is the one that was stuck."""
    record = operations.open.pop(operation_id, None)
    if record is None:
        return
    duration = time.monotonic() - record.started_monotonic
    retired = PendingOperation(
        name=record.name,
        started_monotonic=record.started_monotonic,
        state=state,
        duration=duration,
        span=record.span,
    )
    if not record.span:
        operations.last_unwound = retired
    if not record.span and duration >= PENDING_OPERATION_LOG_THRESHOLD_SECONDS:
        try:
            LOG.warning(
                "copilot_pending_operation_slow",
                pending_operation=record.name,
                pending_operation_started_monotonic=round(record.started_monotonic, 3),
                pending_operation_seconds=round(duration, 3),
                pending_operation_state=state,
                **operations.log_fields,
            )
        except Exception:
            pass


def _best_candidate(operations: _TurnOperations) -> PendingOperation | None:
    """Which scope best explains where the turn is.

    An operation still outstanding outranks one that already finished, so a fast sibling that
    started later and returned cannot blame itself for a hang — the SDK cancels concurrent tool
    tasks without awaiting their cleanup, so the one that hung can still be open here. Among equals
    the latest start wins, which for nesting is the innermost scope. Otherwise the scope that
    retired most recently, which during a ``wait_for`` unwind is the one that was blocked. The
    whole-iteration span is the last resort: it is always open, so ranking it with the rest would
    let it mask every real answer.
    """
    open_operations = [record for record in operations.open.values() if not record.span]
    if open_operations:
        return max(open_operations, key=lambda record: record.started_monotonic)
    if operations.last_unwound is not None:
        return operations.last_unwound
    spans = list(operations.open.values())
    return max(spans, key=lambda record: record.started_monotonic) if spans else None


def pending_operation_fields() -> dict[str, str | float | int]:
    """The operation that best explains where the turn is — see ``_best_candidate``.

    ``pending_operation_seconds`` is always that operation's own outstanding time, so a scope that
    finished early in a long turn cannot read as though it hung for the whole budget.

    Never raises, so a failed read cannot change what the emitters do.
    """
    try:
        operations = _turn_operations.get()
        if operations is None:
            return {}
        latest = _best_candidate(operations)
        if latest is None:
            return {}
        return {
            "pending_operation": latest.name,
            "pending_operation_started_monotonic": round(latest.started_monotonic, 3),
            "pending_operation_seconds": round(latest.outstanding_seconds(time.monotonic()), 3),
            "pending_operation_state": latest.state,
            "pending_operation_open_count": len(operations.open),
            **operations.log_fields,
        }
    except Exception:
        return {}
