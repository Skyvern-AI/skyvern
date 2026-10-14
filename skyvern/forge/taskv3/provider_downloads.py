"""Read a vendor-kept browser download back into a v3 complete_on_download block's run download directory.

A vendor session can keep the browser's own save while the out-of-band replay of the URL fails, so the
completion probe reads the provider copy and lets the existing finalize path judge what lands."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any, Protocol

import structlog

from skyvern.constants import BROWSER_DOWNLOADING_SUFFIX
from skyvern.webeye.browser_artifacts import ActionDownloadObservation, ActionDownloadSource

LOG = structlog.get_logger()

PROVIDER_BASELINE_TIMEOUT_SECONDS = 10.0
# The provider lists a file only once the browser finished saving it; a block's download_timeout, when
# set, replaces this per-download wait.
PROVIDER_DOWNLOAD_WAIT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 1.0
# Lets a downloadWillBegin raised by the action that just returned reach the monitor's counter.
EVENT_ADMISSION_GRACE_SECONDS = 0.25


class _DownloadMonitor(Protocol):
    @property
    def browser_download_event_count(self) -> int: ...

    def has_pending_download_activity(self) -> bool: ...


def _download_monitor(browser_context: Any) -> _DownloadMonitor | None:
    # Attached dynamically to the Playwright context by the browser factory.
    monitor = getattr(browser_context, "_skyvern_cdp_download_interceptor", None) if browser_context else None
    count = getattr(monitor, "browser_download_event_count", None)
    if not isinstance(count, int) or not callable(getattr(monitor, "has_pending_download_activity", None)):
        return None
    return monitor


def _completed_names(directory: Path) -> set[str]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return set()
    return {entry.name for entry in entries if entry.is_file() and not entry.name.endswith(BROWSER_DOWNLOADING_SUFFIX)}


class ProviderDownloadRecovery:
    """One v3 block's provider read-back. The download probe awaits it serially, never concurrently."""

    def __init__(
        self,
        observation: ActionDownloadObservation,
        download_dir: Path,
        monitor: _DownloadMonitor | None,
    ) -> None:
        self._observation = observation
        self._download_dir = download_dir
        self._monitor = monitor
        self._events_seen = monitor.browser_download_event_count if monitor is not None else 0
        self._local_seen = _completed_names(download_dir)
        self._owed_read = False

    @classmethod
    async def begin(
        cls,
        source: ActionDownloadSource | None,
        *,
        download_dir: Path,
        browser_context: Any,
        deadline: float,
    ) -> ProviderDownloadRecovery | None:
        """Freeze the provider baseline before the block acts, or ``None`` to leave every download path as is."""
        if source is None:
            return None
        try:
            observation = await source.begin_observation(deadline=deadline)
        except Exception as exc:
            # A provider error can embed a secret-bearing presigned URL, so only its type is logged.
            LOG.warning("Task V3 provider download baseline unavailable", error_type=type(exc).__name__)
            return None
        if observation is None:
            return None
        return cls(observation, download_dir, _download_monitor(browser_context))

    async def recover(
        self,
        *,
        staged: Collection[str],
        timeout: float,
        should_cancel: Callable[[], Awaitable[bool]],
    ) -> bool:
        """Read back files a download the browser began left with the provider; True only when ``should_cancel``
        fired. An admitted download leaves a read owed until a file lands, and completion stays any-file."""
        if timeout <= 0:
            return False
        deadline = time.monotonic() + timeout
        try:
            if self._local_delta(staged):
                self._settle_read()
                return False
            if self._monitor is None:
                # Nothing reports whether a download began, so read the provider once per probe.
                if await self._poll(deadline, should_cancel):
                    return True
                self._log_read_back(staged)
                return False
            await asyncio.sleep(min(EVENT_ADMISSION_GRACE_SECONDS, max(0.0, deadline - time.monotonic())))
            new_event = self._monitor.browser_download_event_count != self._events_seen
            if new_event:
                self._events_seen = self._monitor.browser_download_event_count
                self._owed_read = True
            if not self._owed_read:
                return False
            # The monitor's own capture (a replay that works) runs first; reading the provider while it
            # is still in flight could publish the same download twice.
            while self._monitor.has_pending_download_activity():
                if not new_event or time.monotonic() >= deadline:
                    return False
                if await should_cancel():
                    return True
                await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
            while True:
                if self._local_delta(staged):
                    self._settle_read()
                    return False
                if time.monotonic() >= deadline:
                    return False
                if await self._poll(deadline, should_cancel):
                    return True
                if self._log_read_back(staged):
                    self._settle_read()
                    return False
                if not new_event or time.monotonic() >= deadline:
                    return False
                if await should_cancel():
                    return True
                await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
        finally:
            self._local_seen = _completed_names(self._download_dir)

    def _settle_read(self) -> None:
        # A file landed locally, so every event admitted so far is accounted for; a later probe must not
        # read one of them as new and publish the provider's copy of the same download beside it.
        self._owed_read = False
        if self._monitor is not None:
            self._events_seen = self._monitor.browser_download_event_count

    def _log_read_back(self, staged: Collection[str]) -> bool:
        landed = self._local_delta(staged)
        if landed:
            LOG.info("Task V3 provider download read back", file_count=len(landed))
        return bool(landed)

    def _local_delta(self, staged: Collection[str]) -> set[str]:
        return _completed_names(self._download_dir) - self._local_seen - set(staged)

    async def _poll(self, deadline: float, should_cancel: Callable[[], Awaitable[bool]]) -> bool:
        poll = asyncio.ensure_future(
            self._observation.poll_and_materialize(destination_dir=self._download_dir, deadline=deadline)
        )
        try:
            while True:
                remaining = deadline - time.monotonic()
                done, _ = await asyncio.wait({poll}, timeout=max(0.0, min(POLL_INTERVAL_SECONDS, remaining)))
                if poll in done or remaining <= 0:
                    return False
                if await should_cancel():
                    return True
        finally:
            await _join(poll)


async def _join(poll: asyncio.Future[None]) -> None:
    """Cancel ``poll`` and wait for its own (cooperative, not deadline-bound) cleanup before returning.
    A cancellation of the caller while waiting is re-raised once the poll is done."""
    poll.cancel()
    cancellation: asyncio.CancelledError | None = None
    while not poll.done():
        try:
            await asyncio.wait({poll})
        except asyncio.CancelledError as caught:
            cancellation = caught
    if not poll.cancelled() and (exc := poll.exception()) is not None:
        # A provider error can embed a secret-bearing presigned URL, so only its type is logged.
        LOG.warning("Task V3 provider download poll failed", error_type=type(exc).__name__)
    if cancellation is not None:
        raise cancellation
