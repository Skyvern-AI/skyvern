"""Caller-attribution wiring for ``skyvern.agent.complete_verify``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skyvern.webeye.actions import actions
from skyvern.webeye.actions.handler import handle_complete_action


@pytest.mark.asyncio
async def test_handle_complete_action_forwards_complete_action_forced_to_complete_verify() -> None:
    action = actions.CompleteAction(verified=False)
    page = MagicMock()
    scraped_page = MagicMock()
    task = MagicMock()
    task.navigation_goal = "Submit the form"
    task.workflow_run_id = "wr_456"
    step = MagicMock()

    captured: dict = {}

    async def fake_complete_verify(*_args, **kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.is_terminate = False
        result.thoughts = "ok"
        return result

    with patch("skyvern.webeye.actions.handler.app.agent.complete_verify", side_effect=fake_complete_verify):
        await handle_complete_action(action, page, scraped_page, task, step)

    assert captured.get("verification_trigger") == "complete_action_forced"
