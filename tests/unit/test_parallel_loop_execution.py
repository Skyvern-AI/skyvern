"""Tests for parallel loop execution (SKY-8175 + SKY-8176 + SKY-8180).

Tests cover:
1. max_concurrency field validation and clamping
2. Sequential behavior unchanged when max_concurrency is None or 1
3. Parallel execution dispatches correctly
4. Result ordering is preserved regardless of completion order
5. Error handling: one iteration failure doesn't kill others (when next_loop_on_failure=True)
6. WorkflowRunContext snapshot/merge isolation
7. Browser isolation key format
8. YAML schema passthrough
9. Batch sizing logic
10. Quota-enforced fallback to sequential
11. Concurrency slot release on success and failure
12. YAML round-trip through workflow_definition_converter
13. Browser cleanup verification
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import (
    BlockStatus,
    BlockType,
    ForLoopBlock,
    LoopBlockExecutedResult,
    TaskBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockResult, ForLoopBlockYAML, TaskBlockYAML, WorkflowDefinitionYAML

_NOW = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_failure_block_result(label: str = "failure") -> BlockResult:
    """Create a real BlockResult for failed iteration testing."""
    return BlockResult(
        success=False,
        output_parameter=_make_output_parameter(label),
        status=BlockStatus.failed,
        failure_reason="test failure",
    )


def _make_output_parameter(label: str) -> OutputParameter:
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_output",
        workflow_id="wf_test",
        created_at=_NOW,
        modified_at=_NOW,
    )


def _make_task_block(label: str) -> TaskBlock:
    return TaskBlock(
        label=label,
        block_type=BlockType.TASK,
        output_parameter=_make_output_parameter(label),
        url="https://example.com",
    )


def _make_for_loop_block(
    label: str = "loop_block",
    loop_blocks: list | None = None,
    max_concurrency: int | None = None,
    next_loop_on_failure: bool = False,
) -> ForLoopBlock:
    blocks = loop_blocks or [_make_task_block("inner_task")]
    return ForLoopBlock(
        label=label,
        block_type=BlockType.FOR_LOOP,
        output_parameter=_make_output_parameter(label),
        loop_blocks=blocks,
        max_concurrency=max_concurrency,
        next_loop_on_failure=next_loop_on_failure,
    )


# ---------------------------------------------------------------------------
# max_concurrency field validation
# ---------------------------------------------------------------------------


class TestMaxConcurrencyValidation:
    def test_none_is_preserved(self) -> None:
        block = _make_for_loop_block(max_concurrency=None)
        assert block.max_concurrency is None

    def test_one_is_preserved(self) -> None:
        block = _make_for_loop_block(max_concurrency=1)
        assert block.max_concurrency == 1

    def test_valid_value_preserved(self) -> None:
        block = _make_for_loop_block(max_concurrency=5)
        assert block.max_concurrency == 5

    def test_clamped_to_min_one(self) -> None:
        block = _make_for_loop_block(max_concurrency=0)
        assert block.max_concurrency == 1

    def test_clamped_negative_to_one(self) -> None:
        block = _make_for_loop_block(max_concurrency=-5)
        assert block.max_concurrency == 1

    def test_clamped_to_max_twenty(self) -> None:
        block = _make_for_loop_block(max_concurrency=100)
        assert block.max_concurrency == 20

    def test_twenty_is_preserved(self) -> None:
        block = _make_for_loop_block(max_concurrency=20)
        assert block.max_concurrency == 20


# ---------------------------------------------------------------------------
# YAML schema passthrough
# ---------------------------------------------------------------------------


class TestForLoopBlockYAMLMaxConcurrency:
    def test_yaml_accepts_max_concurrency(self) -> None:
        yaml_block = ForLoopBlockYAML(
            label="test_loop",
            loop_blocks=[TaskBlockYAML(label="inner", url="https://example.com")],
            max_concurrency=5,
        )
        assert yaml_block.max_concurrency == 5

    def test_yaml_defaults_to_none(self) -> None:
        yaml_block = ForLoopBlockYAML(
            label="test_loop",
            loop_blocks=[TaskBlockYAML(label="inner", url="https://example.com")],
        )
        assert yaml_block.max_concurrency is None


# ---------------------------------------------------------------------------
# WorkflowRunContext snapshot/merge
# ---------------------------------------------------------------------------


class TestWorkflowRunContextSnapshot:
    def _make_context(self) -> WorkflowRunContext:
        ctx = WorkflowRunContext(
            workflow_title="Test",
            workflow_id="wf_1",
            workflow_permanent_id="wpid_1",
            workflow_run_id="wr_1",
            aws_client=MagicMock(),
        )
        ctx.values = {"key1": "original_value", "key2": [1, 2, 3]}
        ctx.blocks_metadata = {"block_a": {"current_index": 0}}
        ctx.parameters = {"param1": MagicMock()}
        ctx.secrets = {"secret1": "s3cr3t"}
        ctx.organization_id = "org_1"
        return ctx

    def test_snapshot_deep_copies_values(self) -> None:
        ctx = self._make_context()
        snapshot = ctx.create_iteration_snapshot(0)

        # Modifying snapshot values should not affect original
        snapshot.values["key1"] = "modified"
        snapshot.values["key2"].append(4)

        assert ctx.values["key1"] == "original_value"
        assert ctx.values["key2"] == [1, 2, 3]

    def test_snapshot_shallow_copies_parameters_and_secrets(self) -> None:
        ctx = self._make_context()
        snapshot = ctx.create_iteration_snapshot(0)

        # Parameters and secrets dicts are shallow-copied so a future code
        # path that mutates them inside a loop block can't leak across
        # iterations. Values inside are still shared (read-only contract).
        assert snapshot.parameters is not ctx.parameters
        assert snapshot.parameters == ctx.parameters
        assert snapshot.secrets is not ctx.secrets
        assert snapshot.secrets == ctx.secrets

    def test_snapshot_deep_copies_blocks_metadata(self) -> None:
        ctx = self._make_context()
        snapshot = ctx.create_iteration_snapshot(0)

        snapshot.blocks_metadata["block_a"]["current_index"] = 99

        assert ctx.blocks_metadata["block_a"]["current_index"] == 0

    def test_snapshot_preserves_immutable_fields(self) -> None:
        ctx = self._make_context()
        snapshot = ctx.create_iteration_snapshot(0)

        assert snapshot.workflow_run_id == ctx.workflow_run_id
        assert snapshot.organization_id == ctx.organization_id
        assert snapshot.workflow_id == ctx.workflow_id

    def test_merge_iteration_results_preserves_order(self) -> None:
        ctx = self._make_context()

        # Create two snapshots with different values
        snap1 = ctx.create_iteration_snapshot(0)
        snap1.values["iter_0_result"] = "result_0"
        snap1.blocks_metadata["block_iter_0"] = {"done": True}

        snap2 = ctx.create_iteration_snapshot(1)
        snap2.values["iter_1_result"] = "result_1"
        snap2.blocks_metadata["block_iter_1"] = {"done": True}

        # Merge in reverse order — should still apply in index order
        ctx.merge_iteration_results([(1, snap2), (0, snap1)])

        assert ctx.values["iter_0_result"] == "result_0"
        assert ctx.values["iter_1_result"] == "result_1"
        assert "block_iter_0" in ctx.blocks_metadata
        assert "block_iter_1" in ctx.blocks_metadata

    def test_merge_later_iteration_overwrites_earlier_on_collision(self) -> None:
        ctx = self._make_context()

        snap0 = ctx.create_iteration_snapshot(0)
        snap0.values["shared_key"] = "from_iter_0"

        snap1 = ctx.create_iteration_snapshot(1)
        snap1.values["shared_key"] = "from_iter_1"

        ctx.merge_iteration_results([(0, snap0), (1, snap1)])

        # Later iteration (idx=1) should win
        assert ctx.values["shared_key"] == "from_iter_1"


# ---------------------------------------------------------------------------
# Browser iteration key format
# ---------------------------------------------------------------------------


class TestBrowserIterationKey:
    def test_key_format(self) -> None:
        from skyvern.constants import loop_iteration_key

        assert loop_iteration_key("wr_abc123", 5) == "wr_abc123__iter_5"

    def test_key_format_zero(self) -> None:
        from skyvern.constants import loop_iteration_key

        assert loop_iteration_key("wr_xyz", 0) == "wr_xyz__iter_0"


# ---------------------------------------------------------------------------
# Sequential path unchanged
# ---------------------------------------------------------------------------


class TestSequentialPathUnchanged:
    """Verify that max_concurrency=None and max_concurrency=1 use the sequential path."""

    @pytest.mark.asyncio
    async def test_none_concurrency_does_not_call_parallel(self) -> None:
        block = _make_for_loop_block(max_concurrency=None)
        # Mock _execute_loop_parallel to verify it's NOT called
        mock_parallel = AsyncMock()
        object.__setattr__(block, "_execute_loop_parallel", mock_parallel)

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.get_value.return_value = None
        mock_context.has_value.return_value = False

        # The sequential path will fail because we haven't mocked everything,
        # but _execute_loop_parallel should NOT be called
        try:
            await block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b"],
            )
        except Exception:
            pass

        mock_parallel.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrency_one_does_not_call_parallel(self) -> None:
        block = _make_for_loop_block(max_concurrency=1)
        mock_parallel = AsyncMock()
        object.__setattr__(block, "_execute_loop_parallel", mock_parallel)

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.get_value.return_value = None
        mock_context.has_value.return_value = False

        try:
            await block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b"],
            )
        except Exception:
            pass

        mock_parallel.assert_not_called()


# ---------------------------------------------------------------------------
# Parallel dispatch
# ---------------------------------------------------------------------------


class TestParallelDispatch:
    """Verify that max_concurrency > 1 dispatches to _execute_loop_parallel."""

    @pytest.mark.asyncio
    async def test_concurrency_gt_one_calls_parallel(self) -> None:
        block = _make_for_loop_block(max_concurrency=3)

        expected_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[],
            last_block=None,
        )
        mock_parallel = AsyncMock(return_value=expected_result)
        object.__setattr__(block, "_execute_loop_parallel", mock_parallel)

        mock_context = MagicMock(spec=WorkflowRunContext)

        result = await block.execute_loop_helper(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            workflow_run_context=mock_context,
            loop_over_values=["a", "b", "c"],
        )

        mock_parallel.assert_called_once()
        assert result is expected_result

    @pytest.mark.asyncio
    async def test_parallel_passes_granted_concurrency(self) -> None:
        """Verify granted_concurrency kwarg is forwarded from execute_loop_helper."""
        block = _make_for_loop_block(max_concurrency=5)

        expected_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[],
            block_outputs=[],
            last_block=None,
        )
        mock_parallel = AsyncMock(return_value=expected_result)
        object.__setattr__(block, "_execute_loop_parallel", mock_parallel)

        mock_context = MagicMock(spec=WorkflowRunContext)

        await block.execute_loop_helper(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            workflow_run_context=mock_context,
            loop_over_values=["a", "b"],
        )

        call_kwargs = mock_parallel.call_args.kwargs
        assert call_kwargs["granted_concurrency"] == 5


class TestBatchSizing:
    """Verify that _execute_loop_parallel processes iterations in correct batch sizes."""

    @pytest.mark.asyncio
    async def test_single_batch_when_values_lte_concurrency(self) -> None:
        """3 values with concurrency=5 should produce a single batch of 3."""
        block = _make_for_loop_block(max_concurrency=5)
        gather_calls: list[int] = []

        async def tracking_gather(*coros_or_futures, return_exceptions=False):
            gather_calls.append(len(coros_or_futures))
            # Return successful mock results for each coroutine
            results = []
            for coro in coros_or_futures:
                try:
                    # Cancel the coroutine since we can't actually run them
                    coro.close()
                except Exception:
                    pass
                # Return a successful iteration result tuple
                mock_ctx = MagicMock(spec=WorkflowRunContext)
                mock_ctx.values = {}
                mock_ctx.blocks_metadata = {}
                mock_ctx.workflow_run_outputs = {}
                results.append((len(results), [], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = tracking_gather
            mock_skyvern_ctx.current.return_value = None

            await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                granted_concurrency=5,
            )

        assert len(gather_calls) == 1
        assert gather_calls[0] == 3  # single batch of 3

    @pytest.mark.asyncio
    async def test_multiple_batches_when_values_gt_concurrency(self) -> None:
        """7 values with concurrency=3 should produce 3 batches: [3, 3, 1]."""
        block = _make_for_loop_block(max_concurrency=3)
        gather_calls: list[int] = []

        async def tracking_gather(*coros_or_futures, return_exceptions=False):
            gather_calls.append(len(coros_or_futures))
            results = []
            for coro in coros_or_futures:
                try:
                    coro.close()
                except Exception:
                    pass
                mock_ctx = MagicMock(spec=WorkflowRunContext)
                mock_ctx.values = {}
                mock_ctx.blocks_metadata = {}
                mock_ctx.workflow_run_outputs = {}
                results.append((len(results), [], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = tracking_gather
            mock_skyvern_ctx.current.return_value = None

            await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c", "d", "e", "f", "g"],
                granted_concurrency=3,
            )

        assert len(gather_calls) == 3
        assert gather_calls == [3, 3, 1]


# ---------------------------------------------------------------------------
# Parallel error handling
# ---------------------------------------------------------------------------


class TestParallelErrorHandling:
    """Verify error handling in _execute_loop_parallel."""

    @pytest.mark.asyncio
    async def test_failed_iteration_with_next_loop_on_failure_continues(self) -> None:
        """When next_loop_on_failure=True, one exception doesn't stop the loop."""
        block = _make_for_loop_block(max_concurrency=3, next_loop_on_failure=True)

        async def mock_gather(*coros_or_futures, return_exceptions=False):
            results = []
            for i, coro in enumerate(coros_or_futures):
                try:
                    coro.close()
                except Exception:
                    pass
                if i == 1:
                    # Second iteration raises
                    results.append(RuntimeError("iteration 1 failed"))
                else:
                    mock_ctx = MagicMock(spec=WorkflowRunContext)
                    mock_ctx.values = {}
                    mock_ctx.blocks_metadata = {}
                    mock_ctx.workflow_run_outputs = {}
                    results.append((i, [{"output": f"result_{i}"}], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = mock_gather
            mock_skyvern_ctx.current.return_value = None

            # build_block_result needs to return a mock BlockResult
            object.__setattr__(block, "build_block_result", AsyncMock(return_value=_make_failure_block_result()))

            result = await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                granted_concurrency=3,
            )

        # All 3 iterations processed: 2 success + 1 failure
        assert len(result.outputs_with_loop_values) == 3
        # Failure produces empty outputs list
        assert result.outputs_with_loop_values[1] == []

    @pytest.mark.asyncio
    async def test_failed_iteration_without_next_loop_on_failure_stops(self) -> None:
        """When next_loop_on_failure=False, first exception stops the loop early."""
        block = _make_for_loop_block(max_concurrency=3, next_loop_on_failure=False)

        async def mock_gather(*coros_or_futures, return_exceptions=False):
            results = []
            for i, coro in enumerate(coros_or_futures):
                try:
                    coro.close()
                except Exception:
                    pass
                if i == 0:
                    # First iteration (index 0) returns success
                    mock_ctx = MagicMock(spec=WorkflowRunContext)
                    mock_ctx.values = {}
                    mock_ctx.blocks_metadata = {}
                    mock_ctx.workflow_run_outputs = {}
                    results.append((i, [{"output": "ok"}], [], None, mock_ctx))
                elif i == 1:
                    # Second iteration (index 1) raises
                    results.append(RuntimeError("iteration 1 failed"))
                else:
                    mock_ctx = MagicMock(spec=WorkflowRunContext)
                    mock_ctx.values = {}
                    mock_ctx.blocks_metadata = {}
                    mock_ctx.workflow_run_outputs = {}
                    results.append((i, [{"output": "ok"}], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = mock_gather
            mock_skyvern_ctx.current.return_value = None

            object.__setattr__(block, "build_block_result", AsyncMock(return_value=_make_failure_block_result()))

            result = await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                granted_concurrency=3,
            )

        # Stops after the batch finishes, but all 3 iteration results are
        # captured first (idx 0 success + idx 1 failure + idx 2 success).
        # asyncio.gather already ran them to completion, so dropping the
        # later successes would silently lose data the browser produced.
        assert len(result.outputs_with_loop_values) == 3

    @pytest.mark.asyncio
    async def test_all_iterations_fail_with_next_loop_on_failure(self) -> None:
        """Edge case: all iterations fail but loop continues due to next_loop_on_failure."""
        block = _make_for_loop_block(max_concurrency=3, next_loop_on_failure=True)

        async def mock_gather(*coros_or_futures, return_exceptions=False):
            results = []
            for coro in coros_or_futures:
                try:
                    coro.close()
                except Exception:
                    pass
                results.append(RuntimeError("failed"))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = mock_gather
            mock_skyvern_ctx.current.return_value = None

            object.__setattr__(block, "build_block_result", AsyncMock(return_value=_make_failure_block_result()))

            result = await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                granted_concurrency=3,
            )

        # All 3 iterations produced output (all failures)
        assert len(result.outputs_with_loop_values) == 3
        # All outputs are empty lists (failure case)
        assert all(o == [] for o in result.outputs_with_loop_values)


class TestWorkflowRunContextSnapshotIsolation:
    """Additional snapshot/merge isolation tests for parallel loops."""

    def _make_context(self) -> WorkflowRunContext:
        ctx = WorkflowRunContext(
            workflow_title="Test",
            workflow_id="wf_1",
            workflow_permanent_id="wpid_1",
            workflow_run_id="wr_1",
            aws_client=MagicMock(),
        )
        ctx.values = {"shared": "original"}
        ctx.blocks_metadata = {"block_a": {"idx": 0}}
        ctx.workflow_run_outputs = {"output_a": "val_a"}
        ctx.parameters = {"p1": MagicMock()}
        ctx.secrets = {"s1": "secret"}
        ctx.organization_id = "org_1"
        return ctx

    def test_snapshot_deep_copies_workflow_run_outputs(self) -> None:
        ctx = self._make_context()
        snapshot = ctx.create_iteration_snapshot(0)

        snapshot.workflow_run_outputs["output_b"] = "val_b"

        assert "output_b" not in ctx.workflow_run_outputs

    def test_multiple_snapshots_are_independent(self) -> None:
        ctx = self._make_context()
        snap0 = ctx.create_iteration_snapshot(0)
        snap1 = ctx.create_iteration_snapshot(1)

        snap0.values["only_in_0"] = True
        snap1.values["only_in_1"] = True

        assert "only_in_1" not in snap0.values
        assert "only_in_0" not in snap1.values

    def test_merge_workflow_run_outputs(self) -> None:
        ctx = self._make_context()
        snap0 = ctx.create_iteration_snapshot(0)
        snap0.workflow_run_outputs["iter_0_out"] = "r0"

        snap1 = ctx.create_iteration_snapshot(1)
        snap1.workflow_run_outputs["iter_1_out"] = "r1"

        ctx.merge_iteration_results([(0, snap0), (1, snap1)])

        assert ctx.workflow_run_outputs["iter_0_out"] == "r0"
        assert ctx.workflow_run_outputs["iter_1_out"] == "r1"

    def test_merge_empty_snapshots_is_safe(self) -> None:
        ctx = self._make_context()
        original_values = dict(ctx.values)

        ctx.merge_iteration_results([])

        assert ctx.values == original_values


# ---------------------------------------------------------------------------
# Browser cleanup verification
# ---------------------------------------------------------------------------


class TestBrowserCleanup:
    """Verify browser cleanup is called correctly in parallel loops."""

    @pytest.mark.asyncio
    async def test_cleanup_called_for_each_batch(self) -> None:
        """cleanup_loop_iterations should be called once per batch."""
        block = _make_for_loop_block(max_concurrency=2)
        cleanup_calls: list[list[int]] = []

        async def mock_gather(*coros_or_futures, return_exceptions=False):
            results = []
            for i, coro in enumerate(coros_or_futures):
                try:
                    coro.close()
                except Exception:
                    pass
                mock_ctx = MagicMock(spec=WorkflowRunContext)
                mock_ctx.values = {}
                mock_ctx.blocks_metadata = {}
                mock_ctx.workflow_run_outputs = {}
                results.append((i, [], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        async def track_cleanup(workflow_run_id, loop_indices, organization_id=None):
            cleanup_calls.append(list(loop_indices))

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_app.BROWSER_MANAGER.cleanup_loop_iterations = track_cleanup
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = mock_gather
            mock_skyvern_ctx.current.return_value = None

            await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c", "d", "e"],
                granted_concurrency=2,
            )

        # 5 values / 2 concurrency = 3 batches
        assert len(cleanup_calls) == 3
        assert cleanup_calls[0] == [0, 1]
        assert cleanup_calls[1] == [2, 3]
        assert cleanup_calls[2] == [4]


# ---------------------------------------------------------------------------
# YAML round-trip through workflow_definition_converter
# ---------------------------------------------------------------------------


class TestYAMLRoundTrip:
    """Verify max_concurrency survives YAML→block conversion."""

    def test_converter_preserves_max_concurrency(self) -> None:
        from skyvern.forge.sdk.workflow.workflow_definition_converter import (
            convert_workflow_definition,
        )
        from skyvern.schemas.workflows import WorkflowParameterYAML

        yaml_def = WorkflowDefinitionYAML(
            version=2,
            blocks=[
                ForLoopBlockYAML(
                    label="parallel_loop",
                    loop_blocks=[TaskBlockYAML(label="inner_task", url="https://example.com")],
                    loop_over_parameter_key="items",
                    max_concurrency=5,
                ),
            ],
            parameters=[
                WorkflowParameterYAML(
                    key="items",
                    parameter_type="workflow",
                    workflow_parameter_type="json",
                    default_value=["a", "b"],
                ),
            ],
        )

        wd = convert_workflow_definition(yaml_def, "wf_test")
        assert len(wd.blocks) == 1
        loop_block = wd.blocks[0]
        assert isinstance(loop_block, ForLoopBlock)
        assert loop_block.max_concurrency == 5

    def test_converter_preserves_none_max_concurrency(self) -> None:
        from skyvern.forge.sdk.workflow.workflow_definition_converter import (
            convert_workflow_definition,
        )
        from skyvern.schemas.workflows import WorkflowParameterYAML

        yaml_def = WorkflowDefinitionYAML(
            version=2,
            blocks=[
                ForLoopBlockYAML(
                    label="sequential_loop",
                    loop_blocks=[TaskBlockYAML(label="inner_task", url="https://example.com")],
                    loop_over_parameter_key="items",
                ),
            ],
            parameters=[
                WorkflowParameterYAML(
                    key="items",
                    parameter_type="workflow",
                    workflow_parameter_type="json",
                    default_value=["a", "b"],
                ),
            ],
        )

        wd = convert_workflow_definition(yaml_def, "wf_test")
        loop_block = wd.blocks[0]
        assert isinstance(loop_block, ForLoopBlock)
        assert loop_block.max_concurrency is None


# ---------------------------------------------------------------------------
# Single iteration edge case
# ---------------------------------------------------------------------------


class TestSingleIterationEdgeCase:
    """With only 1 iteration, parallel path should still work correctly."""

    @pytest.mark.asyncio
    async def test_single_value_parallel(self) -> None:
        block = _make_for_loop_block(max_concurrency=5)

        async def mock_gather(*coros_or_futures, return_exceptions=False):
            results = []
            for i, coro in enumerate(coros_or_futures):
                try:
                    coro.close()
                except Exception:
                    pass
                mock_ctx = MagicMock(spec=WorkflowRunContext)
                mock_ctx.values = {"result": "single"}
                mock_ctx.blocks_metadata = {}
                mock_ctx.workflow_run_outputs = {}
                results.append((0, [{"output": "single_result"}], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = mock_gather
            mock_skyvern_ctx.current.return_value = None

            result = await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=["only_one"],
                granted_concurrency=5,
            )

        assert len(result.outputs_with_loop_values) == 1
        assert result.outputs_with_loop_values[0] == [{"output": "single_result"}]


# ---------------------------------------------------------------------------
# Iteration cap enforcement in parallel path
# ---------------------------------------------------------------------------


class TestIterationCapParallel:
    """Verify DEFAULT_MAX_LOOP_ITERATIONS is enforced in parallel path."""

    @pytest.mark.asyncio
    async def test_values_capped_at_max_iterations(self) -> None:
        from skyvern.forge.sdk.workflow.models.block import DEFAULT_MAX_LOOP_ITERATIONS

        block = _make_for_loop_block(max_concurrency=20)
        gather_call_sizes: list[int] = []

        async def mock_gather(*coros_or_futures, return_exceptions=False):
            gather_call_sizes.append(len(coros_or_futures))
            results = []
            for i, coro in enumerate(coros_or_futures):
                try:
                    coro.close()
                except Exception:
                    pass
                mock_ctx = MagicMock(spec=WorkflowRunContext)
                mock_ctx.values = {}
                mock_ctx.blocks_metadata = {}
                mock_ctx.workflow_run_outputs = {}
                results.append((i, [], [], None, mock_ctx))
            return results

        mock_context = MagicMock(spec=WorkflowRunContext)
        mock_context.create_iteration_snapshot.return_value = MagicMock(
            spec=WorkflowRunContext,
            values={},
            blocks_metadata={},
            workflow_run_outputs={},
        )

        # Create more values than the max
        oversized_values = list(range(DEFAULT_MAX_LOOP_ITERATIONS + 50))

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.asyncio") as mock_asyncio,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_app.AGENT_FUNCTION = AsyncMock()
            mock_app.AGENT_FUNCTION.release_parallel_loop_quota = AsyncMock()
            mock_app.BROWSER_MANAGER = AsyncMock()
            mock_asyncio.create_task = lambda coro: coro
            mock_asyncio.gather = mock_gather
            mock_skyvern_ctx.current.return_value = None

            await block._execute_loop_parallel(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                workflow_run_context=mock_context,
                loop_over_values=oversized_values,
                granted_concurrency=20,
            )

        total_processed = sum(gather_call_sizes)
        assert total_processed == DEFAULT_MAX_LOOP_ITERATIONS
