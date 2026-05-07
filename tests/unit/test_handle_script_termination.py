"""Locks the bookkeeping contract for _handle_script_termination (SKY-9568):
IllegitCompleteScriptTermination -> BlockStatus.failed (so AI fallback fires);
plain ScriptTerminationException -> BlockStatus.terminated (no fallback)."""

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.exceptions import (
    IllegitCompleteScriptTermination,
    ScriptTerminationException,
)
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.workflows import BlockStatus
from skyvern.services.script_service import _handle_script_termination


@pytest.mark.asyncio
async def test_helper_writes_failed_for_illegit_complete():
    e = IllegitCompleteScriptTermination("Illegit complete, data={'error': '...'}")
    with patch(
        "skyvern.services.script_service._update_workflow_block",
        new_callable=AsyncMock,
    ) as mock_update:
        await _handle_script_termination(
            e,
            "task block",
            workflow_run_block_id="wrb_1",
            task_id="tsk_1",
            step_id="stp_1",
            cache_key="MyTaskBlock",
        )
        mock_update.assert_awaited_once()
        kwargs = mock_update.await_args.kwargs
        positional = mock_update.await_args.args
        assert positional[0] == "wrb_1"
        assert positional[1] == BlockStatus.failed
        assert kwargs["task_status"] == TaskStatus.failed
        assert kwargs["step_status"] == StepStatus.failed
        assert kwargs["failure_reason"] == "Illegit complete, data={'error': '...'}"


@pytest.mark.asyncio
async def test_helper_writes_terminated_for_plain_termination():
    e = ScriptTerminationException("Terminate called: no results found")
    with patch(
        "skyvern.services.script_service._update_workflow_block",
        new_callable=AsyncMock,
    ) as mock_update:
        await _handle_script_termination(
            e,
            "task block",
            workflow_run_block_id="wrb_2",
            task_id="tsk_2",
            step_id="stp_2",
            cache_key="MyTaskBlock",
        )
        mock_update.assert_awaited_once()
        kwargs = mock_update.await_args.kwargs
        positional = mock_update.await_args.args
        assert positional[0] == "wrb_2"
        assert positional[1] == BlockStatus.terminated
        assert kwargs["task_status"] == TaskStatus.terminated
        assert kwargs["step_status"] == StepStatus.failed
        assert kwargs["failure_reason"] == "Terminate called: no results found"


@pytest.mark.asyncio
async def test_helper_skips_db_write_when_no_workflow_run_block_id():
    e = ScriptTerminationException("Terminate called")
    with patch(
        "skyvern.services.script_service._update_workflow_block",
        new_callable=AsyncMock,
    ) as mock_update:
        await _handle_script_termination(
            e,
            "task block",
            workflow_run_block_id=None,
            task_id=None,
            step_id=None,
            cache_key="MyTaskBlock",
        )
        mock_update.assert_not_awaited()
