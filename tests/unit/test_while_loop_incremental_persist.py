"""WhileLoopBlock persists completed iterations so they survive an activity timeout."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import JinjaBranchCriteria, TaskBlock, WhileLoopBlock
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
async def test_while_loop_writes_the_partial_iteration_outputs_to_its_output_parameter() -> None:
    loop = WhileLoopBlock(
        label="wl",
        output_parameter=_op("wl"),
        loop_blocks=[TaskBlock(label="in", output_parameter=_op("in"), url="https://z.test")],
        condition=JinjaBranchCriteria(expression="{{ false }}"),
    )
    outputs = [[{"loop_value": 1}]]
    with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
        write = AsyncMock()
        mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = write
        await loop._persist_partial_loop_output("wr1", outputs, loop_idx=0)

    write.assert_awaited_once_with(workflow_run_id="wr1", output_parameter_id="op_wl", value=outputs)
