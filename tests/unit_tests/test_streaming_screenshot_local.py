from __future__ import annotations

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
        id_key="task_id",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
        get_workflow_run_id=lambda: "wr_123",
    )

    wait_for_running.assert_awaited_once()
    wait_for_browser_state_mock.assert_awaited_once_with("task_123", "task", workflow_run_id="wr_123")
    start_screencast_loop_mock.assert_awaited_once_with(
        websocket=websocket,
        browser_state=browser_state,
        entity_id="task_123",
        entity_type="task",
        check_finalized=check_finalized,
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
        id_key="browser_session_id",
        wait_for_running=wait_for_running,
        check_finalized=check_finalized,
        get_current_status=get_current_status,
    )

    wait_for_running.assert_awaited_once()
    wait_for_browser_state_mock.assert_awaited_once_with(
        "bs_123",
        "browser_session",
        workflow_run_id=None,
    )
    start_screencast_loop_mock.assert_not_awaited()
    get_current_status.assert_not_awaited()
    send_status_mock.assert_awaited_once_with(websocket, "browser_session_id", "bs_123", "timeout")
