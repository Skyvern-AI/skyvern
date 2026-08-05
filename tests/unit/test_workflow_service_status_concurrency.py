import asyncio
from collections.abc import Awaitable, Callable

import pytest

from skyvern.forge.sdk.workflow.service import WorkflowService


@pytest.mark.asyncio
async def test_gather_with_max_in_flight_limits_parallelism() -> None:
    service = WorkflowService()
    state = {"active": 0, "max_active": 0}
    lock = asyncio.Lock()

    async def _task(_: int) -> int:
        async with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])

        await asyncio.sleep(0.01)

        async with lock:
            state["active"] -= 1

        return _

    def _operation_factory(value: int) -> Callable[[], Awaitable[int]]:
        return lambda: _task(value)

    results: tuple[int, ...] = await service._gather_with_max_in_flight(
        tuple(_operation_factory(i) for i in range(6)),
        max_in_flight=2,
    )

    assert results == (0, 1, 2, 3, 4, 5)
    assert state["max_active"] <= 2

    state = {"active": 0, "max_active": 0}
    results = await service._gather_with_max_in_flight(
        tuple(_operation_factory(i) for i in range(4)),
        max_in_flight=1,
    )
    assert results == (0, 1, 2, 3)
    assert state["max_active"] <= 1
