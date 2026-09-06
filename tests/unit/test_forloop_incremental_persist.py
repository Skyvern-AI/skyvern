"""Tests for ForLoopBlock incremental output persistence.

Validates that:
1. Partial for-loop output is persisted to DB at intervals via direct DB call
2. Context registration (record_output_parameter_value) happens only once at final completion
3. Incremental persist failures don't break the loop
4. Accumulated output grows correctly across iterations
5. PERSIST_LOOP_OUTPUT_INTERVAL controls write frequency (O(N/K) not O(N))
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.forge.sdk.workflow.models.block import Block, ForLoopBlock, LoopBlockExecutedResult, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockResult, BlockStatus
from skyvern.webeye.real_browser_state import RealBrowserState

INTERVAL_PATCH = "skyvern.forge.sdk.workflow.models.block.PERSIST_LOOP_OUTPUT_INTERVAL"


def _make_output_param(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_output",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _make_block_result(output_param: OutputParameter, value: dict[str, Any] | None = None) -> BlockResult:
    return BlockResult(
        success=True,
        output_parameter=output_param,
        output_parameter_value=value or {"extracted": "data"},
        status=BlockStatus.completed,
    )


class TestExecuteCallsRecordOnceAtEnd:
    """ForLoopBlock.execute() must call record_output_parameter_value exactly once
    (at the end, after execute_loop_helper returns), not during iterations."""

    @pytest.mark.asyncio
    async def test_record_output_called_once_after_helper_returns(self) -> None:
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        loop_result = LoopBlockExecutedResult(
            outputs_with_loop_values=[[{"loop_value": "a"}], [{"loop_value": "b"}], [{"loop_value": "c"}]],
            block_outputs=[_make_block_result(inner_task.output_parameter)] * 3,
            last_block=inner_task,
        )

        mock_context = MagicMock()
        final_result = _make_block_result(loop_block.output_parameter)

        with (
            patch.object(Block, "get_workflow_run_context", return_value=mock_context),
            patch.object(
                ForLoopBlock, "get_loop_over_parameter_values", new_callable=AsyncMock, return_value=["a", "b", "c"]
            ),
            patch.object(ForLoopBlock, "execute_loop_helper", new_callable=AsyncMock, return_value=loop_result),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
            patch.object(Block, "build_block_result", new_callable=AsyncMock, return_value=final_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            await loop_block.execute(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

            # record_output_parameter_value called exactly once — the final write
            assert mock_record.call_count == 1
            call_args = mock_record.call_args
            assert call_args.args[1] == "wr_test"  # workflow_run_id
            assert call_args.args[2] == loop_result.outputs_with_loop_values  # full accumulated output


class TestExecuteLoopHelperPersistsToDbDirectly:
    """execute_loop_helper must persist partial output via direct DB UPSERT,
    not through record_output_parameter_value (which re-registers context)."""

    @pytest.mark.asyncio
    async def test_db_upsert_called_per_iteration_not_record_output(self) -> None:
        """With interval=1, each iteration triggers a DB UPSERT. record_output_parameter_value
        must NOT be called within the helper — that's execute()'s responsibility."""
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        inner_result = _make_block_result(inner_task.output_parameter)
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()

        mock_db_upsert = AsyncMock()

        with (
            patch(INTERVAL_PATCH, 1),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock) as mock_record,
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            result = await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                organization_id="org_test",
            )

            # DB UPSERT called once per iteration (interval=1, 3 iterations)
            assert mock_db_upsert.call_count == 3
            # record_output_parameter_value NOT called during iterations
            assert mock_record.call_count == 0
            # All 3 iterations produced output
            assert len(result.outputs_with_loop_values) == 3

    @pytest.mark.asyncio
    async def test_accumulated_output_grows_per_iteration(self) -> None:
        """Each DB UPSERT should contain all iterations completed so far."""
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        inner_result = _make_block_result(inner_task.output_parameter, {"med": "data"})
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()

        # Capture copies of the value at each call since the list is mutated in-place
        captured_values: list[list] = []

        async def capture_upsert(**kwargs: Any) -> None:
            captured_values.append(list(kwargs["value"]))

        mock_db_upsert = AsyncMock(side_effect=capture_upsert)

        with (
            patch(INTERVAL_PATCH, 1),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                organization_id="org_test",
            )

            # Verify accumulation: snapshot i has i items (interval=1 → persist every iteration)
            assert len(captured_values) == 3
            for i, snapshot in enumerate(captured_values, start=1):
                assert len(snapshot) == i, f"Iteration {i}: expected {i} accumulated outputs, got {len(snapshot)}"

    @pytest.mark.asyncio
    async def test_upsert_uses_forloop_output_parameter_id(self) -> None:
        """The DB UPSERT must target the ForLoopBlock's own output_parameter_id."""
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        inner_result = _make_block_result(inner_task.output_parameter)
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()

        mock_db_upsert = AsyncMock()

        with (
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=["x"],
                organization_id="org_test",
            )

            call_kwargs = mock_db_upsert.call_args_list[0].kwargs
            assert call_kwargs["workflow_run_id"] == "wr_test"
            assert call_kwargs["output_parameter_id"] == "op_test_loop"


