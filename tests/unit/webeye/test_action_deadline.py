from __future__ import annotations

import asyncio

import pytest

from skyvern.exceptions import ActionDeadlineExceeded
from skyvern.webeye import action_deadline
from skyvern.webeye.action_deadline import cancellable_driver_call


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
