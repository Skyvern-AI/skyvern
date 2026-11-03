import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from skyvern.forge.sdk.models import Step
from skyvern.schemas.steps import StepPageDom

LOG = structlog.get_logger()

TURN_DOM_CAP_SECONDS = 2.0


class TurnDomCapture:
    """The page each Task V3 turn started from, read while the turn's model call is in flight.

    `start` runs when the turn's step exists and before its model call; `wait` runs after the call returns and
    before anything touches the page, so the stored page is the one the decision was made on.
    """

    def __init__(
        self,
        read_page: Callable[[], Awaitable[str | None]],
        store: Callable[[Step, bytes], str],
        cap_seconds: float = TURN_DOM_CAP_SECONDS,
    ) -> None:
        self._read_page = read_page
        self._store = store
        self._cap_seconds = cap_seconds
        self._task: asyncio.Task[tuple[StepPageDom, int, float]] | None = None
        self._turn = 0
        self._turn_output: StepPageDom | None = None
        self._last_digest: str | None = None
        self._last_artifact_id: str | None = None
        self._counts = {"stored": 0, "deduped": 0, "skipped": 0}
        self._wait_seconds = 0.0
        self._bytes = 0

    def start(self, step: Step | None, turn: int) -> None:
        self.cancel()
        self._turn = turn
        self._turn_output = None
        self._task = asyncio.create_task(self._capture(step))

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def wait(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        waited_from = time.monotonic()
        result = await task
        self._record(*result, wait_seconds=time.monotonic() - waited_from)

    def turn_output(self) -> StepPageDom | None:
        # A turn whose model call failed never reaches `wait`: keep a capture that already finished, stop one
        # still running before it stores into an archive the turn has handed off.
        task, self._task = self._task, None
        if task is not None:
            if task.done() and not task.cancelled():
                self._record(*task.result(), wait_seconds=0.0)
            else:
                task.cancel()
        return self._turn_output

    def _record(self, page_dom: StepPageDom, size: int, capture_seconds: float, *, wait_seconds: float) -> None:
        self._turn_output = page_dom
        self._counts[page_dom.status] += 1
        self._wait_seconds += wait_seconds
        self._bytes += size if page_dom.status == "stored" else 0
        LOG.info(
            f"taskv3 turn DOM {page_dom.status}",
            turn=self._turn,
            capture_seconds=round(capture_seconds, 3),
            wait_seconds=round(wait_seconds, 3),
            bytes=size,
            reason=page_dom.reason,
        )

    def totals(self) -> dict[str, Any]:
        return {
            "dom_captures": sum(self._counts.values()),
            "dom_stored": self._counts["stored"],
            "dom_deduped": self._counts["deduped"],
            "dom_skipped": self._counts["skipped"],
            "dom_wait_seconds": round(self._wait_seconds, 3),
            "dom_bytes": self._bytes,
        }

    async def _capture(self, step: Step | None) -> tuple[StepPageDom, int, float]:
        started = time.monotonic()
        if step is None:
            return StepPageDom(status="skipped", reason="no_turn_step"), 0, 0.0
        size = 0
        try:
            async with asyncio.timeout(self._cap_seconds):
                html = await self._read_page()
            if html is None:
                return StepPageDom(status="skipped", reason="no_working_page"), 0, time.monotonic() - started
            data = html.encode("utf-8")
            size = len(data)
            digest = hashlib.sha256(data).hexdigest()
            if digest == self._last_digest:
                page_dom = StepPageDom(status="deduped", artifact_id=self._last_artifact_id)
            else:
                artifact_id = self._store(step, data)
                self._last_digest, self._last_artifact_id = digest, artifact_id
                page_dom = StepPageDom(status="stored", artifact_id=artifact_id)
        except Exception as exc:
            page_dom = StepPageDom(status="skipped", reason=type(exc).__name__)
        return page_dom, size, time.monotonic() - started
