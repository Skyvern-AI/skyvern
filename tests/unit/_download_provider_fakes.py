"""Provider-neutral fakes for an action-scoped remote download source (``ActionDownloadSource``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError


class _FakeActionDownloadObservation:
    """Neutral provider observation: drops completed remote files into the run dir on its first poll,
    honoring the ``deadline`` (monotonic) contract strictly (a handler passing per-call ``timeout_seconds``
    materializes nothing -- the anti-terminal-cleanup RED)."""

    def __init__(self, files: list[tuple[str, bytes]], *, empty_polls: int = 0) -> None:
        self._pending = list(files)
        self._empty_polls = empty_polls
        self.poll_deadlines: list[float] = []
        self.poll_started_at: list[float] = []

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.poll_deadlines.append(deadline)
        self.poll_started_at.append(time.monotonic())
        if len(self.poll_deadlines) <= self._empty_polls:
            return
        destination_dir.mkdir(parents=True, exist_ok=True)
        for name, data in self._pending:
            (destination_dir / name).write_bytes(data)
        self._pending = []


class _RaisingActionDownloadObservation:
    """Provider observation whose poll raises a schema/validation error embedding a presigned URL, mirroring
    the real leak surface (the vendor list body is parsed into typed rows). When ``materialize_first`` is
    set, the first poll drops a completed file (so the handler reaches its final poll) and a later poll raises."""

    def __init__(self, error: BaseException, *, materialize_first: tuple[str, bytes] | None = None) -> None:
        self._error = error
        self._materialize_first = materialize_first
        self.calls = 0

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.calls += 1
        if self._materialize_first is not None and self.calls == 1:
            destination_dir.mkdir(parents=True, exist_ok=True)
            name, data = self._materialize_first
            (destination_dir / name).write_bytes(data)
            return
        raise self._error


class _StallingActionDownloadObservation:
    """Provider observation whose poll never returns on its own -- only the handler's deadline can end it, so
    if the handler polls it once a local artifact already accounts for the action the stalled provider would
    consume the whole download deadline; the fix must never poll it in that case."""

    def __init__(self) -> None:
        self.poll_calls = 0
        self.poll_deadlines: list[float] = []

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.poll_calls += 1
        self.poll_deadlines.append(deadline)
        await asyncio.Event().wait()


class _ActionDownloadSource:
    """Neutral provider source: hands back a fixed observation and records begin bookkeeping."""

    def __init__(
        self,
        observation: object,
        *,
        begin_error: Exception | None = None,
        on_begin: Callable[[], object] | None = None,
    ) -> None:
        self._observation = observation
        self._begin_error = begin_error
        self._on_begin = on_begin
        self.begin_calls = 0
        self.begin_deadline: float | None = None

    async def begin_observation(self, *, deadline: float) -> object:
        self.begin_calls += 1
        self.begin_deadline = deadline
        if self._on_begin is not None:
            self._on_begin()
        if self._begin_error is not None:
            raise self._begin_error
        return self._observation


def _secret_bearing_validation_error(presigned_url: str) -> ValidationError:
    class _Row(BaseModel):
        size: int

    try:
        _Row.model_validate({"size": presigned_url})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


class _FakeBrowserDownloadMonitor:
    """Stand-in for the run's CDP download monitor: an admitted-event counter and a pending flag that
    reads True for ``pending_checks`` calls, then runs ``on_settle`` once (e.g. a replay landing a file)."""

    def __init__(self, *, pending_checks: int = 0, on_settle: object = None) -> None:
        self.browser_download_event_count = 0
        self._pending_checks = pending_checks
        self._on_settle = on_settle
        self.pending_calls = 0

    def has_pending_download_activity(self) -> bool:
        self.pending_calls += 1
        if self.pending_calls <= self._pending_checks:
            return True
        if self._on_settle is not None:
            settle, self._on_settle = self._on_settle, None
            settle()  # type: ignore[operator]
        return False


class _GatedCleanupObservation:
    """Provider observation whose poll never lists anything and whose cancellation cleanup waits on ``gate``,
    so a caller that returns before awaiting the cancelled poll is seen returning with cleanup unfinished."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_finished = False
        self.poll_finished = False

    async def poll_and_materialize(self, *, destination_dir: Path, deadline: float) -> None:
        self.started.set()
        try:
            try:
                await asyncio.Event().wait()
            finally:
                self.cleanup_started.set()
                await self._gate.wait()
                self.cleanup_finished = True
        finally:
            self.poll_finished = True
