import asyncio

import pytest
from structlog.testing import capture_logs

from skyvern.utils.stall_watch import log_if_stalled


async def _waits_on_the_browser(fut: asyncio.Future[str]) -> str:
    return await fut


async def _run_step(fut: asyncio.Future[str], first_after_seconds: float) -> str:
    with log_if_stalled(
        "Agent step still running",
        first_after_seconds=first_after_seconds,
        repeat_every_seconds=60,
        step_id="stp_1",
    ):
        return await _waits_on_the_browser(fut)


@pytest.mark.asyncio
async def test_a_stalled_step_logs_the_call_it_is_waiting_in() -> None:
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    with capture_logs() as logs:
        step = asyncio.create_task(_run_step(fut, first_after_seconds=0.01))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if logs:
                break
        [stall] = [log for log in logs if log["event"] == "Agent step still running"]
        assert stall["step_id"] == "stp_1"
        assert stall["log_level"] == "warning"
        # The innermost awaiting function is the one the step is stuck in.
        assert stall["await_chain"][-2].endswith("_waits_on_the_browser")
        fut.set_result("done")
        assert await step == "done"


@pytest.mark.asyncio
async def test_a_step_that_finishes_in_time_logs_nothing_and_stops_its_watcher() -> None:
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("done")
    with capture_logs() as logs:
        assert await _run_step(fut, first_after_seconds=60) == "done"
        await asyncio.sleep(0)
    assert logs == []
    assert asyncio.all_tasks() == {asyncio.current_task()}
