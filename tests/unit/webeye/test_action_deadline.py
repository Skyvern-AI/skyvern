from __future__ import annotations

import asyncio
import gc
import warnings

import pytest

from skyvern.exceptions import ActionDeadlineExceeded
from skyvern.webeye import action_deadline
from skyvern.webeye.action_deadline import cancellable_driver_call


@pytest.mark.asyncio
async def test_a_caller_cancelled_before_the_call_takes_its_first_step_still_starts_it() -> None:
    """The OTel asyncio instrumentor (cloud workers) drops the coroutine of a task cancelled before its first step,
    and CPython reports that as an error-level "never awaited" warning on every such cancellation."""
    entered = asyncio.Event()

    async def call() -> None:
        entered.set()
        await asyncio.sleep(30)

    instrumentation = pytest.importorskip("opentelemetry.instrumentation.asyncio")
    instrumentor = instrumentation.AsyncioInstrumentor()
    instrumentor.instrument()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0):
                    await cancellable_driver_call(call)
            for _ in range(5):
                await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
    finally:
        instrumentor.uninstrument()

    assert [str(entry.message) for entry in caught if "never awaited" in str(entry.message)] == []
    assert entered.is_set()


@pytest.mark.asyncio
async def test_each_deadline_rearm_reaches_a_call_that_ignored_the_first_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(action_deadline, "DEADLINE_REARM_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(action_deadline, "ACTION_DEADLINE_HEADROOM_MS", 0)
    cancels = 0

    async def call() -> None:
        nonlocal cancels
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancels += 1
                if cancels > 1:
                    raise

    with pytest.raises(ActionDeadlineExceeded):
        async with action_deadline.under_action_deadline(budget_ms=50):
            await cancellable_driver_call(call)

    assert cancels == 2
