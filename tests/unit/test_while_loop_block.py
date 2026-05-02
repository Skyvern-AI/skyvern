"""Tests for WhileLoopBlock (PR 1 of SKY-8771).

Covers schema validation, top-of-loop semantics, max-iteration safety,
condition rendering errors, per-iteration metadata shape, cancellation
propagation, get_all_blocks recursion, and nested-label validation.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from skyvern.forge.sdk.workflow.exceptions import (
    FailedToFormatJinjaStyleParameter,
    MissingJinjaVariables,
    WorkflowDefinitionHasDuplicateBlockLabels,
)
from skyvern.forge.sdk.workflow.models.block import (
    Block,
    ForLoopBlock,
    JinjaBranchCriteria,
    PromptBranchCriteria,
    TaskBlock,
    WhileLoopBlock,
    get_all_blocks,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import (
    BlockResult,
    BlockStatus,
    BlockType,
    BranchCriteriaYAML,
    ForLoopBlockYAML,
    TaskBlockYAML,
    WhileLoopBlockYAML,
    WorkflowDefinitionYAML,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_output_param(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_output",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _make_block_result(output_param: OutputParameter, status: BlockStatus = BlockStatus.completed) -> BlockResult:
    return BlockResult(
        success=status == BlockStatus.completed,
        output_parameter=output_param,
        output_parameter_value={"value": "ok"},
        status=status,
    )


def _make_while_loop(condition_expression: str = "{{ keep_going }}") -> WhileLoopBlock:
    inner = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
    return WhileLoopBlock(
        label="my_while",
        output_parameter=_make_output_param("my_while"),
        loop_blocks=[inner],
        condition=JinjaBranchCriteria(expression=condition_expression),
    )


# ---------------------------------------------------------------------------
# 1) Schema validation
# ---------------------------------------------------------------------------


class TestWhileLoopBlockYAMLSchema:
    """YAML-level schema validation for WhileLoopBlockYAML."""

    def test_jinja_condition_accepted(self) -> None:
        block = WhileLoopBlockYAML(
            label="loop",
            loop_blocks=[TaskBlockYAML(label="t", url="https://example.com")],
            condition=BranchCriteriaYAML(criteria_type="jinja2_template", expression="{{ x > 0 }}"),
        )
        assert block.block_type == BlockType.WHILE_LOOP
        assert block.condition.criteria_type == "jinja2_template"

    def test_prompt_condition_accepted_at_parse_time(self) -> None:
        # Forward-compat: prompt criteria parses cleanly. Execution-time rejection
        # is verified separately in the runtime tests below.
        block = WhileLoopBlockYAML(
            label="loop",
            loop_blocks=[TaskBlockYAML(label="t", url="https://example.com")],
            condition=BranchCriteriaYAML(criteria_type="prompt", expression="dates are recent"),
        )
        assert block.condition.criteria_type == "prompt"

    def test_missing_condition_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhileLoopBlockYAML(  # type: ignore[call-arg]
                label="loop",
                loop_blocks=[TaskBlockYAML(label="t", url="https://example.com")],
            )

    def test_round_trip_through_workflow_definition_yaml(self) -> None:
        yaml_def = WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                WhileLoopBlockYAML(
                    label="loop",
                    loop_blocks=[TaskBlockYAML(label="inner", url="https://example.com")],
                    condition=BranchCriteriaYAML(criteria_type="jinja2_template", expression="{{ a }}"),
                ),
            ],
        )
        # round-trip through dict — ensures discriminator works in both directions
        as_dict = yaml_def.model_dump()
        restored = WorkflowDefinitionYAML(**as_dict)
        assert restored.blocks[0].block_type == BlockType.WHILE_LOOP
        assert isinstance(restored.blocks[0], WhileLoopBlockYAML)


# ---------------------------------------------------------------------------
# 2) Validation: nested labels
# ---------------------------------------------------------------------------


class TestWhileLoopNestedLabelValidation:
    """Duplicate label detection across while_loop nesting."""

    def test_duplicate_label_top_level_vs_inside_while_loop(self) -> None:
        yaml_def = WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                TaskBlockYAML(label="dup", url="https://example.com"),
                WhileLoopBlockYAML(
                    label="loop",
                    loop_blocks=[TaskBlockYAML(label="dup", url="https://example.com")],
                    condition=BranchCriteriaYAML(criteria_type="jinja2_template", expression="{{ x }}"),
                ),
            ],
        )
        with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
            convert_workflow_definition(yaml_def, workflow_id="wf_test")

    def test_duplicate_label_for_loop_inside_while_loop(self) -> None:
        yaml_def = WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                WhileLoopBlockYAML(
                    label="outer_while",
                    loop_blocks=[
                        ForLoopBlockYAML(
                            label="inner_for",
                            loop_variable_reference="items",
                            loop_blocks=[TaskBlockYAML(label="dup", url="https://example.com")],
                        ),
                        TaskBlockYAML(label="dup", url="https://example.com"),
                    ],
                    condition=BranchCriteriaYAML(criteria_type="jinja2_template", expression="{{ x }}"),
                ),
            ],
        )
        with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
            convert_workflow_definition(yaml_def, workflow_id="wf_test")

    def test_unique_labels_in_nested_loops_pass(self) -> None:
        # No exception should be raised
        wf = WorkflowDefinition(
            parameters=[],
            blocks=[
                _make_while_loop(),
            ],
        )
        wf.validate()


# ---------------------------------------------------------------------------
# 3) Execution: top-of-loop semantics
# ---------------------------------------------------------------------------


class TestExecuteTopOfLoopSemantics:
    """Condition is evaluated before each iteration."""

    @pytest.mark.asyncio
    async def test_condition_false_on_first_check_skips_body(self) -> None:
        loop_block = _make_while_loop()
        mock_context = MagicMock()

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new_callable=AsyncMock, return_value=False),
            patch.object(Block, "execute_safe", new_callable=AsyncMock) as mock_execute_safe,
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            # Body never ran
            assert mock_execute_safe.call_count == 0
            assert result.outputs_with_loop_values == []
            assert result.block_outputs == []

    @pytest.mark.asyncio
    async def test_condition_true_twice_then_false_runs_two_iterations(self) -> None:
        loop_block = _make_while_loop()
        inner_block = loop_block.loop_blocks[0]
        inner_result = _make_block_result(inner_block.output_parameter)

        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.update_block_metadata = MagicMock()
        mock_context.set_value = MagicMock()

        condition_results = iter([True, True, False])

        async def fake_eval(_self: Any, _ctx: Any) -> bool:  # type: ignore[override]
            return next(condition_results)

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new=fake_eval),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            # Exactly two iterations executed
            assert len(result.outputs_with_loop_values) == 2
            assert len(result.block_outputs) == 2

            # Verify per-iteration metadata sets current_index 0 and 1, with
            # current_value / current_item explicitly nulled so a stale outer-loop
            # value can't leak into child Jinja expressions (SKY-8835).
            metadata_calls = [c.args for c in mock_context.update_block_metadata.call_args_list]
            indices_set = [args[1].get("current_index") for args in metadata_calls if isinstance(args[1], dict)]
            assert 0 in indices_set
            assert 1 in indices_set
            for args in metadata_calls:
                meta = args[1]
                assert meta.get("current_value") is None
                assert meta.get("current_item") is None

    @pytest.mark.asyncio
    async def test_metadata_explicitly_nulls_for_loop_keys(self) -> None:
        """SKY-8771 review fix (SKY-8835 dependency): when WhileLoopBlock writes its
        per-iteration metadata, ``current_value`` and ``current_item`` must be present
        as explicit None so that ``update_block_metadata``'s merge semantics overwrite
        any stale outer for_loop values rather than letting them linger.
        """
        loop_block = _make_while_loop()
        inner_block = loop_block.loop_blocks[0]
        inner_result = _make_block_result(inner_block.output_parameter)

        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.update_block_metadata = MagicMock()
        mock_context.set_value = MagicMock()

        condition_results = iter([True, False])

        async def fake_eval(_self: Any, _ctx: Any) -> bool:  # type: ignore[override]
            return next(condition_results)

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new=fake_eval),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            # Every metadata write must carry the for_loop keys with explicit None values.
            metadata_calls = [c.args[1] for c in mock_context.update_block_metadata.call_args_list]
            assert metadata_calls, "expected at least one metadata write"
            for meta in metadata_calls:
                assert "current_value" in meta and meta["current_value"] is None
                assert "current_item" in meta and meta["current_item"] is None
                assert isinstance(meta.get("current_index"), int)


# ---------------------------------------------------------------------------
# 4) Execution: max iterations safety
# ---------------------------------------------------------------------------


class TestExecuteMaxIterationsCap:
    @pytest.mark.asyncio
    async def test_condition_permanently_true_terminates_at_cap(self) -> None:
        loop_block = _make_while_loop()
        inner_block = loop_block.loop_blocks[0]
        inner_result = _make_block_result(inner_block.output_parameter)

        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.update_block_metadata = MagicMock()
        mock_context.set_value = MagicMock()

        # Patch the cap to a small number so the test is fast.
        with (
            patch("skyvern.forge.sdk.workflow.models.block.DEFAULT_MAX_LOOP_ITERATIONS", 5),
            patch.object(WhileLoopBlock, "_evaluate_condition", new_callable=AsyncMock, return_value=True),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            # 5 successful iterations + 1 final failure block result for the cap
            assert len(result.outputs_with_loop_values) == 5
            assert result.block_outputs[-1].success is False
            assert result.block_outputs[-1].status == BlockStatus.failed
            assert "max_loop_iterations" in (result.block_outputs[-1].failure_reason or "")

    @pytest.mark.asyncio
    async def test_condition_false_on_cap_plus_one_check_succeeds(self) -> None:
        """SKY-8771 review fix: a loop that completes exactly N=cap iterations and would
        naturally exit on the next condition check must succeed, not trip the cap.

        With cap=3 and condition iter([True, True, True, False]), the loop must run 3
        bodies and then exit cleanly when the 4th check returns False — *not* return
        a max_loop_iterations failure.
        """
        loop_block = _make_while_loop()
        inner_block = loop_block.loop_blocks[0]
        inner_result = _make_block_result(inner_block.output_parameter)

        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.update_block_metadata = MagicMock()
        mock_context.set_value = MagicMock()

        condition_results = iter([True, True, True, False])

        async def fake_eval(_self: Any, _ctx: Any) -> bool:  # type: ignore[override]
            return next(condition_results)

        with (
            patch("skyvern.forge.sdk.workflow.models.block.DEFAULT_MAX_LOOP_ITERATIONS", 3),
            patch.object(WhileLoopBlock, "_evaluate_condition", new=fake_eval),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=inner_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            # Exactly 3 body iterations executed, then condition false → clean exit.
            assert len(result.outputs_with_loop_values) == 3
            assert len(result.block_outputs) == 3
            # No max_loop_iterations failure result was appended.
            assert all("max_loop_iterations" not in (b.failure_reason or "") for b in result.block_outputs)


# ---------------------------------------------------------------------------
# 5) Execution: condition rendering errors
# ---------------------------------------------------------------------------


class TestCurrentIndexWrittenBeforeCondition:
    """Before each condition check, the WhileLoopBlock writes ``current_index`` to its
    own block metadata so the existing for_loop injection in
    ``format_block_parameter_template_from_workflow_run_context`` exposes it to the
    condition's template scope. Authors can then bootstrap iteration 0 with
    ``{{ current_index == 0 or <body_output_ref> }}``."""

    @pytest.mark.asyncio
    async def test_self_label_metadata_includes_current_index_zero_before_first_eval(self) -> None:
        """Iteration 0's condition check sees ``current_index = 0`` written to the
        WhileLoopBlock's own metadata BEFORE the eval lambda fires. Captured by
        snapshotting ``update_block_metadata.call_args_list`` from inside the fake
        evaluator and asserting the expected write happened first."""
        loop_block = _make_while_loop()
        mock_context = MagicMock()
        mock_context.update_block_metadata = MagicMock()
        prior_calls_at_first_eval: list[Any] = []

        async def fake_eval(_self: Any, ctx: Any) -> bool:  # type: ignore[override]
            if not prior_calls_at_first_eval:
                prior_calls_at_first_eval.extend(ctx.update_block_metadata.call_args_list)
            return False  # exit immediately after the first check

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new=fake_eval),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

        self_label_writes_before_eval = [
            c
            for c in prior_calls_at_first_eval
            if c.args[0] == loop_block.label and c.args[1].get("current_index") == 0
        ]
        assert self_label_writes_before_eval, (
            f"Expected current_index=0 written to self.label before first condition eval; "
            f"got {prior_calls_at_first_eval}"
        )


class TestExecuteConditionRenderingErrors:
    @pytest.mark.asyncio
    async def test_failed_jinja_format_returns_failure_result(self) -> None:
        loop_block = _make_while_loop()
        mock_context = MagicMock()

        async def raise_format_error(_self: Any, _ctx: Any) -> bool:  # type: ignore[override]
            raise FailedToFormatJinjaStyleParameter("{{ ??? }}", "syntax error")

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new=raise_format_error),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            assert len(result.block_outputs) == 1
            assert result.block_outputs[0].success is False
            assert "Failed to evaluate while-loop condition" in (result.block_outputs[0].failure_reason or "")

    @pytest.mark.asyncio
    async def test_missing_jinja_variables_returns_failure_result(self) -> None:
        loop_block = _make_while_loop()
        mock_context = MagicMock()

        async def raise_missing(_self: Any, _ctx: Any) -> bool:  # type: ignore[override]
            raise MissingJinjaVariables("{{ undefined_var }}", {"undefined_var"})

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new=raise_missing),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            assert len(result.block_outputs) == 1
            assert result.block_outputs[0].success is False


# ---------------------------------------------------------------------------
# 6) Cancellation propagation
# ---------------------------------------------------------------------------


class TestExecuteCancellationPropagation:
    @pytest.mark.asyncio
    async def test_canceled_child_terminates_loop_with_partial_outputs(self) -> None:
        loop_block = _make_while_loop()
        inner_block = loop_block.loop_blocks[0]
        canceled_result = _make_block_result(inner_block.output_parameter, status=BlockStatus.canceled)

        mock_context = MagicMock()
        mock_context.has_value.return_value = False
        mock_context.update_block_metadata = MagicMock()
        mock_context.set_value = MagicMock()

        with (
            patch.object(WhileLoopBlock, "_evaluate_condition", new_callable=AsyncMock, return_value=True),
            patch.object(Block, "execute_safe", new_callable=AsyncMock, return_value=canceled_result),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
            patch("skyvern.forge.sdk.workflow.models.block.skyvern_context") as mock_skyvern_ctx,
        ):
            mock_skyvern_ctx.current.return_value = None
            mock_app.DATABASE.workflow_runs.create_or_update_workflow_run_output_parameter = AsyncMock()
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._execute_while_loop_helper(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                workflow_run_context=mock_context,
                organization_id="org_test",
            )

            # One iteration ran and was canceled
            assert len(result.outputs_with_loop_values) == 1
            assert result.is_canceled() is True


# ---------------------------------------------------------------------------
# 7) Prompt criteria rejected at runtime
# ---------------------------------------------------------------------------


class TestPromptCriteriaRejected:
    @pytest.mark.asyncio
    async def test_prompt_criteria_returns_clear_failure(self) -> None:
        inner = TaskBlock(label="inner_task", output_parameter=_make_output_param("inner_task"))
        loop_block = WhileLoopBlock(
            label="my_while",
            output_parameter=_make_output_param("my_while"),
            loop_blocks=[inner],
            condition=PromptBranchCriteria(expression="dates on the page are still recent"),
        )

        mock_context = MagicMock()

        with (
            patch.object(Block, "get_workflow_run_context", return_value=mock_context),
            patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        ):
            mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()

            result = await loop_block._run_loop(
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_loop",
                organization_id="org_test",
            )

            assert result.success is False
            assert result.status == BlockStatus.failed
            assert "Prompt criteria are not yet supported" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# 8) get_all_blocks recursion
# ---------------------------------------------------------------------------


class TestGetAllBlocksRecursion:
    def test_get_all_blocks_recurses_into_while_loop(self) -> None:
        inner_a = TaskBlock(label="a", output_parameter=_make_output_param("a"))
        inner_b = TaskBlock(label="b", output_parameter=_make_output_param("b"))
        loop_block = WhileLoopBlock(
            label="loop",
            output_parameter=_make_output_param("loop"),
            loop_blocks=[inner_a, inner_b],
            condition=JinjaBranchCriteria(expression="{{ x }}"),
        )

        all_blocks = get_all_blocks([loop_block])
        labels = [b.label for b in all_blocks]
        assert labels == ["loop", "a", "b"]

    def test_get_all_blocks_recurses_into_nested_for_inside_while(self) -> None:
        deep = TaskBlock(label="deep", output_parameter=_make_output_param("deep"))
        for_loop = ForLoopBlock(
            label="inner_for",
            output_parameter=_make_output_param("inner_for"),
            loop_blocks=[deep],
            loop_variable_reference="items",
        )
        while_loop = WhileLoopBlock(
            label="outer_while",
            output_parameter=_make_output_param("outer_while"),
            loop_blocks=[for_loop],
            condition=JinjaBranchCriteria(expression="{{ x }}"),
        )

        all_blocks = get_all_blocks([while_loop])
        labels = [b.label for b in all_blocks]
        assert labels == ["outer_while", "inner_for", "deep"]
