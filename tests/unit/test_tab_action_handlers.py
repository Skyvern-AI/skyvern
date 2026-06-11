"""Tests for the NEW_TAB / SWITCH_TAB action handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.webeye.actions import actions
from skyvern.webeye.actions.handler import handle_new_tab_action, handle_switch_tab_action
from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess


def _task() -> MagicMock:
    task = MagicMock()
    task.task_id = "tsk_test"
    task.workflow_run_id = None
    return task


def _mock_app(browser_state: MagicMock) -> MagicMock:
    mock_app = MagicMock()
    # get_for_task is synchronous and returns the browser state for the task.
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    return mock_app


@pytest.mark.asyncio
async def test_new_tab_opens_navigates_pins_and_stops_batch() -> None:
    new_page = MagicMock()
    new_page.bring_to_front = AsyncMock()
    browser_state = MagicMock()
    browser_state.new_page = AsyncMock(return_value=new_page)
    browser_state.navigate_to_url = AsyncMock()
    browser_state.set_active_page = AsyncMock()

    action = actions.NewTabAction(url="https://example.test/page")
    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_new_tab_action(action, MagicMock(), MagicMock(), _task(), MagicMock())

    assert len(result) == 1
    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    browser_state.navigate_to_url.assert_awaited_once_with(page=new_page, url="https://example.test/page")
    browser_state.set_active_page.assert_awaited_once_with(new_page)


@pytest.mark.asyncio
async def test_new_tab_closes_tab_and_fails_when_navigation_fails() -> None:
    new_page = MagicMock()
    new_page.close = AsyncMock()
    browser_state = MagicMock()
    browser_state.new_page = AsyncMock(return_value=new_page)
    browser_state.navigate_to_url = AsyncMock(side_effect=Exception("net::ERR_NAME_NOT_RESOLVED"))
    browser_state.set_active_page = AsyncMock()

    action = actions.NewTabAction(url="https://does-not-exist.test")
    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_new_tab_action(action, MagicMock(), MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionFailure)
    # The failed/blank tab must be closed so the next scrape doesn't fail the task.
    new_page.close.assert_awaited_once()
    browser_state.set_active_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_tab_pins_target_and_stops_batch() -> None:
    page0, page1 = MagicMock(), MagicMock()
    page1.bring_to_front = AsyncMock()
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page0, page1])
    browser_state.set_active_page = AsyncMock()

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_switch_tab_action(
            actions.SwitchTabAction(tab_index=1), MagicMock(), MagicMock(), _task(), MagicMock()
        )

    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    browser_state.set_active_page.assert_awaited_once_with(page1)


@pytest.mark.asyncio
async def test_switch_tab_out_of_range_fails_without_stopping_step() -> None:
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[MagicMock()])
    browser_state.set_active_page = AsyncMock()

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_switch_tab_action(
            actions.SwitchTabAction(tab_index=5), MagicMock(), MagicMock(), _task(), MagicMock()
        )

    assert isinstance(result[0], ActionFailure)
    assert result[0].stop_execution_on_failure is False
    browser_state.set_active_page.assert_not_awaited()
