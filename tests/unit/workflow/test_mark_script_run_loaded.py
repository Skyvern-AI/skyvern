"""Regression guard for _mark_script_run_loaded — populates script_run on
server-side code-mode runs so the API can detect cache use.

Before this helper existed, `workflow_run.script_run` was null on every
Temporal run even when `execution_mode=code` was resolved at
service.py:1508. See SKY-* / PR #10522 for context.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.scripts import Script


@pytest.mark.asyncio
async def test_mark_script_run_loaded_calls_update_with_script_identity() -> None:
    """The helper must issue exactly one update with the script identity +
    `ai_fallback_triggered=False` initialization. The call-site caller in
    `_execute_workflow_blocks` routes through this helper, so this test
    pins down the shape of what hits the DB on every cached-code run."""
    service = WorkflowService()
    script = MagicMock(spec=Script, script_id="s_abc", script_revision_id="sr_xyz")

    with patch(
        "skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run",
        new_callable=AsyncMock,
    ) as mock_update:
        await service._mark_script_run_loaded("wr_test", script)

    mock_update.assert_awaited_once_with(
        workflow_run_id="wr_test",
        ai_fallback_triggered=False,
        script_id="s_abc",
        script_revision_id="sr_xyz",
    )


@pytest.mark.asyncio
async def test_mark_script_run_loaded_swallows_db_error_with_warning() -> None:
    """A transient DB error on the setup-time metadata write must not abort
    workflow setup. `script_run` is reporting state for API consumers — not
    load-bearing for the run's own execution — so a failure here should
    degrade to a warning, not a hard stop. This mirrors the try/except in
    `_mark_script_fallback_triggered`."""
    service = WorkflowService()
    script = MagicMock(spec=Script, script_id="s_abc", script_revision_id="sr_xyz")

    with (
        patch(
            "skyvern.forge.sdk.workflow.service.app.DATABASE.workflow_runs.update_workflow_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db transient"),
        ),
        patch("skyvern.forge.sdk.workflow.service.LOG.warning") as mock_log,
    ):
        # Must not raise.
        await service._mark_script_run_loaded("wr_test", script)
    mock_log.assert_called_once()
    assert "script_run" in mock_log.call_args.args[0]
