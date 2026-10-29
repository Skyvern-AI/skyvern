from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.datetime_utils import naive_utc_now

if TYPE_CHECKING:
    from skyvern.forge.sdk.db.repositories.run_execution_leases import RunExecutionLeasesRepository

LOG = structlog.get_logger()
INTERVAL_SECONDS = 60
HEARTBEAT_ERROR_RETRY_SECONDS = 1
SHUTDOWN_DRAIN_SECONDS = 15
_pid: int | None = None
_owner_id: str | None = None
_enabled = False
_wake = asyncio.Event()
_stop = asyncio.Event()
_lease_write_lock = asyncio.Lock()
_registry: dict[str, _Registration] = {}
_kept_leases: set[str] = set()
_held: set[str] = set()
_tasks: set[asyncio.Task[None]] = set()
_reaper_task: asyncio.Task[None] | None = None
_side_effect_tasks: set[asyncio.Task[None]] = set()
_side_effect_limit = asyncio.Semaphore(10)
# Tasks this module already cancelled; a second cancellation would interrupt the cleanup the first one started.
_interrupted: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()


@dataclass
class _Registration:
    run_type: str
    attempt_number: int | None
    count: int = 0
    task: asyncio.Task[Any] | None = None
    acquired: bool = False
    lost: bool = False
    finalized: bool = False
    # Registrations open where this one was entered: an inline child's task is awaited by its parent's.
    enclosing: tuple[_Registration, ...] = field(default=(), repr=False)
    cancel_seen: bool = False
    # This execution's own conditional write set the run canceled; delivery must not interrupt its cleanup.
    internal_cancel_won: bool = False


class RunExecution(NamedTuple):
    """A registration as bound when an execution entered it; the registration itself is re-bound in place."""

    entry: _Registration
    task: asyncio.Task[Any] | None
    attempt_number: int | None

    def is_current(self, run_id: str) -> bool:
        return (
            self.task is not None
            and _registry.get(run_id) is self.entry
            and self.entry.task is self.task
            and self.entry.attempt_number == self.attempt_number
        )


_open_executions: ContextVar[tuple[RunExecution, ...]] = ContextVar("run_liveness_open_executions", default=())


def owner_id() -> str:
    global _pid, _owner_id
    pid = os.getpid()
    if _pid != pid or _owner_id is None:
        _pid = pid
        _owner_id = f"{socket.gethostname()}:{pid}:{uuid.uuid4().hex[:8]}"
    return _owner_id


def run_lease_tasks_enabled() -> bool:
    return _enabled


def effective_timeout_minutes() -> int:
    return settings.RUN_HEARTBEAT_TIMEOUT_MINUTES


def _register(
    run_id: str,
    run_type: str,
    attempt_number: int | None,
    task: asyncio.Task[Any] | None,
) -> Callable[[bool], None]:
    if not _enabled:
        return lambda cancelled: None
    _kept_leases.discard(run_id)
    entry = _registry.get(run_id)
    if entry is None or entry.attempt_number != attempt_number:
        entry = _Registration(run_type, attempt_number, enclosing=_open_entries())
        _registry[run_id] = entry
    entry.count += 1
    if entry.task is not task:
        entry.cancel_seen = False
        entry.internal_cancel_won = False
        if task is not None:
            # A held run's scope is where it executes: the dispatcher that held it may be another live run.
            entry.enclosing = _open_entries()
    entry.task = task
    _wake.set()
    released = False

    def release(cancelled: bool) -> None:
        nonlocal released
        if released:
            return
        released = True
        entry.count -= 1
        if entry.count == 0 and _registry.get(run_id) is entry:
            if cancelled:
                _kept_leases.add(run_id)
            del _registry[run_id]
        _wake.set()

    return release


