import asyncio

import pytest
from fastapi import BackgroundTasks

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.executor.background_task_executor import BackgroundTaskExecutor


@pytest.mark.asyncio
async def test_schedule_runs_work_without_background_tasks() -> None:
    """Without a FastAPI BackgroundTasks the work used to be dropped silently."""
    ran = asyncio.Event()

    async def work(value: str, *, keyword: str) -> None:
        assert value == "positional"
        assert keyword == "keyword"
        ran.set()

    BackgroundTaskExecutor()._schedule(None, work, "positional", keyword="keyword")

    await asyncio.wait_for(ran.wait(), timeout=1)


@pytest.mark.asyncio
async def test_schedule_defers_to_background_tasks_when_present() -> None:
    calls: list[tuple[str, str]] = []

    async def work(value: str, *, keyword: str) -> None:
        calls.append((value, keyword))

    background_tasks = BackgroundTasks()
    BackgroundTaskExecutor()._schedule(background_tasks, work, "positional", keyword="keyword")

    # Queued on the request's BackgroundTasks rather than started eagerly.
    assert calls == []
    await background_tasks()
    assert calls == [("positional", "keyword")]


@pytest.mark.asyncio
async def test_scheduled_run_cannot_clobber_the_callers_context() -> None:
    """The caller keeps running after dispatching; both must not write one context object."""
    ran = asyncio.Event()
    child_context: list[SkyvernContext | None] = []

    async def work() -> None:
        context = skyvern_context.current()
        child_context.append(context)
        assert context is not None
        # execute_workflow assigns this on whatever context it finds.
        context.generate_script = False
        context.task_id = "tsk_child"
        ran.set()

    parent = SkyvernContext(organization_id="org_1", task_id="tsk_parent", generate_script=True)
    with skyvern_context.scoped(parent):
        BackgroundTaskExecutor()._schedule(None, work)
        await asyncio.wait_for(ran.wait(), timeout=1)

        assert child_context[0] is not parent
        # The caller's context survives the child's writes.
        assert parent.task_id == "tsk_parent"
        assert parent.generate_script is True
        # ...while inherited values still reach the child.
        assert child_context[0].organization_id == "org_1"


@pytest.mark.asyncio
async def test_scheduled_task_is_retained_until_done() -> None:
    """A bare create_task reference can be garbage collected mid-flight."""
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    executor = BackgroundTaskExecutor()
    executor._schedule(None, work)

    await asyncio.sleep(0)
    assert len(executor._background_tasks) == 1

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert executor._background_tasks == set()
