"""Tests for prompt-based conditional branch evaluation behavior."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    BranchEvaluationContext,
    ConditionalBlock,
    PromptBranchCriteria,
    _make_empty_params_explicit,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.workflows import BlockResult


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{key}_id",
        key=key,
        workflow_id="wf",
        created_at=now,
        modified_at=now,
    )


def _conditional_block() -> ConditionalBlock:
    return ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("conditional_output"),
        branch_conditions=[
            BranchCondition(criteria=PromptBranchCriteria(expression="fallback"), next_block_label="next"),
            BranchCondition(is_default=True, next_block_label=None),
        ],
    )


def _extraction_result(output_parameter: OutputParameter, evaluations: list[dict]) -> BlockResult:
    return BlockResult(
        success=True,
        output_parameter=output_parameter,
        output_parameter_value={"evaluations": evaluations},
        failure_reason=None,
    )


@pytest.mark.asyncio
async def test_jinja_rendered_prompt_condition_omits_browser_session() -> None:
    """When all expressions are fully Jinja-rendered, ExtractionBlock should be used
    but with browser_session_id=None to prevent the LLM from reinterpreting resolved
    literal values as on-screen references (SKY-7985)."""
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression='{{Single_or_Joint__c}} == "Joint"'),
        next_block_label="joint",
    )

    evaluation_context = BranchEvaluationContext(
        workflow_run_context=None,
        template_renderer=lambda expr: expr.replace("{{Single_or_Joint__c}}", "Joint"),
    )
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"Single_or_Joint__c": "Joint"})  # type: ignore[method-assign]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal") as mock_prompt,
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [{"reasoning": "ok", "result": True}],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        results, rendered_expressions, _, llm_response = await block._evaluate_prompt_branches(
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
            browser_session_id="bs_test",
        )

    assert results == [True]
    assert rendered_expressions == ['Joint == "Joint"']
    # ExtractionBlock should be called with browser_session_id=None (not "bs_test")
    mock_extraction.execute.assert_awaited_once()
    assert mock_extraction.execute.call_args.kwargs["browser_session_id"] is None
    # No context should be passed when all expressions are Jinja-rendered
    evaluation_context.build_llm_safe_context_snapshot.assert_not_called()  # type: ignore[attr-defined]
    assert mock_prompt.call_args.kwargs["context_json"] is None


@pytest.mark.asyncio
async def test_pure_natlang_prompt_condition_uses_browser_session_and_context() -> None:
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"),
        next_block_label="premium",
    )

    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"plan": "premium"})  # type: ignore[method-assign]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal") as mock_prompt,
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [
                    {
                        "rendered_condition": "user selected premium plan",
                        "reasoning": "ok",
                        "result": True,
                    }
                ],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        await block._evaluate_prompt_branches(
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
            browser_session_id="bs_test",
        )

    assert mock_extraction.execute.call_args.kwargs["browser_session_id"] == "bs_test"
    evaluation_context.build_llm_safe_context_snapshot.assert_called_once()  # type: ignore[attr-defined]
    assert mock_prompt.call_args.kwargs["context_json"] is not None


@pytest.mark.asyncio
async def test_mixed_prompt_conditions_keep_browser_session() -> None:
    block = _conditional_block()
    branches = [
        BranchCondition(
            criteria=PromptBranchCriteria(expression="{{var}} == 'value'"),
            next_block_label="jinja_branch",
        ),
        BranchCondition(
            criteria=PromptBranchCriteria(expression="user selected premium plan"),
            next_block_label="natlang_branch",
        ),
    ]

    evaluation_context = BranchEvaluationContext(
        workflow_run_context=None,
        template_renderer=lambda expr: expr.replace("{{var}}", "value"),
    )
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"var": "value"})  # type: ignore[method-assign]

    with patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls:
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [
                    {"rendered_condition": "value == 'value'", "reasoning": "ok", "result": True},
                    {
                        "rendered_condition": "user selected premium plan",
                        "reasoning": "ok",
                        "result": False,
                    },
                ],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
            browser_session_id="bs_test",
        )

    assert mock_extraction.execute.call_args.kwargs["browser_session_id"] == "bs_test"
    evaluation_context.build_llm_safe_context_snapshot.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_jinja_render_failure_falls_back_to_extraction_block() -> None:
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression='{{Single_or_Joint__c}} == "Joint"'),
        next_block_label="joint",
    )

    def _raise_render_error(_: str) -> str:
        raise RuntimeError("render failed")

    evaluation_context = BranchEvaluationContext(
        workflow_run_context=None,
        template_renderer=_raise_render_error,
    )
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"Single_or_Joint__c": "Joint"})  # type: ignore[method-assign]
    mock_llm_handler = AsyncMock()

    with (
        patch.dict(block_module.app.__dict__, {"LLM_API_HANDLER": mock_llm_handler}),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [{"rendered_condition": '{{Single_or_Joint__c}} == "Joint"', "reasoning": "ok", "result": False}],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        await block._evaluate_prompt_branches(
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
            browser_session_id="bs_test",
        )

    mock_extraction.execute.assert_awaited_once()
    assert mock_extraction.execute.call_args.kwargs["browser_session_id"] == "bs_test"
    mock_llm_handler.assert_not_called()
    evaluation_context.build_llm_safe_context_snapshot.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests for _make_empty_params_explicit  (SKY-8073)
# ---------------------------------------------------------------------------


class TestMakeEmptyParamsExplicit:
    """Unit tests for _make_empty_params_explicit helper."""

    def test_empty_param_is_patched(self) -> None:
        """When a single parameter resolves to empty string, it should be replaced with (empty value)."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="if {{test_parameter}} is not empty",
            rendered_expression="if  is not empty",
        )
        assert was_patched is True
        assert patched == "if (empty value) is not empty"

    def test_non_empty_param_is_not_patched(self) -> None:
        """Non-empty parameter values should pass through unchanged."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="if {{test_parameter}} is not empty",
            rendered_expression="if hello is not empty",
        )
        assert was_patched is False
        assert patched == "if hello is not empty"

    def test_no_jinja_blocks(self) -> None:
        """Expressions without Jinja blocks should pass through unchanged."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="user selected premium plan",
            rendered_expression="user selected premium plan",
        )
        assert was_patched is False
        assert patched == "user selected premium plan"

    def test_multiple_params_one_empty(self) -> None:
        """When one of multiple parameters is empty, only that one should be patched."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="{{a}} equals {{b}}",
            rendered_expression=" equals hello",
        )
        assert was_patched is True
        assert patched == "(empty value) equals hello"

    def test_multiple_params_both_empty(self) -> None:
        """When all parameters are empty, all should be patched."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="{{a}} equals {{b}}",
            rendered_expression=" equals ",
        )
        assert was_patched is True
        assert patched == "(empty value) equals (empty value)"

    def test_whitespace_only_param_is_patched(self) -> None:
        """A parameter that resolves to whitespace-only should be treated as empty."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="if {{test_parameter}} is not empty",
            rendered_expression="if    is not empty",
        )
        assert was_patched is True
        assert patched == "if (empty value) is not empty"

    def test_empty_original_expression(self) -> None:
        """Empty original expression should pass through."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="",
            rendered_expression="",
        )
        assert was_patched is False

    def test_adjacent_variables_are_skipped(self) -> None:
        """Adjacent Jinja variables (no separator) cannot be reliably split, so skip patching."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="{{a}}{{b}}",
            rendered_expression="helloworld",
        )
        assert was_patched is False
        assert patched == "helloworld"

    def test_param_at_end_of_expression(self) -> None:
        """Parameter at the end of an expression should be handled correctly."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="check if empty: {{param}}",
            rendered_expression="check if empty: ",
        )
        assert was_patched is True
        assert patched == "check if empty: (empty value)"

    def test_single_bare_variable_empty(self) -> None:
        """Entire expression is one variable that resolved to empty string."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="{{a}}",
            rendered_expression="",
        )
        assert was_patched is True
        assert patched == "(empty value)"

    def test_rendered_value_containing_static_anchor(self) -> None:
        """When a rendered value contains static anchor text, regex may mis-split.
        Verify we don't falsely detect an empty parameter."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="{{a}} equals {{b}}",
            rendered_expression="x equals y equals z",
        )
        assert was_patched is False
        assert patched == "x equals y equals z"

    def test_multiline_rendered_value_passes_through(self) -> None:
        """Multiline rendered values (re.DOTALL path) should not be falsely patched."""
        patched, was_patched = _make_empty_params_explicit(
            original_expression="if {{data}} is valid",
            rendered_expression="if line1\nline2 is valid",
        )
        assert was_patched is False
        assert patched == "if line1\nline2 is valid"


@pytest.mark.asyncio
async def test_empty_param_produces_explicit_marker_in_prompt_evaluation() -> None:
    """Integration test: when a parameter resolves to empty string, the rendered
    expression sent to the LLM should contain '(empty value)' so the LLM can
    correctly evaluate the condition (SKY-8073)."""
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="if {{test_parameter}} is not empty"),
        next_block_label="not_empty_branch",
    )

    evaluation_context = BranchEvaluationContext(
        workflow_run_context=None,
        template_renderer=lambda expr: expr.replace("{{test_parameter}}", ""),
    )
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"test_parameter": ""})  # type: ignore[method-assign]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal") as mock_prompt,
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [{"reasoning": "empty value is not empty -> false", "result": False}],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        results, rendered_expressions, _, _ = await block._evaluate_prompt_branches(
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
        )

    assert results == [False]
    # The rendered expression should contain the explicit marker, not a bare gap
    assert rendered_expressions == ["if (empty value) is not empty"]
    # The prompt should be loaded with the patched expression
    assert mock_prompt.call_args.kwargs["conditions"] == ["if (empty value) is not empty"]