@contextmanager
def track_run(run_id: str, run_type: str, attempt_number: int | None = None) -> Iterator[None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    release = _register(run_id, run_type, attempt_number, task)
    enclosing = _open_executions.get()
    entry = _registry.get(run_id) if _enabled else None
    if entry is not None:
        _open_executions.set((*enclosing, RunExecution(entry, entry.task, entry.attempt_number)))
    cancelled = False
    try:
        yield
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        _open_executions.set(enclosing)
        release(cancelled)


def _open_entries() -> tuple[_Registration, ...]:
    return tuple(scope.entry for scope in _open_executions.get())


def current_execution(run_id: str) -> RunExecution | None:
    """The caller's own execution of the run, if the registration is still bound as that execution entered it."""
    return next((scope for scope in _open_executions.get() if scope.is_current(run_id)), None)


def note_internal_cancel_won(run_id: str, execution: RunExecution | None) -> None:
    """Record that the execution captured before its canceled write won that write, if it is still the same one."""
    if execution is not None and execution.is_current(run_id):
        execution.entry.internal_cancel_won = True


def start_independent_run_scope() -> None:
    """Detach a fire-and-forget run from the registrations of the run that dispatched it."""
    _open_executions.set(())


def hold_run(run_id: str, run_type: str) -> Callable[[], None]:
    release = _register(run_id, run_type, None, None)
    return lambda: release(False)


def is_run_registered(run_id: str) -> bool:
    return run_id in _registry


@dataclass(frozen=True)
class RunWriteAuthority:
    owner_id: str
    attempt_number: int | None
    lease_acquired: bool


def run_write_authority(run_id: str) -> RunWriteAuthority | None:
    """This process's claim on the run for outcome writes, or None when such writes are not fenced."""
    if not _enabled:
        return None
    entry = _registry.get(run_id)
    if entry is None:
        return None
    return RunWriteAuthority(owner_id(), entry.attempt_number, entry.acquired or entry.lost)


class BlockOutcomeAuthority(NamedTuple):
    """The registered execution that may write a run's block outcomes from this process."""

    owner_id: str
    attempt_number: int | None
    lease_acquired: bool


def block_outcome_authority(run_id: str) -> BlockOutcomeAuthority | None:
    """Who may write this run's block outcomes, or None when such writes are not guarded."""
    if not _enabled:
        return None
    entry = _registry.get(run_id)
    if entry is None:
        return None
    return BlockOutcomeAuthority(owner_id(), entry.attempt_number, entry.acquired or entry.lost)


def note_run_finalized(run_id: str) -> None:
    entry = _registry.get(run_id)
    if entry is not None:
        entry.finalized = True


async def acquire_run_lease(run_id: str) -> bool:
    if not _enabled:
        return True
    entry = _registry.get(run_id)
    if entry is None:
        return True
    if entry.acquired or entry.lost:
        return not entry.lost
    attempt = entry.attempt_number
    try:
        async with _lease_write_lock:
            if _registry.get(run_id) is not entry or entry.attempt_number != attempt:
                return False
            if entry.acquired or entry.lost:
                return not entry.lost
            acquired = await app.DATABASE.run_execution_leases.acquire(
                owner_id(),
                {run_id: (entry.run_type, attempt)},
                stale_before=naive_utc_now() - timedelta(minutes=effective_timeout_minutes()),
            )
            if run_id not in acquired:
                entry.lost = True
                entry.acquired = False
                LOG.info(
                    "Run lease held by another process",
                    run_id=run_id,
                    run_type=entry.run_type,
                    attempt_number=attempt,
                    owner_id=owner_id(),
                )
                return False
            _held.add(run_id)
            if _registry.get(run_id) is entry and entry.attempt_number == attempt:
                entry.acquired = True
                return True
            return False
    except Exception:
        LOG.warning("Run lease acquire failed before run start", run_id=run_id, exc_info=True)
        return _registry.get(run_id) is entry and entry.attempt_number == attempt and not entry.lost


async def run_lease_state(run_id: str) -> Literal["held", "lost", "unknown"]:
    if not _enabled:
        return "held"
    entry = _registry.get(run_id)
    if entry is None or (not entry.acquired and not entry.lost):
        return "held"
    if entry.lost:
        return "lost"
    try:
        renewed = await app.DATABASE.run_execution_leases.renew(owner_id(), [run_id])
    except Exception:
        LOG.warning("Run lease check failed before status write", run_id=run_id, exc_info=True)
        return "unknown"
    if run_id in renewed:
        return "held"
    LOG.warning(
        "Run lease lost before status write",
        run_id=run_id,
        run_type=entry.run_type,
        attempt_number=entry.attempt_number,
        owner_id=owner_id(),
    )
    return "lost"


async def run_lease_lost(run_id: str) -> bool:
    return await run_lease_state(run_id) == "lost"


async def run_lease_final_write_blocked(run_id: str) -> bool:
    # A failed lease check must not let a final write replace a final row another server may have committed.
    return await run_lease_state(run_id) != "held"


def _cancel_lost_runs(entries: list[_Registration]) -> None:
    # Repeated cancellation would interrupt the cleanup started by the first cancellation.
    lost_targets = {entry.task for entry in entries if entry.task is not None and not entry.finalized}
    for target in lost_targets:
        if (
            not target.done()
            and target not in _interrupted
            and all(entry.lost and not entry.finalized for entry in _registry.values() if entry.task is target)
        ):
            _interrupted.add(target)
            target.cancel()


async def _deliver_accepted_cancels() -> None:
    """Cancel this process's execution of a run whose row an accepted cancel has set to canceled.

    Unlike lease loss, a finalized registration is not exempt. A run is cancelled once, after its row was seen
    canceled on two consecutive passes, so an execution that notices the row at its next block finishes on its own.
    """
    # Snapshot the task and attempt too: a registration can be re-bound to a new task while the query awaits.
    observed = {
        run_id: (entry, entry.task, entry.attempt_number)
        for run_id, entry in _registry.items()
        if entry.run_type == "workflow_run" and entry.task is not None
    }
    if not observed:
        return
    rows = await app.DATABASE.workflow_runs.get_workflow_runs_by_ids(list(observed))
    canceled = {row.workflow_run_id for row in rows if row.status == "canceled"}

    def unchanged(run_id: str, entry: _Registration) -> bool:
        snapshot = observed.get(run_id)
        return (
            snapshot is not None
            and snapshot[0] is entry
            and _registry.get(run_id) is entry
            and entry.task is snapshot[1]
            and entry.attempt_number == snapshot[2]
        )

    registered = {id(entry) for entry in _registry.values()}
    for run_id, (entry, target, _) in observed.items():
        if not unchanged(run_id, entry):
            continue
        if run_id not in canceled:
            entry.cancel_seen = False
            continue
        if not entry.cancel_seen:
            entry.cancel_seen = True
            continue
        if target is None or target.done() or target in _interrupted:
            continue
        # An inline child's task is awaited by an enclosing run, which is cancelled through its own registration.
        if any(id(outer) in registered for outer in entry.enclosing):
            continue
        sharing = [(other_id, other) for other_id, other in _registry.items() if other.task is target]
        # A run that set itself canceled is finishing its own cleanup.
        if any(other.internal_cancel_won for _, other in sharing):
            continue
        # Every run executing on the task must be one observed canceled in this pass.
        if all(other_id in canceled and unchanged(other_id, other) for other_id, other in sharing):
            _interrupted.add(target)
            target.cancel()
            LOG.info("Cancelled the execution of a canceled run", run_id=run_id, attempt_number=entry.attempt_number)


async def _heartbeat(repository: RunExecutionLeasesRepository) -> None:
    last_renew = time.monotonic()
    while True:
        _wake.clear()
        try:
            # Snapshot the generation as well as the object: a retry can enter while an UPDATE awaits I/O.
            renewing = {
                key: (entry, entry.attempt_number)
                for key, entry in _registry.items()
                if entry.acquired and not entry.lost
            }
            # Serialize release and acquire so a release cannot delete a new registration's lease.
            async with _lease_write_lock:
                released = _held - _registry.keys()
                await repository.release(owner_id(), released - _kept_leases)
                _held.difference_update(released)
            if time.monotonic() - last_renew >= INTERVAL_SECONDS:
                last_renew = time.monotonic()
                renewed = await repository.renew(owner_id(), renewing.keys())
                newly_lost: list[_Registration] = []
                for run_id, (entry, attempt) in renewing.items():
                    if run_id in renewed or _registry.get(run_id) is not entry or entry.attempt_number != attempt:
                        continue
                    entry.lost = True
                    entry.acquired = False
                    newly_lost.append(entry)
                    _held.discard(run_id)
                    LOG.warning(
                        "Run lease lost",
                        finalized=entry.finalized,
                        run_id=run_id,
                        run_type=entry.run_type,
                        attempt_number=attempt,
                        owner_id=owner_id(),
                    )
                _cancel_lost_runs(newly_lost)
                try:
                    await _deliver_accepted_cancels()
                except Exception:
                    LOG.warning("Failed to check for canceled runs", owner_id=owner_id(), exc_info=True)
            async with _lease_write_lock:
                acquiring = {
                    key: (entry, entry.attempt_number)
                    for key, entry in _registry.items()
                    if not entry.acquired and not entry.lost
                }
                acquired = await repository.acquire(
                    owner_id(),
                    {key: (entry.run_type, attempt) for key, (entry, attempt) in acquiring.items()},
                    stale_before=naive_utc_now() - timedelta(minutes=effective_timeout_minutes()),
                )
                acquisition_losses: list[_Registration] = []
                for run_id, (entry, attempt) in acquiring.items():
                    if run_id not in acquired:
                        if _registry.get(run_id) is not entry or entry.attempt_number != attempt:
                            continue
                        entry.lost = True
                        entry.acquired = False
                        acquisition_losses.append(entry)
                        LOG.info(
                            "Run lease held by another process",
                            run_id=run_id,
                            run_type=entry.run_type,
                            attempt_number=attempt,
                            owner_id=owner_id(),
                        )
                        continue
                    _held.add(run_id)
                    if _registry.get(run_id) is entry and entry.attempt_number == attempt:
                        entry.acquired = True
                _cancel_lost_runs(acquisition_losses)
            _kept_leases.intersection_update(_held)
        except Exception:
            LOG.exception("Run lease heartbeat failed", owner_id=owner_id())
            # A failing release runs before `last_renew` advances, so an overdue renewal would otherwise retry at once.
            await asyncio.sleep(HEARTBEAT_ERROR_RETRY_SECONDS)
        try:
            remaining = max(0.0, min(INTERVAL_SECONDS, INTERVAL_SECONDS - (time.monotonic() - last_renew)))
            async with asyncio.timeout(remaining):
                await _wake.wait()
        except TimeoutError:
            pass


async def _reaper_loop(reap_once: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            async with asyncio.timeout(INTERVAL_SECONDS):
                await _stop.wait()
            return
        except TimeoutError:
            pass
        # The interval can expire in the same turn a stop is requested; no pass may start once it is.
        if _stop.is_set():
            return
        try:
            await reap_once()
            LOG.debug("Orphaned run reaper pass completed")
        except Exception:
            LOG.exception("Orphaned run reaper pass failed")


def schedule_side_effects(effect: Callable[[], Awaitable[None]]) -> None:
    async def run() -> None:
        try:
            async with _side_effect_limit, asyncio.timeout(300):
                await effect()
        except Exception:
            LOG.exception("Orphaned run side effects failed")

    task = asyncio.create_task(run(), name="orphaned-run-side-effects")
    _side_effect_tasks.add(task)
    task.add_done_callback(_side_effect_tasks.discard)


def start_run_lease_tasks(reap_once: Callable[[], Awaitable[None]]) -> None:
    global _enabled, _wake, _stop, _reaper_task, _side_effect_limit, _lease_write_lock
    if _enabled or settings.RUN_HEARTBEAT_TIMEOUT_MINUTES <= 0:
        return
    _enabled = True
    _wake = asyncio.Event()
    _stop = asyncio.Event()
    _lease_write_lock = asyncio.Lock()
    _held.clear()
    _side_effect_limit = asyncio.Semaphore(10)
    _tasks.add(asyncio.create_task(_heartbeat(app.DATABASE.run_execution_leases), name="run-lease-heartbeat"))
    _reaper_task = asyncio.create_task(_reaper_loop(reap_once), name="orphaned-run-reaper")
    _tasks.add(_reaper_task)
    LOG.info("Run lease tasks started", owner_id=owner_id(), timeout_minutes=effective_timeout_minutes())


async def stop_run_lease_tasks() -> None:
    global _enabled, _reaper_task
    _enabled = False
    _stop.set()
    # Stop the producers before collecting effects so a pass cannot enqueue work during shutdown. An active reaper
    # pass is awaited, not cancelled: it schedules its effects only after its lease release commits.
    for task in _tasks:
        if task is not _reaper_task:
            task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _reaper_task = None
    _tasks.clear()
    effects = list(_side_effect_tasks)
    if not effects:
        return
    _, pending = await asyncio.wait(effects, timeout=SHUTDOWN_DRAIN_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if pending:
        LOG.warning("Orphaned run side effects cancelled at shutdown", count=len(pending))
    _side_effect_tasks.clear()
