"""
Test that block output parameters are correctly scoped across loop iterations.

Verifies that when the same block runs multiple times inside a for-loop,
later iterations' extracted_information takes precedence over earlier ones.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import BrowserStateDiagnostic, MissingBrowserStatePage
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock, Block, TaskBlock, TextPromptBlock
from skyvern.forge.sdk.workflow.models.parameter import ContextParameter, OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockStatus


def _make_output_parameter(key: str) -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        output_parameter_id="op_test",
        workflow_id="wf_test",
        created_at=datetime.now(),
        modified_at=datetime.now(),
    )


def test_block_output_updates_across_loop_iterations():
    """
    Simulates two loop iterations where block 'extract_data' produces different
    extracted_information each time. Verifies that the second registration
    overwrites (not merges-under) the first.
    """
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=None,  # type: ignore[arg-type]
    )

    param = _make_output_parameter("extract_data_output")

    # --- Iteration 1 ---
    iteration_1_value = {
        "extracted_information": {"quote": "Quote from page 1", "author": "Author 1"},
        "status": "completed",
    }
    ctx.register_block_reference_variable_from_output_parameter(param, iteration_1_value)

    assert ctx.values["extract_data"]["extracted_information"] == {
        "quote": "Quote from page 1",
        "author": "Author 1",
    }

    # --- Iteration 2 ---
    iteration_2_value = {
        "extracted_information": {"quote": "Quote from page 2", "author": "Author 2"},
        "status": "completed",
    }
    ctx.register_block_reference_variable_from_output_parameter(param, iteration_2_value)

    # After iteration 2, values must reflect iteration 2's data
    result = ctx.values["extract_data"]
    assert result["extracted_information"] == {"quote": "Quote from page 2", "author": "Author 2"}, (
        f"Iteration 2's extracted_information was overwritten by iteration 1's. Got: {result}"
    )
    # The `output` alias must also reflect the latest iteration
    assert result["output"] == {"quote": "Quote from page 2", "author": "Author 2"}


def test_old_only_keys_preserved_across_iterations():
    """
    When iteration 1 produces keys that iteration 2 does not, those keys
    should be preserved (merge semantics), while overlapping keys use
    iteration 2's values.
    """
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=None,  # type: ignore[arg-type]
    )

    param = _make_output_parameter("block_output")

    # Iteration 1 has an extra key "extra_field"
    ctx.register_block_reference_variable_from_output_parameter(
        param,
        {
            "extracted_information": {"name": "Alice"},
            "extra_field": "only_in_iter1",
            "status": "completed",
        },
    )

    # Iteration 2 does not have "extra_field"
    ctx.register_block_reference_variable_from_output_parameter(
        param,
        {
            "extracted_information": {"name": "Bob"},
            "status": "completed",
        },
    )

    result = ctx.values["block"]
    # Overlapping keys use iteration 2's values
    assert result["extracted_information"] == {"name": "Bob"}
    assert result["status"] == "completed"
    # Old-only keys are preserved
    assert result["extra_field"] == "only_in_iter1"


def _make_ctx() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="test",
        workflow_id="wf_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=None,  # type: ignore[arg-type]
    )


def _make_task_block(label: str = "extract_details") -> TaskBlock:
    return TaskBlock(label=label, output_parameter=_make_output_parameter(f"{label}_output"))


def _make_text_prompt_block(label: str, missing_param_key: str) -> TextPromptBlock:
    return TextPromptBlock(
        label=label,
        output_parameter=_make_output_parameter(f"{label}_output"),
        prompt="Summarize the extracted details.",
        parameters=[_make_output_parameter(missing_param_key)],
    )


def _wire_app(mock_app: MagicMock, ctx: WorkflowRunContext) -> None:
    wrb = MagicMock()
    wrb.workflow_run_block_id = "wrb_test"
    mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx
    mock_app.DATABASE.observer.create_workflow_run_block = AsyncMock(return_value=wrb)
    mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
    mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
    mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = None
    mock_app.AGENT_FUNCTION.validate_block_execution = AsyncMock()


class TestFailedBlockDoesNotLeakPriorIterationValue:
    """SKY-12981: when a for-loop body block fails, a prior iteration's output value
    must not survive for downstream blocks in the failed iteration."""

    @pytest.mark.asyncio
    async def test_failed_loop_block_invalidates_stale_prior_iteration_value(self) -> None:
        ctx = _make_ctx()
        param = _make_output_parameter("extract_details_output")
        await ctx.register_output_parameter_value_post_execution(param, {"quote": "iteration-1 data"})
        assert ctx.values["extract_details_output"] == {"quote": "iteration-1 data"}
        assert ctx.values["extract_details"] == {"quote": "iteration-1 data"}

        block = _make_task_block("extract_details")
        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(
                BaseTaskBlock,
                "execute",
                new_callable=AsyncMock,
                side_effect=MissingBrowserStatePage(workflow_run_id="wr_test"),
            ),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert ctx.values["extract_details_output"] is None
        assert ctx.get_value("extract_details") is None

    @pytest.mark.asyncio
    async def test_missing_browser_state_failure_redacts_disconnect_diagnostic(self) -> None:
        ctx = _make_ctx()
        block = _make_task_block("extract_details")
        detected_at = datetime.now(timezone.utc)
        exception = MissingBrowserStatePage(
            workflow_run_id="wr_test",
            diagnostic=BrowserStateDiagnostic(
                reason="browser_context_disconnected",
                disconnect_observed_at=detected_at - timedelta(seconds=2),
                browser_session_id="pbs_test",
            ),
            detected_at=detected_at,
        )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=exception),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test")

        assert result.success is False
        assert result.failure_reason is not None
        assert result.failure_reason == exception.user_facing_message
        assert "browser_context_disconnected" not in result.failure_reason
        assert "browser_session_id=pbs_test" not in result.failure_reason
        assert "observation_gap_seconds=2.000" not in result.failure_reason
        assert "browser_context_disconnected" in str(exception)

    @pytest.mark.asyncio
    async def test_failed_loop_block_preserves_value_recorded_this_execution(self) -> None:
        ctx = _make_ctx()
        param = _make_output_parameter("extract_details_output")
        await ctx.register_output_parameter_value_post_execution(param, {"quote": "iteration-1 data"})

        block = _make_task_block("extract_details")

        async def _record_then_raise(*args: object, **kwargs: object) -> None:
            await block.record_output_parameter_value(ctx, "wr_test", {"quote": "iteration-2 fresh"})
            raise MissingBrowserStatePage(workflow_run_id="wr_test")

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=_record_then_raise),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert ctx.values["extract_details_output"] == {"quote": "iteration-2 fresh"}

    @pytest.mark.asyncio
    async def test_failed_block_with_no_prior_value_records_none(self) -> None:
        ctx = _make_ctx()
        block = _make_task_block("extract_details")
        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(
                BaseTaskBlock,
                "execute",
                new_callable=AsyncMock,
                side_effect=MissingBrowserStatePage(workflow_run_id="wr_test"),
            ),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=None)

        assert result.success is False
        assert ctx.values["extract_details_output"] is None

    @pytest.mark.asyncio
    async def test_failed_loop_block_with_dependent_context_parameter_does_not_crash(self) -> None:
        # A ContextParameter sourced from the failing block's OutputParameter must be
        # invalidated to None, not raise ValueError (which would crash the workflow run).
        ctx = _make_ctx()
        op = _make_output_parameter("extract_details_output")
        ctx.values["extract_details_output"] = {"quote": "iteration-1 data"}
        cp = ContextParameter(key="contact_from_extract", source=op)
        cp.value = {"quote": "iteration-1 data"}
        ctx.parameters["contact_from_extract"] = cp
        ctx.values["contact_from_extract"] = cp.value

        block = _make_task_block("extract_details")
        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(
                BaseTaskBlock,
                "execute",
                new_callable=AsyncMock,
                side_effect=MissingBrowserStatePage(workflow_run_id="wr_test"),
            ),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert ctx.values["extract_details_output"] is None
        assert ctx.parameters["contact_from_extract"].value is None
        assert ctx.values["contact_from_extract"] is None

    @pytest.mark.asyncio
    async def test_failed_non_loop_block_keeps_prior_value(self) -> None:
        ctx = _make_ctx()
        param = _make_output_parameter("extract_details_output")
        await ctx.register_output_parameter_value_post_execution(param, {"quote": "prior data"})

        block = _make_task_block("extract_details")
        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(
                BaseTaskBlock,
                "execute",
                new_callable=AsyncMock,
                side_effect=MissingBrowserStatePage(workflow_run_id="wr_test"),
            ),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=None)

        assert result.success is False
        assert ctx.values["extract_details_output"] == {"quote": "prior data"}

    @pytest.mark.asyncio
    async def test_failed_loop_textpromptblock_missing_parameter_invalidates_stale_value(self) -> None:
        # Real TextPromptBlock.execute reports failure by RETURNING an unsuccessful BlockResult
        # (missing-required-parameter branch) without recording output. A prior iteration's value
        # must not survive for the failed iteration's downstream blocks.
        ctx = _make_ctx()
        param = _make_output_parameter("summarize_output")
        await ctx.register_output_parameter_value_post_execution(param, {"summary": "iteration-1 summary"})
        assert ctx.values["summarize_output"] == {"summary": "iteration-1 summary"}
        assert ctx.values["summarize"] == {"summary": "iteration-1 summary"}

        block = _make_text_prompt_block("summarize", missing_param_key="upstream_extract")
        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert ctx.values["summarize_output"] is None
        assert ctx.get_value("summarize") is None

    @pytest.mark.asyncio
    async def test_failed_loop_taskblock_timed_out_return_invalidates_stale_value(self) -> None:
        # BaseTaskBlock reports a timed-out task by RETURNING an unsuccessful BlockResult
        # (build_block_result, which does not touch WorkflowRunContext) rather than raising.
        ctx = _make_ctx()
        param = _make_output_parameter("extract_details_output")
        await ctx.register_output_parameter_value_post_execution(param, {"quote": "iteration-1 data"})
        assert ctx.values["extract_details_output"] == {"quote": "iteration-1 data"}

        block = _make_task_block("extract_details")

        async def _return_timed_out(*args: object, **kwargs: object) -> object:
            return await block.build_block_result(
                success=False,
                failure_reason="Task timed out",
                output_parameter_value=None,
                status=BlockStatus.timed_out,
                workflow_run_block_id=str(args[1]) if len(args) > 1 else None,
                organization_id=None,
            )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=_return_timed_out),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert result.status == BlockStatus.timed_out
        assert ctx.values["extract_details_output"] is None
        assert ctx.get_value("extract_details") is None

    @pytest.mark.asyncio
    async def test_returned_failure_preserves_value_recorded_this_execution(self) -> None:
        # Mirrors the non-retry task-failure path that records its real output before returning
        # success=False: a value recorded during THIS execution must be preserved, not invalidated.
        ctx = _make_ctx()
        param = _make_output_parameter("extract_details_output")
        await ctx.register_output_parameter_value_post_execution(param, {"quote": "iteration-1 data"})

        block = _make_task_block("extract_details")

        async def _record_then_return_failure(*args: object, **kwargs: object) -> object:
            await block.record_output_parameter_value(ctx, "wr_test", {"quote": "iteration-2 fresh"})
            return await block.build_block_result(
                success=False,
                failure_reason="Task failed after recording output",
                output_parameter_value={"quote": "iteration-2 fresh"},
                status=BlockStatus.failed,
                workflow_run_block_id=str(args[1]) if len(args) > 1 else None,
                organization_id=None,
            )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=_record_then_return_failure),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert ctx.values["extract_details_output"] == {"quote": "iteration-2 fresh"}

    @pytest.mark.asyncio
    async def test_non_loop_returned_failure_keeps_prior_value(self) -> None:
        # Outside a loop (current_index is None) a returned failure must not invalidate output.
        ctx = _make_ctx()
        param = _make_output_parameter("extract_details_output")
        await ctx.register_output_parameter_value_post_execution(param, {"quote": "prior data"})

        block = _make_task_block("extract_details")

        async def _return_failure(*args: object, **kwargs: object) -> object:
            return await block.build_block_result(
                success=False,
                failure_reason="Task failed",
                output_parameter_value=None,
                status=BlockStatus.failed,
                workflow_run_block_id=str(args[1]) if len(args) > 1 else None,
                organization_id=None,
            )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=_return_failure),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=None)

        assert result.success is False
        assert ctx.values["extract_details_output"] == {"quote": "prior data"}

    @pytest.mark.asyncio
    async def test_returned_failure_with_dependent_context_parameter_does_not_crash(self) -> None:
        # A returned failure that invalidates a loop output feeding a ContextParameter must set the
        # ContextParameter to None (crash-safe and leak-safe), never raise ValueError.
        ctx = _make_ctx()
        op = _make_output_parameter("extract_details_output")
        ctx.values["extract_details_output"] = {"quote": "iteration-1 data"}
        cp = ContextParameter(key="contact_from_extract", source=op)
        cp.value = {"quote": "iteration-1 data"}
        ctx.parameters["contact_from_extract"] = cp
        ctx.values["contact_from_extract"] = cp.value

        block = _make_task_block("extract_details")

        async def _return_timed_out(*args: object, **kwargs: object) -> object:
            return await block.build_block_result(
                success=False,
                failure_reason="Task timed out",
                output_parameter_value=None,
                status=BlockStatus.timed_out,
                workflow_run_block_id=str(args[1]) if len(args) > 1 else None,
                organization_id=None,
            )

        with (
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch.object(BaseTaskBlock, "execute", new_callable=AsyncMock, side_effect=_return_timed_out),
            patch.object(Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
        ):
            _wire_app(mock_app, ctx)
            result = await block.execute_safe(workflow_run_id="wr_test", current_index=1)

        assert result.success is False
        assert ctx.values["extract_details_output"] is None
        assert ctx.parameters["contact_from_extract"].value is None
        assert ctx.values["contact_from_extract"] is None


def test_traced_decorator_owns_execute_safe_not_the_invalidation_helper() -> None:
    # @traced(role="wrapper") must decorate execute_safe, not _invalidate_stale_output_on_failure;
    # inserting the helper between the decorator and execute_safe once orphaned it. The traced
    # wrapper sets __wrapped__ via functools.wraps.
    assert Block.execute_safe.__wrapped__.__qualname__ == "Block.execute_safe"
    assert not hasattr(Block._invalidate_stale_output_on_failure, "__wrapped__")
