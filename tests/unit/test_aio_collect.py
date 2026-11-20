"""Unit tests for skyvern.forge.sdk.utils.aio.collect."""

from __future__ import annotations

import asyncio

import pytest

from skyvern.forge.sdk.utils import aio


def test_collect_preserves_task_order() -> None:
    async def run() -> list[int]:
        async def job(index: int) -> int:
            # Stagger completion so the tasks finish in a different order than the input.
            await asyncio.sleep(0.02 * (5 - index))
            return index

        return await aio.collect([asyncio.create_task(job(index)) for index in range(5)])

    assert asyncio.run(run()) == [0, 1, 2, 3, 4]


def test_collect_of_no_tasks_returns_empty_list() -> None:
    assert asyncio.run(aio.collect([])) == []


def test_collect_propagates_failure() -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    async def run() -> None:
        await aio.collect([asyncio.create_task(boom())])

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())
