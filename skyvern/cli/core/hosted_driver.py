"""The Playwright driver a cached hosted-MCP client shares across the tool calls that lease it."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from skyvern.exceptions import raise_local_extra_required
from skyvern.webeye.driver_connection import (
    close_driver_connection_on_transport_loss,
    driver_connection_closed,
    kill_driver_process,
)

if TYPE_CHECKING:
    from playwright.async_api import Playwright
    from playwright.async_api._context_manager import PlaywrightContextManager

LOG = structlog.get_logger(__name__)

DRIVER_STOP_TIMEOUT_SECONDS = 5
_CLEANUP_TASKS: set[asyncio.Task[None]] = set()
# Every owner with a start, reachable by the sweep and shutdown even after its client left the cache.
_OWNERS: set[HostedDriver] = set()


def owners() -> list[HostedDriver]:
    return list(_OWNERS)


class HostedDriver:
    """One Node driver per cached client: started once, leased by the calls using it, stopped only when idle."""

    def __init__(self) -> None:
        self._start: asyncio.Task[Playwright] | None = None
        self._manager: PlaywrightContextManager | None = None
        self._leases = 0
        self._idle_since = time.monotonic()
        self._started_at = 0.0

    @property
    def leases(self) -> int:
        return self._leases

    @property
    def owns_driver(self) -> bool:
        return self._start is not None

    @property
    def started(self) -> bool:
        return self._start is not None and self._start.done() and _usable(self._start)

    def acquire(self) -> None:
        self._leases += 1

    def release(self) -> None:
        self._leases -= 1
        if self._leases == 0:
            self._idle_since = time.monotonic()

    async def playwright(self) -> Playwright:
        self._idle_since = time.monotonic()
        start = self._start
        if start is not None and start.done() and not _usable(start):
            self._forget(start)
            start = None
        if start is None:
            try:
                from playwright.async_api import async_playwright  # noqa: PLC0415
            except ImportError as exc:
                raise_local_extra_required("Browser APIs", exc)
            # Created before the task so the owner holds the spawned child's handle even if every waiter is cancelled.
            self._manager = async_playwright()
            start = self._start = asyncio.create_task(_launch(self._manager))
            start.add_done_callback(_consume_start_error)
            self._started_at = time.monotonic()
            _OWNERS.add(self)
        # Shielded: a cancelled waiter must not cancel a start the other waiters share.
        return await asyncio.shield(start)

    async def reap(self, now: float, idle_seconds: float) -> bool:
        """Stop the driver if no call has used it for ``idle_seconds``. Returns whether a driver was reclaimed."""
        start = self._start
        if start is None:
            return False
        idle = not self._leases and now - self._idle_since >= idle_seconds
        if not start.done():
            # A start this old has no usable driver for its waiters to lose, so it is ended even while calls wait.
            if idle or now - self._started_at >= idle_seconds:
                # Never cancel a pending start: cancelling mid-spawn strands a child that no handle reaches. Killing
                # through the manager works once Playwright publishes the handle; until then a later sweep retries.
                kill_driver_process(self._manager)
            return False
        if not idle:
            return False
        manager = self._forget(start)
        if _usable(start):
            await _stop_within(start.result(), DRIVER_STOP_TIMEOUT_SECONDS)
        else:
            kill_driver_process(manager)
        return True

    async def close(self) -> bool:
        """Stop the driver regardless of leases. Returns whether one was reclaimed; a start that is still pending
        stays registered so a later sweep can reach it once its handle is published."""
        start = self._start
        if start is None:
            return False
        if not start.done():
            kill_driver_process(self._manager)
            await asyncio.wait({start}, timeout=DRIVER_STOP_TIMEOUT_SECONDS)
            if self._start is not start:
                return False
            if not start.done():
                kill_driver_process(self._manager)
                LOG.warning("Hosted MCP browser driver was still starting when it was closed")
                return False
        manager = self._forget(start)
        if _usable(start):
            await _stop_within(start.result(), DRIVER_STOP_TIMEOUT_SECONDS)
        else:
            kill_driver_process(manager)
        return True

    def _forget(self, start: asyncio.Task[Playwright]) -> PlaywrightContextManager | None:
        manager = self._manager
        self._start = None
        self._manager = None
        _OWNERS.discard(self)
        if not _usable(start):
            # A failed or dead driver may still have a child; the kill is a no-op once it has exited.
            kill_driver_process(manager)
        return manager


async def _launch(manager: PlaywrightContextManager) -> Playwright:
    driver = await manager.start()
    close_driver_connection_on_transport_loss(driver)
    return driver


def _usable(start: asyncio.Task[Playwright]) -> bool:
    return not start.cancelled() and start.exception() is None and not driver_connection_closed(start.result())


def _consume_start_error(start: asyncio.Task[Playwright]) -> None:
    if not start.cancelled():
        start.exception()


def _retain(task: asyncio.Task[None]) -> None:
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_finish_retained)


def _finish_retained(task: asyncio.Task[None]) -> None:
    _CLEANUP_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        LOG.warning("Hosted MCP browser driver stop failed", error_type=type(task.exception()).__name__)


async def _stop_within(driver: Playwright, timeout: float) -> None:
    stop = asyncio.create_task(driver.stop())
    try:
        done, _ = await asyncio.wait({stop}, timeout=timeout)
    except asyncio.CancelledError:
        # The owner has already let go of this driver, so a cancelled sweep must not leave its stop unfinished.
        kill_driver_process(driver)
        _retain(stop)
        raise
    if stop not in done:
        # Signals only this driver's own child process; the stop then completes once the kill lands.
        kill_driver_process(driver)
        _retain(stop)
        return
    error = stop.exception()
    if error is not None:
        LOG.warning("Failed to stop a hosted MCP browser driver", error_type=type(error).__name__)
        # A stop that raised may have left the process running.
        kill_driver_process(driver)
