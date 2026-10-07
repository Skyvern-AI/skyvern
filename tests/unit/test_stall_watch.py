import asyncio

import pytest
from structlog.testing import capture_logs

from skyvern.utils import stall_watch
from skyvern.utils.stall_watch import log_if_stalled, wait_for_task


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


async def _detach_that_holds_its_cancellation() -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_task_that_holds_its_cancellation_cannot_hold_the_waiter_or_outlive_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Far longer than the test waits: only the repeat on the next loop pass can end the task in time.
    monkeypatch.setattr(stall_watch, "ABANDONED_TASK_RECANCEL_SECONDS", 60)
    stuck = asyncio.create_task(_detach_that_holds_its_cancellation())
    deadline = asyncio.get_running_loop().time() + 0.05
    try:
        with capture_logs() as logs:
            waiter = asyncio.create_task(wait_for_task(stuck, deadline, "Verification did not finish", step_id="stp_1"))
            done, _ = await asyncio.wait({waiter}, timeout=5)

        assert waiter in done
        with pytest.raises(TimeoutError):
            waiter.result()
        [timed_out] = [log for log in logs if log["event"] == "Verification did not finish"]
        assert timed_out["step_id"] == "stp_1"
        assert any(frame.endswith("_detach_that_holds_its_cancellation") for frame in timed_out["await_chain"])
        # Abandoned, it is cancelled again rather than left to carry on after swallowing the first cancellation.
        await asyncio.wait({stuck}, timeout=5)
        assert stuck.cancelled()
    finally:
        stuck.cancel()
        await asyncio.gather(stuck, return_exceptions=True)