class TestIncrementalPersistFailureResilience:
    """Incremental DB persist failures must not break the loop."""

    @pytest.mark.asyncio
    async def test_db_error_does_not_stop_loop(self) -> None:
        """If the DB UPSERT fails on every iteration, the loop still completes."""
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        inner_result = _make_block_result(inner_task.output_parameter)
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()

        mock_db_upsert = AsyncMock(side_effect=Exception("DB connection lost"))

        with (
            patch(INTERVAL_PATCH, 1),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            result = await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                organization_id="org_test",
            )

            # All 3 iterations attempted despite DB failures (interval=1)
            assert mock_db_upsert.call_count == 3
            # All 3 iterations produced output
            assert len(result.outputs_with_loop_values) == 3

    @pytest.mark.asyncio
    async def test_transient_db_failure_still_persists_later_iterations(self) -> None:
        """If DB fails on iteration 1 but succeeds on 2+3, those writes go through."""
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        inner_result = _make_block_result(inner_task.output_parameter)
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()

        # Capture copies on successful calls to verify accumulation
        captured_values: list[list] = []
        call_count = 0

        async def transient_failure_then_succeed(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("transient")
            captured_values.append(list(kwargs["value"]))

        mock_db_upsert = AsyncMock(side_effect=transient_failure_then_succeed)

        with (
            patch(INTERVAL_PATCH, 1),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                organization_id="org_test",
            )

            assert call_count == 3
            # Iteration 2 (first successful persist) has 2 accumulated outputs
            assert len(captured_values[0]) == 2
            # Iteration 3 has 3 accumulated outputs
            assert len(captured_values[1]) == 3


class TestPersistIntervalBatching:
    """PERSIST_LOOP_OUTPUT_INTERVAL controls write frequency to reduce DB load."""

    @pytest.mark.asyncio
    async def test_default_interval_batches_writes(self) -> None:
        """With default interval=10, a 25-iteration loop persists at indices 0, 10, 20 (3 writes)."""
        inner_task = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[inner_task],
        )

        inner_result = _make_block_result(inner_task.output_parameter)
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()

        captured_sizes: list[int] = []

        async def capture_upsert(**kwargs: Any) -> None:
            captured_sizes.append(len(kwargs["value"]))

        mock_db_upsert = AsyncMock(side_effect=capture_upsert)

        with (
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            result = await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=list(range(25)),
                organization_id="org_test",
            )

            # 25 iterations, interval=10 → persists at idx 0, 10, 20, and 24 (last)
            assert mock_db_upsert.call_count == 4
            # idx 0: 1 item, idx 10: 11 items, idx 20: 21 items, idx 24: 25 items
            assert captured_sizes == [1, 11, 21, 25]
            # All 25 iterations still produce output
            assert len(result.outputs_with_loop_values) == 25

    @pytest.mark.asyncio
    async def test_no_persist_when_output_parameter_is_none(self) -> None:
        """If output_parameter is None, _persist_partial_loop_output is a no-op."""
        inner_task = TaskBlock(label="inner", output_parameter=_make_output_param("inner"))
        loop_block = ForLoopBlock.model_construct(
            label="test_loop",
            output_parameter=None,
            loop_blocks=[inner_task],
        )

        mock_db_upsert = AsyncMock()

        with patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app:
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = mock_db_upsert

            await loop_block._persist_partial_loop_output(
                workflow_run_id="wr_test",
                outputs_with_loop_values=[[{"data": "val"}]],
                loop_idx=0,
            )

            assert mock_db_upsert.call_count == 0


