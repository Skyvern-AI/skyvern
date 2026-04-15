"""Tests for skipping block description LLM calls on for-loop iterations.

Validates that execute_safe only dispatches description generation when
current_index is None (not in a loop) or 0 (first iteration), and skips
it for current_index > 0 (subsequent iterations).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.models.block import Block, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockResult, BlockStatus


def _make_block() -> TaskBlock:
    now = datetime.now(UTC)
    return TaskBlock(
        label="test_block",
        output_parameter=OutputParameter(
            output_parameter_id="op_test",
            key="test_output",
            workflow_id="wf_test",
            created_at=now,
            modified_at=now,
        ),
    )


def _mock_workflow_run_block() -> MagicMock:
    wrb = MagicMock()
    wrb.workflow_run_block_id = "wrb_test"
    return wrb


def _block_result() -> BlockResult:
    now = datetime.now(UTC)
    return BlockResult(
        success=True,
        output_parameter=OutputParameter(
            output_parameter_id="op_result",
            key="result",
            workflow_id="wf_test",
            created_at=now,
            modified_at=now,
        ),
        status=BlockStatus.completed,
    )


def _setup_mocks(mock_app: MagicMock) -> None:
    """Set up the common mocks needed by execute_safe."""
    mock_app.DATABASE.observer.create_workflow_run_block = AsyncMock(return_value=_mock_workflow_run_block())
    mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
    mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = None


class TestDescriptionSkippedOnLoopIterations:
    @pytest.mark.asyncio
    async def test_generates_description_when_not_in_loop(self) -> None:
        """current_index=None means the block is not inside a for-loop."""
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(Block, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            await block.execute_safe(workflow_run_id="wr_1", current_index=None)

            mock_gen_desc.assert_called_once()

    @pytest.mark.asyncio
    async def test_generates_description_on_first_iteration(self) -> None:
        """current_index=0 is the first loop iteration — should still generate."""
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(Block, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            await block.execute_safe(workflow_run_id="wr_1", current_index=0)

            mock_gen_desc.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_description_on_subsequent_iterations(self) -> None:
        """current_index>0 should skip description generation entirely."""
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(Block, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            await block.execute_safe(workflow_run_id="wr_1", current_index=5)

            mock_gen_desc.assert_not_called()

    @pytest.mark.asyncio
    async def test_description_called_once_across_multiple_iterations(self) -> None:
        """Simulates a 5-iteration loop — description generated only for index 0."""
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(Block, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            for i in range(5):
                await block.execute_safe(workflow_run_id="wr_1", current_index=i)

            assert mock_gen_desc.call_count == 1
