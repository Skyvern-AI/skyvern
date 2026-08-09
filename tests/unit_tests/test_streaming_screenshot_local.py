from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit_tests._stub_streaming import import_with_stubs

screenshot = import_with_stubs("skyvern.forge.sdk.routes.streaming.screenshot")


@pytest.mark.asyncio
async def test_run_local_screencast_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = object()
    browser_state = object()
    wait_for_running = AsyncMock(return_value=None)
    check_finalized = AsyncMock(return_value=False)
    get_current_status = AsyncMock(return_value="completed")
    wait_for_browser_state_mock = AsyncMock(return_value=browser_state)
    start_screencast_loop_mock = AsyncMock()
    send_status_mock = AsyncMock()
    monkeypatch.setattr(screenshot, "wait_for_browser_state", wait_for_browser_state_mock)
    monkeypatch.setattr(screenshot, "start_screencast_loop", start_screencast_loop_mock)
    monkeypatch.setattr(screenshot, "_send_status", send_status_mock)

    await screenshot._run_local_screencast(
        websocket=websocket,
        entity_id="task_123",
        entity_type="task",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
        organization_id="org_123",
        get_workflow_run_id=lambda: "wr_123",
    )

    wait_for_running.assert_awaited_once()
    wait_for_browser_state_mock.assert_awaited_once_with(
        "task_123", "task", workflow_run_id="wr_123", organization_id="org_123"
    )
    start_screencast_loop_mock.assert_awaited_once_with(
        websocket=websocket,
        browser_state=browser_state,
        entity_id="task_123",
        entity_type="task",
        check_finalized=check_finalized,
        workflow_run_id="wr_123",
        organization_id="org_123",
    )
    get_current_status.assert_awaited_once()
    send_status_mock.assert_awaited_once_with(websocket, "task_id", "task_123", "completed")


@pytest.mark.asyncio
async def test_run_local_screencast_timeout_when_browser_state_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = object()
    wait_for_running = AsyncMock(return_value=None)
    check_finalized = AsyncMock(return_value=False)
    get_current_status = AsyncMock(return_value="completed")
    wait_for_browser_state_mock = AsyncMock(return_value=None)
    start_screencast_loop_mock = AsyncMock()
    send_status_mock = AsyncMock()
    monkeypatch.setattr(screenshot, "wait_for_browser_state", wait_for_browser_state_mock)
    monkeypatch.setattr(screenshot, "start_screencast_loop", start_screencast_loop_mock)
    monkeypatch.setattr(screenshot, "_send_status", send_status_mock)

    await screenshot._run_local_screencast(
        websocket=websocket,
        entity_id="bs_123",
        entity_type="browser_session",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
        organization_id="org_123",
    )

    wait_for_running.assert_awaited_once()
    wait_for_browser_state_mock.assert_awaited_once_with(
        "bs_123",
        "browser_session",
        workflow_run_id=None,
        organization_id="org_123",
    )
    start_screencast_loop_mock.assert_not_awaited()
    get_current_status.assert_not_awaited()
    send_status_mock.assert_awaited_once_with(websocket, "browser_session_id", "bs_123", "timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entrypoint", "entity_id", "database_attr", "getter"),
    [
        ("_local_screencast_for_task", "tsk_123", "tasks", "get_task"),
        ("_local_screencast_for_workflow_run", "wr_123", "workflow_runs", "get_workflow_run"),
    ],
)
async def test_local_screencast_forwards_the_organization(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, entity_id: str, database_attr: str, getter: str
) -> None:
    """A browser held out of process is reachable only through an organization-scoped session
    lookup, so an entrypoint that drops the organization streams nothing."""
    finished = SimpleNamespace(status=SimpleNamespace(is_final=lambda: True), organization_id="org_123")
    monkeypatch.setattr(
        screenshot,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(**{database_attr: SimpleNamespace(**{getter: AsyncMock(return_value=finished)})})
        ),
    )
    run_local_screencast_mock = AsyncMock()
    monkeypatch.setattr(screenshot, "_run_local_screencast", run_local_screencast_mock)

    await getattr(screenshot, entrypoint)(object(), entity_id, "org_123")

    assert run_local_screencast_mock.await_args.kwargs["organization_id"] == "org_123"
