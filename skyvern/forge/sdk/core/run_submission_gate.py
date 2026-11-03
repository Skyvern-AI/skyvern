import asyncio
import functools
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import HTTPException, status
from opentelemetry import metrics, trace

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.request_logging import mark_request_load_shed
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.metric_attributes import task_metric_attributes

LOG = structlog.get_logger()

RETRY_AFTER_SECONDS = 5
SLOT_WAIT_SPAN_ATTRIBUTE = "run_submission.slot_wait_ms"
DISABLE_RUN_SUBMISSION_GATE_FLAG = "DISABLE_RUN_SUBMISSION_GATE"
RUN_SUBMISSION_METRIC_PREFIX = "skyvern.run_submission."
RUN_SUBMISSION_REJECTED_COUNTER = f"{RUN_SUBMISSION_METRIC_PREFIX}rejected"
RUN_SUBMISSION_IN_FLIGHT_GAUGE = f"{RUN_SUBMISSION_METRIC_PREFIX}in_flight"
RUN_SUBMISSION_WAITING_GAUGE = f"{RUN_SUBMISSION_METRIC_PREFIX}waiting"
_KILL_SWITCH_LOOKUP_TIMEOUT_SECONDS = 1.0
OPENAPI_SHED_RESPONSE: dict[str, Any] = {
    "description": "The server is busy dispatching other runs. No run was created; retry after Retry-After seconds.",
    "headers": {"Retry-After": {"description": "Seconds to wait before retrying", "schema": {"type": "integer"}}},
}


@functools.cache
def _rejected_counter() -> Any | None:
    # Cached on first use rather than at import: the meter must be created after otel_setup has
    # installed the real MeterProvider or every add() is a no-op.
    if not settings.OTEL_METRICS_ENABLED:
        return None
    try:
        meter = metrics.get_meter("skyvern.run_submission_gate")
        return meter.create_counter(
            RUN_SUBMISSION_REJECTED_COUNTER,
            unit="{request}",
            description="Run submissions answered 503 because no dispatch slot freed in time",
        )
    except Exception as e:
        LOG.warning("Failed to initialize run submission rejection counter", error=str(e))
        return None


class RunSubmissionGate:
    """A dispatching submission holds a pooled connection for most of its life, so an unbounded burst
    on one process drains the pool for every route on it; a caller waiting here holds no connection."""

    def __init__(self, limit: int, wait_seconds: float) -> None:
        self._limit = limit
        self._wait_seconds = wait_seconds
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_flight = 0
        self._waiting = 0

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def waiting(self) -> int:
        return self._waiting

    def _semaphore_for_running_loop(self) -> asyncio.Semaphore:
        # A semaphore binds to the loop that first waits on it, and tests run each case on a new loop.
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self._limit)
            self._loop = loop
        return self._semaphore

    @asynccontextmanager
    async def slot(self, enforce: bool = True) -> AsyncIterator[None]:
        # Counted even when not enforced, so the in-flight gauge can size a limit before one is set.
        semaphore = await self._acquire() if enforce and self.enabled else None
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            if semaphore is not None:
                semaphore.release()

    async def _acquire(self) -> asyncio.Semaphore:
        semaphore = self._semaphore_for_running_loop()
        wait_started = time.monotonic()
        self._waiting += 1
        try:
            async with asyncio.timeout(self._wait_seconds):
                await semaphore.acquire()
        except TimeoutError:
            self._record_rejection()
            mark_request_load_shed()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The server is busy dispatching other runs. No run was created; retry this request.",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            ) from None
        finally:
            self._waiting -= 1
        trace.get_current_span().set_attribute(
            SLOT_WAIT_SPAN_ATTRIBUTE, round((time.monotonic() - wait_started) * 1000, 1)
        )
        return semaphore

    def _record_rejection(self) -> None:
        with contained_effect("record run submission rejection"):
            counter = _rejected_counter()
            if counter is not None:
                counter.add(1, task_metric_attributes())


_GATE = RunSubmissionGate(settings.RUN_SUBMISSION_MAX_CONCURRENCY, settings.RUN_SUBMISSION_SLOT_WAIT_SECONDS)


def _observe_in_flight(_options: Any) -> Iterable[metrics.Observation]:
    return [metrics.Observation(_GATE.in_flight, task_metric_attributes())]


def _observe_waiting(_options: Any) -> Iterable[metrics.Observation]:
    return [metrics.Observation(_GATE.waiting, task_metric_attributes())]


@functools.cache
def _register_gauges() -> None:
    # Registered on first use for the same reason as the counter, and once per process: the meter
    # reports a second instrument under the same name as a conflict.
    if not settings.OTEL_METRICS_ENABLED:
        return
    try:
        meter = metrics.get_meter("skyvern.run_submission_gate")
        meter.create_observable_gauge(
            RUN_SUBMISSION_IN_FLIGHT_GAUGE,
            callbacks=[_observe_in_flight],
            unit="{request}",
            description="Run submissions dispatching on this process, whether or not the gate is on",
        )
        meter.create_observable_gauge(
            RUN_SUBMISSION_WAITING_GAUGE,
            callbacks=[_observe_waiting],
            unit="{request}",
            description="Run submissions waiting for a dispatch slot on this process",
        )
    except Exception as e:
        LOG.warning("Failed to initialize run submission gauges", error=str(e))


async def _disabled_by_kill_switch(organization_id: str) -> bool:
    """Runtime off switch: if a fleet-wide dispatch slowdown (Temporal, Redis) holds every slot, the gate
    would shed every submission above the limit, and changing the setting needs a restart.
    Anything other than an explicit True leaves the setting in charge."""
    try:
        # A cache miss can read the flag snapshot from the database, which is the resource a burst exhausts.
        disabled = await asyncio.wait_for(
            app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached(
                DISABLE_RUN_SUBMISSION_GATE_FLAG,
                organization_id,
                properties={"organization_id": organization_id},
            ),
            _KILL_SWITCH_LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.warning(
            "Failed to check the run submission gate kill switch; the gate stays on",
            organization_id=organization_id,
            exc_info=True,
        )
        return False
    return disabled is True


@asynccontextmanager
async def run_submission_slot(organization_id: str) -> AsyncIterator[None]:
    _register_gauges()
    enforce = _GATE.enabled and not await _disabled_by_kill_switch(organization_id)
    async with _GATE.slot(enforce=enforce):
        yield
