import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks

from skyvern.exceptions import SkyvernException
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.executor.background_task_executor import BackgroundTaskExecutor
from skyvern.forge.sdk.schemas.persistent_browser_sessions import FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE


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
async def test_execute_workflow_stamps_org_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        default_llm_key="CUSTOM_LLM_oat_smart",
        default_secondary_llm_key="CUSTOM_LLM_oat_fast",
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(sequential_credential_id=None)),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.initialize_skyvern_state_file",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.custom_llm_registry.load_custom_llm_configs_for_organization",
        AsyncMock(),
    )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with skyvern_context.scoped(SkyvernContext(organization_id="org_test")) as context:
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=organization,
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key=None,
            browser_session_id=None,
            block_labels=None,
            block_outputs=None,
        )

        assert context.org_default_llm_key == "CUSTOM_LLM_oat_smart"
        assert context.org_default_secondary_llm_key == "CUSTOM_LLM_oat_fast"


@pytest.mark.asyncio
async def test_execute_task_v2_stamps_org_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        default_llm_key="CUSTOM_LLM_oat_smart",
        default_secondary_llm_key="CUSTOM_LLM_oat_fast",
    )
    monkeypatch.setattr(app.DATABASE.organizations, "get_organization", AsyncMock(return_value=organization))
    monkeypatch.setattr(
        app.DATABASE.observer,
        "get_task_v2",
        AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr_test")),
    )
    monkeypatch.setattr(app.DATABASE.observer, "update_task_v2", AsyncMock())
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.initialize_skyvern_state_file",
        AsyncMock(),
    )
    load_custom_llms = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.custom_llm_registry.load_custom_llm_configs_for_organization",
        load_custom_llms,
    )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with skyvern_context.scoped(SkyvernContext(organization_id="org_test")) as context:
        await executor.execute_task_v2(
            request=None,
            background_tasks=None,
            organization_id="org_test",
            task_v2_id="task_v2_test",
            max_steps_override=None,
            browser_session_id=None,
        )

        assert context.org_default_llm_key == "CUSTOM_LLM_oat_smart"
        assert context.org_default_secondary_llm_key == "CUSTOM_LLM_oat_fast"

    load_custom_llms.assert_awaited_once_with(app.DATABASE, "org_test")


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


@pytest.mark.asyncio
async def test_execute_workflow_fails_closed_for_stamped_sequential_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_run = SimpleNamespace(sequential_credential_id="cred_sequential", browser_session_id=None)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=workflow_run),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", mark_failed)
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(SkyvernException, match="background executor"):
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=SimpleNamespace(organization_id="org_test"),
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key=None,
            browser_session_id=None,
            block_labels=None,
            block_outputs=None,
        )

    executor._schedule.assert_not_called()
    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert mark_failed.await_args.kwargs["cascade_children"] is True


@pytest.mark.asyncio
async def test_execute_workflow_closes_forced_session_before_credential_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run = SimpleNamespace(
        sequential_credential_id="cred_sequential",
        browser_session_id="pbs_forced",
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=workflow_run),
    )
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_persistent_browser_session",
        AsyncMock(return_value=SimpleNamespace(runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE)),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "close_session", close_session)
    monkeypatch.setattr(
        app.WORKFLOW_SERVICE,
        "mark_workflow_run_as_failed_if_not_final",
        AsyncMock(),
    )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(SkyvernException, match="background executor"):
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=SimpleNamespace(organization_id="org_test"),
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key=None,
            browser_session_id="pbs_forced",
            block_labels=None,
            block_outputs=None,
        )

    close_session.assert_awaited_once_with("org_test", "pbs_forced")
    executor._schedule.assert_not_called()


@pytest.mark.asyncio
async def test_execute_workflow_terminalizes_when_forced_session_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run = SimpleNamespace(
        sequential_credential_id="cred_sequential",
        browser_session_id="pbs_forced",
    )
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_persistent_browser_session",
        AsyncMock(return_value=SimpleNamespace(runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE)),
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "close_session",
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", mark_failed)
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(SkyvernException, match="background executor"):
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=SimpleNamespace(organization_id="org_test"),
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key=None,
            browser_session_id="pbs_forced",
            block_labels=None,
            block_outputs=None,
        )

    mark_failed.assert_awaited_once()
    executor._schedule.assert_not_called()
