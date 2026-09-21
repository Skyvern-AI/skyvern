"""Tests for the NEW_TAB / SWITCH_TAB / CLOSE_PAGE action handlers."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import FailedToNavigateToUrl
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions import actions
from skyvern.webeye.actions.handler import (
    ActionHandler,
    handle_close_page_action,
    handle_new_tab_action,
    handle_switch_tab_action,
)
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from tests.unit.helpers import make_organization, make_step, make_task


def _task() -> MagicMock:
    task = MagicMock()
    task.task_id = "tsk_test"
    task.workflow_run_id = None
    return task


def _dead_blank_page(url: str = ":") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.main_frame.child_frames = []
    page.is_closed.return_value = False
    page.close = AsyncMock()
    return page


def _survivor_page() -> MagicMock:
    page = MagicMock()
    page.url = "https://survivor.test/app"
    page.is_closed.return_value = False
    return page


def _mock_app(browser_state: MagicMock) -> MagicMock:
    mock_app = MagicMock()
    # get_for_task is synchronous and returns the browser state for the task.
    mock_app.BROWSER_MANAGER.get_for_task.return_value = browser_state
    return mock_app


@pytest.fixture(autouse=True)
def mock_navigation_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.webeye.actions.handler.validate_fetch_url", lambda url: url)


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
    # The browser state navigates through navigate_with_retry, which raises the typed error with the
    # driver's code already read; its message leads with Skyvern's sentence, not the driver's.
    browser_state.navigate_to_url = AsyncMock(
        side_effect=FailedToNavigateToUrl(
            url="https://does-not-exist.test",
            error_message="Page.goto: net::ERR_NAME_NOT_RESOLVED at https://does-not-exist.test/",
            nav_error_code="net::ERR_NAME_NOT_RESOLVED",
        )
    )
    browser_state.set_active_page = AsyncMock()

    action = actions.NewTabAction(url="https://does-not-exist.test")
    task = _task()
    task.task_id = "tsk_new_tab"
    context = SkyvernContext(run_id="wr_new_tab")
    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        with patch.object(skyvern_context, "current", return_value=context):
            result = await handle_new_tab_action(action, MagicMock(), MagicMock(), task, MagicMock())

    assert isinstance(result[0], ActionFailure)
    # The failed/blank tab must be closed so the next scrape doesn't fail the task.
    new_page.close.assert_awaited_once()
    browser_state.set_active_page.assert_not_awaited()
    # Opening a tab navigates, so its failure carries the driver's verdict like any other.
    assert context.task_nav_error_codes == {"tsk_new_tab": "net::ERR_NAME_NOT_RESOLVED"}


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


@pytest.mark.asyncio
async def test_close_page_without_index_closes_current_and_stops_batch() -> None:
    current_page = MagicMock()
    current_page.close = AsyncMock()

    result = await handle_close_page_action(actions.ClosePageAction(), current_page, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    current_page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_page_with_index_closes_target_tab_not_current() -> None:
    current_page = MagicMock()
    current_page.close = AsyncMock()
    page0, page1, page2 = MagicMock(), MagicMock(), MagicMock()
    page2.close = AsyncMock()
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[page0, page1, page2])

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_close_page_action(
            actions.ClosePageAction(tab_index=2), current_page, MagicMock(), _task(), MagicMock()
        )

    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    page2.close.assert_awaited_once()
    current_page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_page_with_index_can_close_the_current_tab() -> None:
    # tab_index may point at the current tab itself; closing it must still succeed and stop the
    # batch so the next step re-scrapes against whatever page becomes active.
    current_page = MagicMock()
    current_page.close = AsyncMock()
    other_page = MagicMock()
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[current_page, other_page])

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_close_page_action(
            actions.ClosePageAction(tab_index=0), current_page, MagicMock(), _task(), MagicMock()
        )

    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    current_page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_page_out_of_range_fails_without_stopping_step() -> None:
    current_page = MagicMock()
    current_page.close = AsyncMock()
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[MagicMock()])

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        result = await handle_close_page_action(
            actions.ClosePageAction(tab_index=9), current_page, MagicMock(), _task(), MagicMock()
        )

    assert isinstance(result[0], ActionFailure)
    assert result[0].stop_execution_on_failure is False
    current_page.close.assert_not_awaited()


# ---- Empty-page recovery guard on the internal-recovery close -------------------------------


@pytest.mark.asyncio
async def test_recovery_close_closes_page_still_dead_blank_with_survivor() -> None:
    dead = _dead_blank_page(":")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[dead, _survivor_page()])
    action = actions.ClosePageAction(is_internal_recovery=True)

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        with patch.object(skyvern_context, "current", return_value=SkyvernContext(run_id="wr")):
            result = await handle_close_page_action(action, dead, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionSuccess)
    assert result[0].skip_remaining_actions is True
    dead.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_close_declines_when_page_no_longer_blank() -> None:
    # The working page committed a real navigation during the sub-second window: decline, don't close.
    live = _dead_blank_page("https://real.test/now")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[live, _survivor_page()])
    action = actions.ClosePageAction(is_internal_recovery=True)

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        with patch.object(skyvern_context, "current", return_value=SkyvernContext(run_id="wr")):
            result = await handle_close_page_action(action, live, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionFailure)
    assert result[0].stop_execution_on_failure is False
    live.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_close_declines_when_no_other_valid_page() -> None:
    dead = _dead_blank_page(":")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[dead])
    action = actions.ClosePageAction(is_internal_recovery=True)

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        with patch.object(skyvern_context, "current", return_value=SkyvernContext(run_id="wr")):
            result = await handle_close_page_action(action, dead, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionFailure)
    dead.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_close_declines_when_only_other_page_is_not_http_survivor() -> None:
    # The healthy HTTP(S) page used at planning time closed/navigated to an error during the window,
    # leaving only another dead page (a chrome-error tab). Execution-time revalidation must fail
    # closed on the missing healthy survivor rather than close the current page and strand the run.
    dead = _dead_blank_page(":")
    other_dead = _dead_blank_page("chrome-error://chromewebdata/")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[dead, other_dead])
    action = actions.ClosePageAction(is_internal_recovery=True)

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        with patch.object(skyvern_context, "current", return_value=SkyvernContext(run_id="wr")):
            result = await handle_close_page_action(action, dead, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionFailure)
    assert result[0].stop_execution_on_failure is False
    dead.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_close_declines_when_dead_page_holds_download_claim() -> None:
    dead = _dead_blank_page(":")
    browser_state = MagicMock()
    browser_state.list_valid_pages = AsyncMock(return_value=[dead, _survivor_page()])
    task = _task()
    context = SkyvernContext(run_id="wr", task_id=task.task_id)
    context.record_download_popup_claim(task.task_id, dead)
    action = actions.ClosePageAction(is_internal_recovery=True)

    with patch("skyvern.webeye.actions.handler.app", _mock_app(browser_state)):
        with patch.object(skyvern_context, "current", return_value=context):
            result = await handle_close_page_action(action, dead, MagicMock(), task, MagicMock())

    assert isinstance(result[0], ActionFailure)
    dead.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_close_is_not_gated_by_blank_recovery_guard() -> None:
    # A user-emitted close (marker false) skips the recovery recheck entirely and closes as before,
    # even on a page that happens to be blank.
    dead = _dead_blank_page(":")

    result = await handle_close_page_action(actions.ClosePageAction(), dead, MagicMock(), _task(), MagicMock())

    assert isinstance(result[0], ActionSuccess)
    dead.close.assert_awaited_once()


def _handle_action_deps(browser_state: MagicMock) -> MagicMock:
    app_mock = _mock_app(browser_state)
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()
    app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=MagicMock(action_id="a1"))
    return app_mock


@pytest.mark.asyncio
async def test_handle_action_preserves_download_claim_for_recovery_close() -> None:
    # The action wrapper must NOT retire the dead page's popup claim for an internal-recovery close,
    # so the handler guard can still read it. _handle_action is stubbed to isolate the wrapper.
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="tsk_recovery", workflow_run_id="wr")
    step = make_step(now, task, step_id="step-x", status=StepStatus.running, order=0, output=None)
    dead = _dead_blank_page(":")
    context = SkyvernContext(task_id=task.task_id)
    context.record_download_popup_claim(task.task_id, dead)
    action = actions.ClosePageAction(is_internal_recovery=True)

    with (
        patch.object(ActionHandler, "_handle_action", AsyncMock(return_value=[ActionSuccess()])),
        patch("skyvern.webeye.actions.handler.app", _handle_action_deps(MagicMock())),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=context),
    ):
        await ActionHandler.handle_action(MagicMock(), task, step, dead, action)

    assert context.has_download_popup_claim(task.task_id, dead) is True


@pytest.mark.asyncio
async def test_handle_action_retires_download_claim_for_user_close() -> None:
    # Control: a user-emitted close is still treated as accepting the page as navigation state, so
    # the wrapper retires its stale claim. Proves the bypass is marker-gated, not blanket close_page.
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="tsk_user", workflow_run_id="wr")
    step = make_step(now, task, step_id="step-x", status=StepStatus.running, order=0, output=None)
    dead = _dead_blank_page(":")
    context = SkyvernContext(task_id=task.task_id)
    context.record_download_popup_claim(task.task_id, dead)
    action = actions.ClosePageAction()

    with (
        patch.object(ActionHandler, "_handle_action", AsyncMock(return_value=[ActionSuccess()])),
        patch("skyvern.webeye.actions.handler.app", _handle_action_deps(MagicMock())),
        patch("skyvern.webeye.actions.handler.skyvern_context.current", return_value=context),
    ):
        await ActionHandler.handle_action(MagicMock(), task, step, dead, action)

    assert context.has_download_popup_claim(task.task_id, dead) is False


@pytest.mark.asyncio
async def test_recovery_dispatch_preserves_late_reservation_until_release() -> None:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), task_id="task-reserved")
    step = make_step(now, task, step_id="step-reserved", status=StepStatus.running, order=0, output=None)
    dead = _dead_blank_page("about:blank")
    context = SkyvernContext(task_id=task.task_id)
    context.record_download_popup_late_candidate(task.task_id, dead)
    state = MagicMock()
    state.list_valid_pages = AsyncMock(return_value=[dead, _survivor_page()])

    async def dispatch(**kwargs: Any) -> list[ActionResult]:
        return await handle_close_page_action(
            kwargs["action"], kwargs["page"], kwargs["scraped_page"], kwargs["task"], kwargs["step"]
        )

    with (
        patch.object(ActionHandler, "_handle_action", side_effect=dispatch),
        patch("skyvern.webeye.actions.handler.app", _handle_action_deps(state)),
        patch.object(skyvern_context, "current", return_value=context),
    ):
        result = await ActionHandler.handle_action(
            MagicMock(), task, step, dead, actions.ClosePageAction(is_internal_recovery=True)
        )
        assert isinstance(result[0], ActionFailure)
        dead.close.assert_not_awaited()
        assert context.has_download_popup_claim(task.task_id, dead)
        context.clear_download_popup_claims(task.task_id)
        result = await ActionHandler.handle_action(
            MagicMock(), task, step, dead, actions.ClosePageAction(is_internal_recovery=True)
        )
        assert isinstance(result[0], ActionSuccess)
        dead.close.assert_awaited_once()
