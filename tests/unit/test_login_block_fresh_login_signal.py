"""Fresh-login discriminator for credential banking: a login block that actually signed in typed a
credential (INPUT_TEXT) or a 2FA code (VERIFICATION_CODE); one already logged in via the seeded
profile completes the check-if-logged-in goal with neither."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.webeye.actions.action_types import ActionType


def _service() -> object:
    from skyvern.forge.sdk.workflow.service import WorkflowService

    return WorkflowService()


def _result(wrb_id: str | None = "wrb_1") -> object:
    return SimpleNamespace(workflow_run_block_id=wrb_id)


def _wire(monkeypatch: pytest.MonkeyPatch, *, task_id: str | None, action_types: list[ActionType]) -> None:
    wrb = SimpleNamespace(task_id=task_id) if task_id is not None else SimpleNamespace(task_id=None)
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_block", AsyncMock(return_value=wrb))
    actions = [SimpleNamespace(action_type=at) for at in action_types]
    monkeypatch.setattr(app.DATABASE.tasks, "get_task_actions", AsyncMock(return_value=actions))


@pytest.mark.asyncio
async def test_input_text_action_is_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, task_id="t_1", action_types=[ActionType.CLICK, ActionType.INPUT_TEXT])
    assert await _service()._login_block_performed_fresh_login(
        workflow_run_block_result=_result(), organization_id="o_1"
    )


@pytest.mark.asyncio
async def test_verification_code_action_is_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, task_id="t_1", action_types=[ActionType.VERIFICATION_CODE])
    assert await _service()._login_block_performed_fresh_login(
        workflow_run_block_result=_result(), organization_id="o_1"
    )


@pytest.mark.asyncio
async def test_no_credential_action_is_not_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    # Already logged in via the seed: the agent only verifies and completes.
    _wire(monkeypatch, task_id="t_1", action_types=[ActionType.CLICK, ActionType.COMPLETE])
    assert not await _service()._login_block_performed_fresh_login(
        workflow_run_block_result=_result(), organization_id="o_1"
    )


@pytest.mark.asyncio
async def test_no_actions_is_not_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, task_id="t_1", action_types=[])
    assert not await _service()._login_block_performed_fresh_login(
        workflow_run_block_result=_result(), organization_id="o_1"
    )


@pytest.mark.asyncio
async def test_missing_block_id_is_not_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not await _service()._login_block_performed_fresh_login(
        workflow_run_block_result=_result(wrb_id=None), organization_id="o_1"
    )


@pytest.mark.asyncio
async def test_taskless_block_is_not_fresh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, task_id=None, action_types=[ActionType.INPUT_TEXT])
    assert not await _service()._login_block_performed_fresh_login(
        workflow_run_block_result=_result(), organization_id="o_1"
    )
