from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

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
_lease_write_lock = asyncio.Lock()
_registry: dict[str, _Registration] = {}
_kept_leases: set[str] = set()
_held: set[str] = set()
_tasks: set[asyncio.Task[None]] = set()
_side_effect_tasks: set[asyncio.Task[None]] = set()
_side_effect_limit = asyncio.Semaphore(10)


@dataclass
class _Registration:
    run_type: str
    attempt_number: int | None
    count: int = 0
    task: asyncio.Task[Any] | None = None
    acquired: bool = False
    lost: bool = False
    finalized: bool = False


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
        entry = _Registration(run_type, attempt_number)
        _registry[run_id] = entry
    entry.count += 1
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
    cancelled = False
    try:
        yield
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        release(cancelled)


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
        if not target.done() and all(
            entry.lost and not entry.finalized for entry in _registry.values() if entry.task is target
        ):
            target.cancel()


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
        await asyncio.sleep(INTERVAL_SECONDS)
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
    global _enabled, _wake, _side_effect_limit, _lease_write_lock
    if _enabled or settings.RUN_HEARTBEAT_TIMEOUT_MINUTES <= 0:
        return
    _enabled = True
    _wake = asyncio.Event()
    _lease_write_lock = asyncio.Lock()
    _held.clear()
    _side_effect_limit = asyncio.Semaphore(10)
    _tasks.add(asyncio.create_task(_heartbeat(app.DATABASE.run_execution_leases), name="run-lease-heartbeat"))
    _tasks.add(asyncio.create_task(_reaper_loop(reap_once), name="orphaned-run-reaper"))
    LOG.info("Run lease tasks started", owner_id=owner_id(), timeout_minutes=effective_timeout_minutes())


async def stop_run_lease_tasks() -> None:
    global _enabled
    _enabled = False
    # Stop the producers before collecting effects so a pass cannot enqueue work during shutdown.
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
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
