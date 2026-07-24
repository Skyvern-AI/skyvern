import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ActionType, ClickAction, WaitAction
from skyvern.webeye.actions.handler import ActionHandler, _resolve_action_execution_timeout
from tests.unit.helpers import make_organization, make_step, make_task


def _rig() -> tuple:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now))
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
    scraped_page = MagicMock(id_to_element_dict={"el": {"id": "el"}})
    return task, step, scraped_page, MagicMock()


def _app_mock() -> MagicMock:
    app_mock = MagicMock()
    app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()
    return app_mock


@pytest.mark.asyncio
async def test_hung_action_fails_within_bounded_timeout() -> None:
    task, step, scraped_page, page = _rig()

    async def hung_handler(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    action = ClickAction(element_id="el")
    with (
        patch("skyvern.webeye.actions.handler.app", _app_mock()),
        patch.dict(ActionHandler._handled_action_types, {ActionType.CLICK: hung_handler}),
        patch.dict(ActionHandler._setup_action_types, {}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {}, clear=True),
        patch("skyvern.webeye.actions.handler.settings.BROWSER_ACTION_MAX_EXECUTION_SECONDS", 0.05, create=True),
    ):
        results = await asyncio.wait_for(
            ActionHandler._handle_action(scraped_page=scraped_page, task=task, step=step, page=page, action=action),
            timeout=2,
        )
    assert len(results) == 1
    assert results[0].success is False
    assert results[0].exception_type == "ActionExecutionTimeout"
    assert "timed out" in (results[0].exception_message or "").lower()


@pytest.mark.asyncio
async def test_handler_raised_timeout_error_keeps_original_exception_type() -> None:
    task, step, scraped_page, page = _rig()

    async def raising_handler(*args: object, **kwargs: object) -> None:
        raise TimeoutError("inner playwright-ish timeout")

    action = ClickAction(element_id="el")
    with (
        patch("skyvern.webeye.actions.handler.app", _app_mock()),
        patch.dict(ActionHandler._handled_action_types, {ActionType.CLICK: raising_handler}),
        patch.dict(ActionHandler._setup_action_types, {}, clear=True),
        patch.dict(ActionHandler._teardown_action_types, {}, clear=True),
        patch("skyvern.webeye.actions.handler.settings.BROWSER_ACTION_MAX_EXECUTION_SECONDS", 60, create=True),
    ):
        results = await asyncio.wait_for(
            ActionHandler._handle_action(scraped_page=scraped_page, task=task, step=step, page=page, action=action),
            timeout=2,
        )
    assert len(results) == 1
    assert results[0].success is False
    assert results[0].exception_type == "TimeoutError"


def test_wait_action_extends_execution_budget() -> None:
    with patch("skyvern.webeye.actions.handler.settings.BROWSER_ACTION_MAX_EXECUTION_SECONDS", 100, create=True):
        assert _resolve_action_execution_timeout(WaitAction(seconds=30)) == 130
        assert _resolve_action_execution_timeout(ClickAction(element_id="el")) == 100
