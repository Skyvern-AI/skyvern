"""Tests for skipping block description LLM calls on for-loop iterations,
and for the content-hash cache that skips regeneration across runs.

Validates that execute_safe only dispatches description generation when
current_index is None (not in a loop) or 0 (first iteration), and skips
it for current_index > 0 (subsequent iterations). Also validates that
_generate_workflow_run_block_description serves identical block configs
from app.CACHE instead of re-calling the LLM every run.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, Block, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockResult, BlockStatus
from tests.unit.conftest import settle_or_fail, stalled_scrolling_capture


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
    workflow_run_context = MagicMock()
    workflow_run_context.cancel_failure_evidence_capture = AsyncMock()
    mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = workflow_run_context


class TestDescriptionSkippedOnLoopIterations:
    @pytest.mark.asyncio
    async def test_generates_description_when_not_in_loop(self) -> None:
        """current_index=None means the block is not inside a for-loop."""
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
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
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
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
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            await block.execute_safe(workflow_run_id="wr_1", current_index=5)

            mock_gen_desc.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_description_in_script_mode(self) -> None:
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch(
                "skyvern.forge.sdk.workflow.models.block.skyvern_context.current",
                return_value=SkyvernContext(script_mode=True),
            ),
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            await block.execute_safe(workflow_run_id="wr_1")

            mock_gen_desc.assert_not_called()

    @pytest.mark.asyncio
    async def test_description_called_once_across_multiple_iterations(self) -> None:
        """Simulates a 5-iteration loop — description generated only for index 0."""
        block = _make_block()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock) as mock_gen_desc,
        ):
            _setup_mocks(mock_app)

            for i in range(5):
                await block.execute_safe(workflow_run_id="wr_1", current_index=i)

            assert mock_gen_desc.call_count == 1


class TestDescriptionContentHashCache:
    def _patch_app(self, mock_app: MagicMock, cached: object = None, cache_error: bool = False) -> AsyncMock:
        mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
        if cache_error:
            mock_app.CACHE.get = AsyncMock(side_effect=RuntimeError("redis down"))
            mock_app.CACHE.set = AsyncMock(side_effect=RuntimeError("redis down"))
        else:
            mock_app.CACHE.get = AsyncMock(return_value=cached)
            mock_app.CACHE.set = AsyncMock()
        llm = AsyncMock(return_value={"summary": "fresh summary"})
        mock_app.SECONDARY_LLM_API_HANDLER = llm
        return llm

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm_call(self) -> None:
        block = _make_block()
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            llm = self._patch_app(mock_app, cached="cached summary")

            await block._generate_workflow_run_block_description("wrb_1", "org_1")

            llm.assert_not_called()
            mock_app.DATABASE.observer.update_workflow_run_block.assert_awaited_once()
            assert (
                mock_app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["description"]
                == "cached summary"
            )

    @pytest.mark.asyncio
    async def test_cache_miss_generates_and_stores(self) -> None:
        block = _make_block()
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            llm = self._patch_app(mock_app, cached=None)

            await block._generate_workflow_run_block_description("wrb_1", "org_1")

            llm.assert_awaited_once()
            mock_app.CACHE.set.assert_awaited_once()
            key, value = mock_app.CACHE.set.await_args.args
            assert key.startswith("wrb-description:")
            assert value == "fresh summary"

    @pytest.mark.asyncio
    async def test_identical_blocks_share_a_cache_key(self) -> None:
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            self._patch_app(mock_app, cached=None)

            await _make_block()._generate_workflow_run_block_description("wrb_1", "org_1")
            await _make_block()._generate_workflow_run_block_description("wrb_2", "org_1")

            keys = {call.args[0] for call in mock_app.CACHE.get.await_args_list}
            assert len(keys) == 1

    @pytest.mark.asyncio
    async def test_cache_errors_fall_through_to_generation(self) -> None:
        block = _make_block()
        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            llm = self._patch_app(mock_app, cache_error=True)

            await block._generate_workflow_run_block_description("wrb_1", "org_1")

            llm.assert_awaited_once()
            mock_app.DATABASE.observer.update_workflow_run_block.assert_awaited_once()
            assert (
                mock_app.DATABASE.observer.update_workflow_run_block.await_args.kwargs["description"] == "fresh summary"
            )


class TestPreBlockCaptureBudget:
    """The optional pre-block capture goes through the real screenshot primitive with a stalled helper."""

    @pytest.mark.asyncio
    async def test_stalled_optional_capture_settles_in_budget_and_block_executes(self) -> None:
        block = _make_block()
        entered = asyncio.Event()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()) as execute,
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _setup_mocks(mock_app)
            mock_app.ARTIFACT_MANAGER.create_workflow_run_block_artifact = AsyncMock()
            browser_state = MagicMock()
            browser_state.take_fullpage_screenshot = stalled_scrolling_capture(entered, timeout_ms=200)
            mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = browser_state

            task, elapsed = await settle_or_fail(block.execute_safe(workflow_run_id="wr_1"))

            assert task.result() is not None
            assert entered.is_set()
            assert elapsed < 2, elapsed
            execute.assert_awaited_once()
            mock_app.ARTIFACT_MANAGER.create_workflow_run_block_artifact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_during_stalled_capture_never_executes_the_block(self) -> None:
        block = _make_block()
        entered = asyncio.Event()

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, return_value=_block_result()) as execute,
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _setup_mocks(mock_app)
            browser_state = MagicMock()
            browser_state.take_fullpage_screenshot = stalled_scrolling_capture(entered, timeout_ms=5000)
            mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = browser_state

            task = asyncio.ensure_future(block.execute_safe(workflow_run_id="wr_1"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            execute.assert_not_awaited()
            mock_app.DATABASE.observer.update_workflow_run_block.assert_not_awaited()
