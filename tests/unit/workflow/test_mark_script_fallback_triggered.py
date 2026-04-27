"""Regression guards for `_mark_script_fallback_triggered` — flips
`ai_fallback_triggered=True` on the run iff a real script→AI fallback
occurred.

Without this helper, several real fallback paths in `_execute_single_block`
would never flip the run-level flag (see CORR-5/CORR-6/CORR-7 in the
debate). The gate semantics are:

- `valid_to_run_code=True` ⇒ script execution was attempted for this
  block. False rules out always-agent routes.
- `block_executed_with_code=False` ⇒ the script attempt didn't succeed.
  True means clean script execution; no fallback; no flip.

Together those two booleans are the exact condition under which the
agent `execute_safe` call we just ran constituted a script→AI fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.service import WorkflowService


@pytest.mark.asyncio
async def test_flips_flag_when_script_attempted_and_failed() -> None:
    """The happy-path fallback case: we tried script, it didn't succeed,
    agent ran. Flag must flip."""
    service = WorkflowService()
    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run",
        new_callable=AsyncMock,
    ) as mock_update:
        await service._mark_script_fallback_triggered(
            workflow_run_id="wr_test",
            valid_to_run_code=True,
            block_executed_with_code=False,
            block_label="some_block",
        )
    mock_update.assert_awaited_once_with(
        workflow_run_id="wr_test",
        ai_fallback_triggered=True,
    )


@pytest.mark.asyncio
async def test_does_not_flip_when_script_was_not_attempted() -> None:
    """Always-agent route: `valid_to_run_code=False` means this block was
    never going to run as script (requires_agent, disable_cache, uncached,
    non-cacheable block type, or pure agent workflow). The agent execution
    is NOT a fallback; it's the intended execution mode. Flag must not
    flip — else always-agent blocks in otherwise code-mode runs would
    falsely claim 'a fallback happened'."""
    service = WorkflowService()
    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run",
        new_callable=AsyncMock,
    ) as mock_update:
        await service._mark_script_fallback_triggered(
            workflow_run_id="wr_test",
            valid_to_run_code=False,
            block_executed_with_code=False,
            block_label="agent_only_block",
        )
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_does_not_flip_when_script_succeeded() -> None:
    """`block_executed_with_code=True` means the cached script ran cleanly.
    No fallback occurred. Flag must not flip."""
    service = WorkflowService()
    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run",
        new_callable=AsyncMock,
    ) as mock_update:
        await service._mark_script_fallback_triggered(
            workflow_run_id="wr_test",
            valid_to_run_code=True,
            block_executed_with_code=True,
            block_label="cached_block",
        )
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_swallows_db_error_with_warning_log() -> None:
    """A transient DB error on the flag flip must not abort block
    post-processing. The helper wraps the write in try/except so the
    caller's downstream logic (fallback-episode enrichment, etc.) runs
    to completion regardless. Asserts both the no-raise contract AND
    the warning-log-was-emitted contract."""
    service = WorkflowService()
    with (
        patch(
            "skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db transient"),
        ),
        patch("skyvern.forge.sdk.workflow.service.LOG.warning") as mock_log,
    ):
        # Must not raise.
        await service._mark_script_fallback_triggered(
            workflow_run_id="wr_test",
            valid_to_run_code=True,
            block_executed_with_code=False,
            block_label="some_block",
        )
    mock_log.assert_called_once()
    # The first positional arg is the log event name.
    assert "ai_fallback_triggered" in mock_log.call_args.args[0]