class TestResetBrowserTabsBetweenIterations:
    """RESET_BROWSER_TABS_BETWEEN_LOOP_ITERATIONS: between iterations close only tabs opened during
    an iteration, preserving the pre-loop baseline (e.g. a deliverable tab) and the working page."""

    def _loop_block(self) -> ForLoopBlock:
        return ForLoopBlock(
            label="test_loop",
            output_parameter=_make_output_param("test_loop"),
            loop_blocks=[TaskBlock(label="inner", output_parameter=_make_output_param("inner"))],
        )

    @pytest.mark.asyncio
    async def test_baseline_snapshot_once_and_reset_between_iterations(self) -> None:
        loop_block = self._loop_block()
        inner_result = _make_block_result(loop_block.loop_blocks[0].output_parameter)
        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.set_value = MagicMock()
        mock_context.update_block_metadata = MagicMock()
        sentinel_baseline = {MagicMock()}

        with (
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch.object(ForLoopBlock, "get_loop_block_context_parameters", return_value=[]),
            patch.object(Block, "record_output_parameter_value", new_callable=AsyncMock),
            patch.object(
                ForLoopBlock, "_snapshot_loop_baseline_pages", new_callable=AsyncMock, return_value=sentinel_baseline
            ) as mock_snapshot,
            patch.object(ForLoopBlock, "_reset_browser_tabs_for_iteration", new_callable=AsyncMock) as mock_reset,
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()

            await loop_block.execute_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                loop_over_values=["a", "b", "c"],
                organization_id="org_test",
                browser_session_id="pbs_test",
            )

            mock_snapshot.assert_awaited_once()
            # 3 iterations -> reset only for idx 1 and 2, each with the captured baseline
            assert mock_reset.call_count == 2
            for call in mock_reset.call_args_list:
                assert call.args[0] == "wr_test"
                assert call.args[3] is sentinel_baseline

    @pytest.mark.asyncio
    async def test_reset_helper_delta_closes_with_session(self) -> None:
        loop_block = self._loop_block()
        mock_bs = MagicMock(spec=RealBrowserState)
        mock_bs.close_pages_opened_after = AsyncMock()
        baseline = {MagicMock()}
        reset_browser_tabs = AsyncMock(return_value=True)
        with (
            patch.object(
                block_module.planner_levers,
                "reset_browser_tabs_between_loop_iterations",
                reset_browser_tabs,
            ),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(return_value=mock_bs)
            await loop_block._reset_browser_tabs_for_iteration("wr_test", "org_test", "pbs_test", baseline)
            reset_browser_tabs.assert_awaited_once_with("org_test")
            mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_awaited_once_with("pbs_test", "org_test")
            mock_bs.close_pages_opened_after.assert_awaited_once_with(baseline)

    @pytest.mark.asyncio
    async def test_reset_helper_noop_when_flag_off(self) -> None:
        loop_block = self._loop_block()
        with (
            patch.object(
                block_module.planner_levers,
                "reset_browser_tabs_between_loop_iterations",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            await loop_block._reset_browser_tabs_for_iteration("wr_test", "org_test", "pbs_test", set())
            mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_called()
            mock_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_snapshot_none_when_flag_off(self) -> None:
        loop_block = self._loop_block()
        reset_browser_tabs = AsyncMock(return_value=False)
        with (
            patch.object(
                block_module.planner_levers,
                "reset_browser_tabs_between_loop_iterations",
                reset_browser_tabs,
            ),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            result = await loop_block._snapshot_loop_baseline_pages("wr_test", "org_test", None)
            assert result is None
            reset_browser_tabs.assert_awaited_once_with("org_test")
            mock_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_snapshot_returns_none_when_it_fails(self) -> None:
        loop_block = self._loop_block()
        with (
            patch.object(
                block_module.planner_levers,
                "reset_browser_tabs_between_loop_iterations",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state = AsyncMock(side_effect=Exception("boom"))
            assert await loop_block._snapshot_loop_baseline_pages("wr_test", "org_test", "pbs_test") is None

    @pytest.mark.asyncio
    async def test_reset_helper_noop_when_baseline_unknown(self) -> None:
        """A failed snapshot must suppress the reset — an empty baseline would close every tab."""
        loop_block = self._loop_block()
        reset_browser_tabs = AsyncMock(return_value=True)
        with (
            patch.object(
                block_module.planner_levers,
                "reset_browser_tabs_between_loop_iterations",
                reset_browser_tabs,
            ),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            await loop_block._reset_browser_tabs_for_iteration("wr_test", "org_test", "pbs_test", None)
            reset_browser_tabs.assert_not_awaited()
            mock_app.PERSISTENT_SESSIONS_MANAGER.get_browser_state.assert_not_called()
            mock_app.BROWSER_MANAGER.get_for_workflow_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_pages_opened_after_preserves_baseline_and_working(self) -> None:
        deliverable = MagicMock(url="https://pad/sheet", close=AsyncMock())
        pre_ref = MagicMock(url="https://ref", close=AsyncMock())
        research_old = MagicMock(url="https://r1", close=AsyncMock())
        research_cur = MagicMock(url="https://r2", close=AsyncMock())

        bs = RealBrowserState(pw=MagicMock(), browser_context=MagicMock())
        bs.browser_context.pages = [deliverable, pre_ref, research_old, research_cur]
        bs.get_working_page = AsyncMock(return_value=research_cur)

        await bs.close_pages_opened_after({deliverable, pre_ref})

        deliverable.close.assert_not_called()
        pre_ref.close.assert_not_called()
        research_cur.close.assert_not_called()
        research_old.close.assert_awaited_once()
