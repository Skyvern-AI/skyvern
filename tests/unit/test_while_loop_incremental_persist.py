"""WhileLoopBlock incremental DB persistence mirrors for-loop script path expectations."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import WhileLoopBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter


def _op(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_out",
        workflow_id="wf",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_persist_partial_while_loop_output_calls_db() -> None:
    from skyvern.forge.sdk.workflow.models.block import JinjaBranchCriteria, TaskBlock

    inner = TaskBlock(label="in", output_parameter=_op("in"), url="https://z.test")
    loop = WhileLoopBlock(
        label="wl",
        output_parameter=_op("wl"),
        loop_blocks=[inner],
        condition=JinjaBranchCriteria(expression="{{ false }}"),
    )
    with patch("skyvern.forge.sdk.workflow.models.control_flow_blocks.app") as mock_app:
        mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
        await loop._persist_partial_loop_output("wr1", [[]], loop_idx=0)
    mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter.assert_awaited_once()
