"""Tests for prompt-based conditional branch evaluation behavior."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.config import settings
from skyvern.exceptions import BranchEvaluationContextTooLargeError, ConditionalBranchEvaluationError
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.exceptions import MissingJinjaVariables
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    BranchEvaluationContext,
    ConditionalBlock,
    ExtractionBlock,
    JinjaBranchCriteria,
    PromptBranchCriteria,
    _build_branch_evaluation_schema,
    _coerce_condition_index,
    _make_empty_params_explicit,
    _neutralize_jinja_delimiters,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import BlockResult, BlockStatus
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext

BRANCH_CONTEXT_TOO_LARGE_FAILURE_REASON = (
    "Workflow branch evaluation context is too large to process safely. "
    "Reduce the workflow input or prior block output size, then retry."
)


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
async def test_jinja_rendered_prompt_condition_keeps_browser_session() -> None:
    """When all expressions are fully Jinja-rendered, ExtractionBlock should still
    receive the browser_session_id so that page-referencing conditions (e.g.
    "the date on the page matches {{date}}") can see the screenshot.  The prompt
    template instructs the LLM to only use page content when the condition
    explicitly references the page (SKY-8465)."""
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
    # ExtractionBlock should be called with the real browser_session_id
    mock_extraction.execute.assert_awaited_once()
    assert mock_extraction.execute.call_args.kwargs["browser_session_id"] == "bs_test"
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
async def test_oversized_branch_context_trips_before_extraction_and_returns_stable_failure() -> None:
    # No default branch: with one defined, evaluation failures now route to it (SKY-14080)
    # instead of surfacing this stable failure.
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("conditional_output"),
        branch_conditions=[
            BranchCondition(criteria=PromptBranchCriteria(expression="fallback"), next_block_label="next"),
        ],
    )
    oversized_context_value = "oversized-context-value " * 200_000

    with (
        patch.object(
            BranchEvaluationContext,
            "build_llm_safe_context_snapshot",
            return_value={"sensitive_customer_key": oversized_context_value},
        ),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch("skyvern.forge.sdk.workflow.models.block.diagnostic_fingerprint", return_value="key-fingerprint"),
        patch.object(block_module.LOG, "warning") as mock_warning,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, "downstream prompt failed")
        )
        mock_extraction_cls.return_value = mock_extraction
        mock_build_result.return_value = BlockResult(
            success=False,
            output_parameter=block.output_parameter,
            output_parameter_value=None,
            failure_reason=BRANCH_CONTEXT_TOO_LARGE_FAILURE_REASON,
        )

        result = await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
        )

    assert result.failure_reason == BRANCH_CONTEXT_TOO_LARGE_FAILURE_REASON
    assert mock_build_result.await_args.kwargs["failure_reason"] == BRANCH_CONTEXT_TOO_LARGE_FAILURE_REASON
    mock_extraction_cls.assert_not_called()

    circuit_logs = [
        call
        for call in mock_warning.call_args_list
        if call.args[0] == "conditional_branch_context_circuit_breaker_tripped"
    ]
    assert len(circuit_logs) == 1
    circuit_log = circuit_logs[0]
    assert circuit_log.kwargs["goal_token_count"] > 180_000
    assert circuit_log.kwargs["max_goal_tokens"] == 150_000
    assert circuit_log.kwargs["reserved_tokens"] == 30_000
    assert circuit_log.kwargs["context_key_count"] == 1
    assert circuit_log.kwargs["top_context_contributors"] == [
        {
            "key_fingerprint": "key-fingerprint",
            "serialized_bytes": len(f'"{oversized_context_value}"'.encode()),
            "token_count": circuit_log.kwargs["top_context_contributors"][0]["token_count"],
        }
    ]
    assert "sensitive_customer_key" not in str(circuit_log)
    assert oversized_context_value not in str(circuit_log)


@pytest.mark.asyncio
async def test_branch_goal_at_reserved_budget_boundary_still_executes_unchanged() -> None:
    block = _conditional_block()
    branch = block.branch_conditions[0]
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"plan": "premium"})  # type: ignore[method-assign]

    def _token_count(value: str) -> int:
        return 150_000 if value == "goal-at-boundary" else 2

    with (
        patch(
            "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
            return_value="goal-at-boundary",
        ) as mock_prompt,
        patch("skyvern.forge.sdk.workflow.models.block.count_tokens", side_effect=_token_count) as mock_count_tokens,
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [{"condition_index": 1, "reasoning": "ok", "result": True}],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        results, _, extraction_goal, _ = await block._evaluate_prompt_branches(
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
        )

    assert results == [True]
    assert extraction_goal == "goal-at-boundary"
    assert mock_extraction_cls.call_args.kwargs["data_extraction_goal"] == "goal-at-boundary"
    assert mock_prompt.call_args.kwargs["context_json"] == '{"plan": "premium"}'
    assert any(call.args == ("goal-at-boundary",) for call in mock_count_tokens.call_args_list)


@pytest.mark.asyncio
async def test_branch_goal_above_reserved_budget_trips_before_extraction() -> None:
    block = _conditional_block()
    branch = block.branch_conditions[0]
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"plan": "premium"})  # type: ignore[method-assign]

    def _token_count(value: str) -> int:
        return 150_001 if value == "goal-above-boundary" else 2

    with (
        patch(
            "skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt",
            return_value="goal-above-boundary",
        ),
        patch("skyvern.forge.sdk.workflow.models.block.count_tokens", side_effect=_token_count),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch("skyvern.forge.sdk.workflow.models.block.diagnostic_fingerprint", return_value="key-fingerprint"),
    ):
        with pytest.raises(BranchEvaluationContextTooLargeError):
            await block._evaluate_prompt_branches(
                branches=[branch],
                evaluation_context=evaluation_context,
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                organization_id="org_test",
            )

    mock_extraction_cls.assert_not_called()


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


# ---------------------------------------------------------------------------
# Tests for None failure_reason guard in _evaluate_prompt_branches (SKY-8026)
# ---------------------------------------------------------------------------


def _failed_extraction_result(output_parameter: OutputParameter, failure_reason: str | None = None) -> BlockResult:
    return BlockResult(
        success=False,
        output_parameter=output_parameter,
        output_parameter_value=None,
        failure_reason=failure_reason,
    )


@pytest.mark.asyncio
async def test_extraction_failure_with_none_reason_produces_informative_error() -> None:
    """When ExtractionBlock fails with failure_reason=None, the raised ValueError
    should NOT contain the literal string 'None' (SKY-8026)."""
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"),
        next_block_label="premium",
    )

    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, failure_reason=None)
        )
        mock_extraction_cls.return_value = mock_extraction

        with pytest.raises(ConditionalBranchEvaluationError, match="Unknown error"):
            await block._evaluate_prompt_branches(
                branches=[branch],
                evaluation_context=evaluation_context,
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                organization_id="org_test",
            )


@pytest.mark.asyncio
async def test_extraction_failure_with_reason_preserves_original_message() -> None:
    """When ExtractionBlock fails with a real failure_reason, that reason should
    appear verbatim in the raised ValueError."""
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"),
        next_block_label="premium",
    )

    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, failure_reason="LLM rate limited")
        )
        mock_extraction_cls.return_value = mock_extraction

        with pytest.raises(ConditionalBranchEvaluationError, match="LLM rate limited"):
            await block._evaluate_prompt_branches(
                branches=[branch],
                evaluation_context=evaluation_context,
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                organization_id="org_test",
            )


@pytest.mark.asyncio
async def test_extra_placeholder_evals_recovered_when_well_formed_subset_matches() -> None:
    """LLM sometimes returns N+k evaluations for N branches, where the k extras have
    reasoning=None. Matches the exact shape observed in production (wr_530455567744647688):
    1 branch, 8 evaluations returned, entry 0 has real reasoning, entries 1-7 are
    reasoning=None placeholders. The fix should strip the extras and return [False]."""
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(
            expression="Does one of the accounts have both a reader_name not ending with 'pdf' and a purpose of 'Invoice'"
        ),
        next_block_label="invoice_branch",
    )

    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    # One well-formed entry followed by 7 reasoning=None placeholders — exact production shape.
    raw_evals = [
        {"reasoning": "Neither account has purpose 'Invoice', so the condition is False.", "result": False},
        {"reasoning": None, "result": False},
        {"reasoning": None, "result": False},
        {"reasoning": None, "result": False},
        {"reasoning": None, "result": False},
        {"reasoning": None, "result": False},
        {"reasoning": None, "result": False},
        {"reasoning": None, "result": False},
    ]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
        )

    assert results == [False]


@pytest.mark.asyncio
async def test_extra_evals_not_recovered_when_well_formed_count_does_not_match() -> None:
    """If stripping reasoning=None entries does NOT yield exactly len(branches) results,
    the function should still raise ValueError rather than silently returning wrong data."""
    block = _conditional_block()
    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="some condition"),
        next_block_label="branch_a",
    )

    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    # 2 well-formed + 1 placeholder for 1 branch — filter yields 2, not 1, so no recovery.
    raw_evals = [
        {"reasoning": "sub-eval A", "result": True},
        {"reasoning": "sub-eval B", "result": False},
        {"reasoning": None, "result": False},
    ]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        with pytest.raises(ConditionalBranchEvaluationError, match="3 results for 1 branches"):
            await block._evaluate_prompt_branches(
                branches=[branch],
                evaluation_context=evaluation_context,
                workflow_run_id="wr_test",
                workflow_run_block_id="wrb_test",
                organization_id="org_test",
            )


@pytest.mark.asyncio
async def test_extra_placeholder_evals_multi_branch_preserves_order() -> None:
    """With 2 branches and interleaved placeholders, the filter must preserve the order
    of well-formed entries so results[0] maps to branch 0 and results[1] maps to branch 1."""
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(criteria=PromptBranchCriteria(expression="condition A"), next_block_label="a"),
            BranchCondition(criteria=PromptBranchCriteria(expression="condition B"), next_block_label="b"),
        ],
    )
    branches = [c for c in block.branch_conditions if not c.is_default]

    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    # Real evals for branch 0 (True) and branch 1 (False) interleaved with placeholders.
    raw_evals = [
        {"reasoning": "branch 0 reasoning", "result": True},
        {"reasoning": None, "result": False},
        {"reasoning": "branch 1 reasoning", "result": False},
        {"reasoning": None, "result": False},
    ]

    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id="wr_test",
            workflow_run_block_id="wrb_test",
            organization_id="org_test",
        )

    assert results == [True, False]


def test_prompt_template_includes_count_and_atomicity_for_compound_conditions() -> None:
    rendered_one = prompt_engine.load_prompt(
        "conditional-prompt-branch-evaluation",
        conditions=["If A and if B"],
        context_json=None,
    )
    rendered_two = prompt_engine.load_prompt(
        "conditional-prompt-branch-evaluation",
        conditions=["A", "B"],
        context_json=None,
    )

    assert "exactly 1" in rendered_one
    assert "exactly 2" in rendered_two

    # Wording-coupled: if rephrased, confirm the replacement still conveys atomicity.
    assert "split" in rendered_one.lower()


# ---------------------------------------------------------------------------
# Tests for condition_index alignment + malformed-batch retry (SKY-10682)
# ---------------------------------------------------------------------------


def _two_branch_block() -> ConditionalBlock:
    return ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(criteria=PromptBranchCriteria(expression="condition A"), next_block_label="a"),
            BranchCondition(criteria=PromptBranchCriteria(expression="condition B"), next_block_label="b"),
        ],
    )


def _no_context() -> BranchEvaluationContext:
    ctx = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    ctx.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]
    return ctx


@pytest.mark.asyncio
async def test_condition_index_alignment_is_order_independent() -> None:
    """Evaluations carrying condition_index must align by index, not position, so a
    reversed-order LLM response still maps each result to the correct branch."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    # Returned out of order: condition 2 first (True), then condition 1 (False).
    raw_evals = [
        {"condition_index": 2, "reasoning": "B", "result": True},
        {"condition_index": 1, "reasoning": "A", "result": False},
    ]
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    assert results == [False, True]  # branch 0 (index 1) -> False, branch 1 (index 2) -> True


@pytest.mark.asyncio
async def test_hallucinated_unindexed_entry_does_not_misroute() -> None:
    """A hallucinated extra entry WITHOUT a condition_index (the shape that shifted
    positional alignment in SKY-10682) must be ignored; indexed entries align to the
    correct branches instead of misrouting."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    raw_evals = [
        {"reasoning": "hallucinated off-topic text", "result": True},  # junk, no condition_index
        {"condition_index": 1, "reasoning": "A", "result": False},
        {"condition_index": 2, "reasoning": "B", "result": True},
    ]
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    assert results == [False, True]


@pytest.mark.asyncio
async def test_under_return_retries_then_succeeds() -> None:
    """The SKY-10682 failure shape: the LLM returns fewer results than branches on the
    first attempt, then a clean response on retry. The batch must retry and succeed
    rather than failing the whole run."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    first = _extraction_result(block.output_parameter, [{"condition_index": 1, "reasoning": "A", "result": False}])
    second = _extraction_result(
        block.output_parameter,
        [
            {"condition_index": 1, "reasoning": "A", "result": False},
            {"condition_index": 2, "reasoning": "B", "result": True},
        ],
    )
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(side_effect=[first, second])
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    assert results == [False, True]
    assert mock_extraction.execute.await_count == 2


@pytest.mark.asyncio
async def test_under_return_fails_loudly_after_retries_exhausted() -> None:
    """If every attempt returns a malformed batch, the evaluation must fail loudly
    (raise) rather than silently routing to a default/wrong branch."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    bad = _extraction_result(block.output_parameter, [{"condition_index": 1, "reasoning": "A", "result": False}])
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=bad)
        mock_extraction_cls.return_value = mock_extraction

        with pytest.raises(ConditionalBranchEvaluationError):
            await block._evaluate_prompt_branches(
                branches=branches,
                evaluation_context=_no_context(),
                workflow_run_id="wr",
                workflow_run_block_id="wrb",
                organization_id="org",
            )
        assert mock_extraction.execute.await_count >= 2


@pytest.mark.asyncio
async def test_retry_varies_extraction_goal_for_true_reroll() -> None:
    """On retry the extraction goal must differ from the first attempt so the extraction
    cache key (which includes data_extraction_goal) changes and we get a genuine re-roll
    instead of replaying a cached malformed result (SKY-10682)."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    bad = _extraction_result(block.output_parameter, [{"condition_index": 1, "reasoning": "A", "result": False}])
    good = _extraction_result(
        block.output_parameter,
        [
            {"condition_index": 1, "reasoning": "A", "result": False},
            {"condition_index": 2, "reasoning": "B", "result": True},
        ],
    )
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(side_effect=[bad, good])
        mock_extraction_cls.return_value = mock_extraction

        await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    goals = [call.kwargs["data_extraction_goal"] for call in mock_extraction_cls.call_args_list]
    assert len(goals) == 2
    assert goals[0] != goals[1]


@pytest.mark.asyncio
async def test_branch_eval_schema_is_strict_and_indexed() -> None:
    """The data_schema must require condition_index and forbid extra keys
    (additionalProperties: false) so the LLM cannot inject hallucinated fields like the
    off-topic `including` key seen in production (SKY-10682)."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    good = _extraction_result(
        block.output_parameter,
        [
            {"condition_index": 1, "reasoning": "A", "result": False},
            {"condition_index": 2, "reasoning": "B", "result": True},
        ],
    )
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=good)
        mock_extraction_cls.return_value = mock_extraction

        await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    schema = mock_extraction_cls.call_args.kwargs["data_schema"]
    item_schema = schema["properties"]["evaluations"]["items"]
    assert item_schema["additionalProperties"] is False
    assert "condition_index" in item_schema["properties"]
    assert "condition_index" in item_schema["required"]


def test_prompt_template_requests_condition_index() -> None:
    rendered = prompt_engine.load_prompt(
        "conditional-prompt-branch-evaluation",
        conditions=["A", "B"],
        context_json=None,
    )
    assert "condition_index" in rendered


def test_build_branch_evaluation_schema_is_strict_and_indexed() -> None:
    schema = _build_branch_evaluation_schema(3)
    evaluations = schema["properties"]["evaluations"]
    assert evaluations["minItems"] == 3
    assert evaluations["maxItems"] == 3
    item = evaluations["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["condition_index", "reasoning", "result"]
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_string_condition_index_out_of_order_aligns_by_index() -> None:
    """The schema requests an integer condition_index but isn't provider-enforced, so the model
    may type it as a string. Out of order, positional alignment would misroute; digit strings must
    coerce to int and stay on the index-aligned path (SKY-10682)."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    raw_evals = [
        {"condition_index": "2", "reasoning": "B", "result": True},
        {"condition_index": "1", "reasoning": "A", "result": False},
    ]
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    assert results == [False, True]


@pytest.mark.asyncio
async def test_float_condition_index_out_of_order_aligns_by_index() -> None:
    """An integral-float condition_index (e.g. 2.0) must align by index too, not fall back to
    positional ordering, which would misroute on a reversed batch (SKY-10682)."""
    block = _two_branch_block()
    branches = [c for c in block.branch_conditions if not c.is_default]

    raw_evals = [
        {"condition_index": 2.0, "reasoning": "B", "result": True},
        {"condition_index": 1.0, "reasoning": "A", "result": False},
    ]
    with (
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal"),
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(return_value=_extraction_result(block.output_parameter, raw_evals))
        mock_extraction_cls.return_value = mock_extraction

        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=_no_context(),
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    assert results == [False, True]


def test_coerce_condition_index_handles_loose_types() -> None:
    assert _coerce_condition_index(2) == 2
    assert _coerce_condition_index(2.0) == 2
    assert _coerce_condition_index("2") == 2
    assert _coerce_condition_index("  3 ") == 3
    # bool is an int subclass but is never a valid index
    assert _coerce_condition_index(True) is None
    assert _coerce_condition_index(False) is None
    # non-integral / unparseable values are rejected
    assert _coerce_condition_index(2.5) is None
    assert _coerce_condition_index("two") is None
    assert _coerce_condition_index("") is None
    assert _coerce_condition_index(None) is None


# ---------------------------------------------------------------------------
# Tests for stored-Jinja injection into branch-eval prompts + default-branch
# fallback on evaluation failure (SKY-14080)
# ---------------------------------------------------------------------------

_ALL_JINJA_DELIMITERS = ("{{", "}}", "{%", "%}", "{#", "#}")
# JSON structure itself produces adjacent `}}` when nested objects close together; without an
# opening delimiter that text is inert in Jinja, so only openers must never appear in full text.
_JINJA_OPENING_DELIMITERS = ("{{", "{%", "{#")


def _iter_strings(value):  # noqa: ANN001, ANN202
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


@pytest.mark.asyncio
async def test_stored_jinja_in_prior_output_does_not_break_branch_evaluation(monkeypatch) -> None:
    """Regression: a prior block's stored output containing a literal ``{{current_value.x}}``
    used to be embedded verbatim in the branch-eval goal and re-rendered by the synthetic
    ExtractionBlock, raising UndefinedError/MissingJinjaVariables under strict templating."""
    monkeypatch.setattr(settings, "WORKFLOW_TEMPLATING_STRICTNESS", "strict")
    fake_ctx = FakeWorkflowRunContext(
        values={
            "prior_conditional_output": {
                "criteria_expression": "{{ current_value.account_number }} == '123'",
                "original_expression": "{{ current_value.account_number }} == '123'",
                "branch_taken": "path_a",
            },
        },
    )
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(criteria=PromptBranchCriteria(expression="the account is active"), next_block_label="a"),
            BranchCondition(criteria=PromptBranchCriteria(expression="the account is closed"), next_block_label="b"),
            BranchCondition(is_default=True, next_block_label="fallback"),
        ],
    )
    branches = [c for c in block.branch_conditions if not c.is_default]
    evaluation_context = BranchEvaluationContext(
        workflow_run_context=fake_ctx,
        block_label="cond",
        template_renderer=lambda expr: expr,
    )

    captured: dict = {}

    async def _execute_after_real_format(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        # Run the real second-render step the production ExtractionBlock.execute performs; the
        # bug fired here as an UndefinedError on the embedded `{{current_value...}}` text.
        self.format_potential_template_parameters(workflow_run_context=fake_ctx)
        captured["goal"] = self.data_extraction_goal
        return _extraction_result(
            self.output_parameter,
            [
                {"condition_index": 1, "reasoning": "ok", "result": False},
                {"condition_index": 2, "reasoning": "ok", "result": True},
            ],
        )

    with patch.object(ExtractionBlock, "execute", _execute_after_real_format):
        results, _, _, _ = await block._evaluate_prompt_branches(
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id="wr",
            workflow_run_block_id="wrb",
            organization_id="org",
        )

    assert results == [False, True]
    goal = captured["goal"]
    # The stored expression text survives for the LLM but neutralized, with no live Jinja opener.
    assert "{ { current_value.account_number } }" in goal
    for delimiter in _JINJA_OPENING_DELIMITERS:
        assert delimiter not in goal


def test_prerendered_extraction_goal_skips_second_jinja_render(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_TEMPLATING_STRICTNESS", "strict")
    goal = "Evaluate: {{ current_value.account_number }} == '123'"
    block = ExtractionBlock(label="eval", data_extraction_goal=goal, output_parameter=_output_parameter("out"))
    block.mark_data_extraction_goal_prerendered()

    block.format_potential_template_parameters(workflow_run_context=FakeWorkflowRunContext(values={}))

    assert block.data_extraction_goal == goal


def test_unmarked_extraction_goal_still_renders(monkeypatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_TEMPLATING_STRICTNESS", "strict")
    block = ExtractionBlock(
        label="eval",
        data_extraction_goal="Evaluate: {{ foo }}",
        output_parameter=_output_parameter("out"),
    )

    block.format_potential_template_parameters(workflow_run_context=FakeWorkflowRunContext(values={"foo": "bar"}))
    assert block.data_extraction_goal == "Evaluate: bar"

    unresolvable = ExtractionBlock(
        label="eval",
        data_extraction_goal="Evaluate: {{ current_value.account_number }}",
        output_parameter=_output_parameter("out"),
    )
    with pytest.raises(MissingJinjaVariables):
        unresolvable.format_potential_template_parameters(workflow_run_context=FakeWorkflowRunContext(values={}))


def test_llm_safe_context_snapshot_neutralizes_jinja_delimiters() -> None:
    fake_ctx = FakeWorkflowRunContext(
        values={
            "prior_output": {
                "criteria_expression": "{{ current_value.x }} == 1",
                "nested": ["{% if x %}", {"deep": "{# comment #}"}],
            },
            "extracted_output": {"extracted_information": {"note": "{{ injected }}"}},
            "plain": "no jinja here",
            "count": 7,
        },
    )
    snapshot = BranchEvaluationContext(
        workflow_run_context=fake_ctx, block_label="cond"
    ).build_llm_safe_context_snapshot()

    for embedded_string in _iter_strings(snapshot):
        for delimiter in _ALL_JINJA_DELIMITERS:
            assert delimiter not in embedded_string
    serialized = json.dumps(snapshot, default=str)
    for delimiter in _JINJA_OPENING_DELIMITERS:
        assert delimiter not in serialized
    # Values stay readable for the LLM and non-strings pass through untouched.
    assert "current_value.x" in serialized
    assert snapshot["plain"] == "no jinja here"
    assert snapshot["count"] == 7


def test_neutralize_jinja_delimiters_cannot_reform_delimiters_from_brace_runs() -> None:
    neutralized = _neutralize_jinja_delimiters({"k": ["{{{x}}}", "{{{{y}}}}", "a{%b%}c", "{#c#}"]})
    for embedded_string in _iter_strings(neutralized):
        for delimiter in _ALL_JINJA_DELIMITERS:
            assert delimiter not in embedded_string


def test_neutralize_jinja_delimiters_keeps_keys_that_would_collide() -> None:
    """Neutralization is not injective, so it is applied to values only: rewriting these two
    distinct keys would map both to ``{ {a} }`` and silently drop one of the values."""
    neutralized = _neutralize_jinja_delimiters({"{{a}}": "{{ x }}", "{ {a} }": "{{ y }}"})

    assert len(neutralized) == 2
    assert neutralized["{{a}}"] == "{ { x } }"
    assert neutralized["{ {a} }"] == "{ { y } }"


@pytest.mark.asyncio
async def test_branch_evaluation_failure_with_default_branch_falls_back() -> None:
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(
                criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
            ),
            BranchCondition(is_default=True, next_block_label="fallback_block"),
        ],
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, "LLM exploded")
        )
        mock_extraction_cls.return_value = mock_extraction

        await block.execute(workflow_run_id="wr", workflow_run_block_id="wrb", organization_id="org")

    kwargs = mock_build_result.await_args.kwargs
    assert kwargs["success"] is True
    assert kwargs["failure_reason"] is None
    assert kwargs["status"] == BlockStatus.completed
    assert kwargs["executed_branch_next_block"] == "fallback_block"

    metadata = kwargs["output_parameter_value"]
    assert metadata["branch_taken"] == "fallback_block"
    assert metadata["next_block_label"] == "fallback_block"
    assert "LLM exploded" in metadata["evaluation_error"]
    default_eval = next(entry for entry in metadata["evaluations"] if entry["is_default"])
    assert default_eval["is_matched"] is True
    failed_eval = next(entry for entry in metadata["evaluations"] if not entry["is_default"])
    assert "LLM exploded" in failed_eval["error"]


@pytest.mark.asyncio
async def test_branch_evaluation_failure_without_default_branch_still_fails() -> None:
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(
                criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
            ),
        ],
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, "LLM exploded")
        )
        mock_extraction_cls.return_value = mock_extraction

        await block.execute(workflow_run_id="wr", workflow_run_block_id="wrb", organization_id="org")

    kwargs = mock_build_result.await_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["status"] == BlockStatus.failed
    assert "LLM exploded" in kwargs["failure_reason"]
    metadata = kwargs["output_parameter_value"]
    assert metadata["branch_taken"] is None
    assert metadata["next_block_label"] is None
    assert "evaluation_error" not in metadata


@pytest.mark.asyncio
async def test_no_branch_matched_without_failure_still_takes_default_branch() -> None:
    """Guard the pre-existing no-match path through the restructured fallback: a clean False
    evaluation (no error) must take the default branch without recording an evaluation_error."""
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(
                criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
            ),
            BranchCondition(is_default=True, next_block_label="fallback_block"),
        ],
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_extraction_result(
                block.output_parameter,
                [{"condition_index": 1, "reasoning": "not premium", "result": False}],
            )
        )
        mock_extraction_cls.return_value = mock_extraction

        await block.execute(workflow_run_id="wr", workflow_run_block_id="wrb", organization_id="org")

    kwargs = mock_build_result.await_args.kwargs
    assert kwargs["success"] is True
    assert kwargs["failure_reason"] is None
    metadata = kwargs["output_parameter_value"]
    assert metadata["branch_taken"] == "fallback_block"
    assert "evaluation_error" not in metadata
    default_eval = next(entry for entry in metadata["evaluations"] if entry["is_default"])
    assert default_eval["is_matched"] is True


@pytest.mark.asyncio
async def test_failed_branch_does_not_hide_a_later_matching_branch() -> None:
    """A branch that cannot be evaluated must not short-circuit the loop into the default: the
    remaining branches are still evaluated, and a later match wins over the default."""
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(
                criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
            ),
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ 1 == 1 }}"), next_block_label="jinja_match"),
            BranchCondition(is_default=True, next_block_label="fallback_block"),
        ],
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, "LLM exploded")
        )
        mock_extraction_cls.return_value = mock_extraction

        await block.execute(workflow_run_id="wr", workflow_run_block_id="wrb", organization_id="org")

    kwargs = mock_build_result.await_args.kwargs
    assert kwargs["success"] is True
    assert kwargs["failure_reason"] is None
    assert kwargs["status"] == BlockStatus.completed
    assert kwargs["executed_branch_next_block"] == "jinja_match"

    metadata = kwargs["output_parameter_value"]
    assert metadata["branch_taken"] == "jinja_match"
    assert metadata["branch_index"] == 1
    # The unevaluable branch stays visible even though routing succeeded.
    assert "LLM exploded" in metadata["evaluation_error"]
    assert "LLM exploded" in metadata["evaluations"][0]["error"]
    assert [entry["next_block_label"] for entry in metadata["evaluations"] if entry["is_matched"]] == ["jinja_match"]


@pytest.mark.asyncio
async def test_failed_branch_does_not_reorder_later_matching_branches() -> None:
    """Among branches that do evaluate, author order still decides: an earlier failure must not
    let a lower-priority branch jump ahead of the first branch that actually matched."""
    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(
                criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
            ),
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ 1 == 1 }}"), next_block_label="first_true"),
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ 2 == 2 }}"), next_block_label="second_true"),
            BranchCondition(is_default=True, next_block_label="fallback_block"),
        ],
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        mock_extraction = MagicMock()
        mock_extraction.execute = AsyncMock(
            return_value=_failed_extraction_result(block.output_parameter, "LLM exploded")
        )
        mock_extraction_cls.return_value = mock_extraction

        await block.execute(workflow_run_id="wr", workflow_run_block_id="wrb", organization_id="org")

    kwargs = mock_build_result.await_args.kwargs
    metadata = kwargs["output_parameter_value"]
    assert metadata["branch_taken"] == "first_true"
    assert metadata["branch_index"] == 1
    # The third branch is never reached, so it must not appear as evaluated or matched.
    assert [entry["next_block_label"] for entry in metadata["evaluations"]] == ["premium", "first_true"]


# Engine A/B parity contracts (SKY-15494): the synthetic branch-eval ExtractionBlock follows the
# run's resolved engine, so these pin that rendering/alignment never branch on engine, a failed
# extraction raises rather than retrying on another engine, and jinja-only conditionals build no
# ExtractionBlock at all. Whether the block honors the run override lives in
# tests/unit/test_workflow_block_engine.py, which owns engine resolution/dispatch coverage.


@pytest.fixture
def scoped_context() -> Iterator[SkyvernContext]:
    context = SkyvernContext()
    skyvern_context.set(context)
    try:
        yield context
    finally:
        skyvern_context.reset()


def _pin_engine_override(context: SkyvernContext, workflow_run_id: str, engine: RunEngine | None) -> None:
    context.workflow_block_engine_resolved_run_id = workflow_run_id
    context.workflow_block_engine_override = engine


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_override", [None, RunEngine.skyvern_v3], ids=["control", "v3_override"])
async def test_multi_branch_batch_parity_regardless_of_resolved_engine(
    scoped_context: SkyvernContext, engine_override: RunEngine | None
) -> None:
    """Contract A: for a multi-branch, all-prompt batch, the (results, rendered_expressions) the
    batch function returns must not depend on which engine the synthetic ExtractionBlock
    resolves to."""
    _pin_engine_override(scoped_context, "wr_multi", engine_override)

    branches = [
        BranchCondition(
            criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
        ),
        BranchCondition(
            criteria=PromptBranchCriteria(expression="user is a returning customer"), next_block_label="returning"
        ),
        BranchCondition(
            criteria=PromptBranchCriteria(expression="cart total exceeds $100"), next_block_label="big_cart"
        ),
    ]
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"plan": "premium"})  # type: ignore[method-assign]

    async def _execute(self: ExtractionBlock, *args: object, **kwargs: object) -> BlockResult:
        return _extraction_result(
            self.output_parameter,
            [
                {"condition_index": 1, "reasoning": "ok", "result": True},
                {"condition_index": 2, "reasoning": "ok", "result": False},
                {"condition_index": 3, "reasoning": "ok", "result": True},
            ],
        )

    with patch.object(ExtractionBlock, "execute", _execute):
        results, rendered_expressions, _, _ = await block_module._evaluate_prompt_branch_conditions_batch(
            log_label="cond",
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id="wr_multi",
            workflow_run_block_id="wrb",
            organization_id="org_1",
            browser_session_id=None,
            workflow_id="wf_1",
        )

    assert results == [True, False, True]
    assert rendered_expressions == [
        "user selected premium plan",
        "user is a returning customer",
        "cart total exceeds $100",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_override", [None, RunEngine.skyvern_v3], ids=["control", "v3_override"])
async def test_jinja_prerendered_mix_batch_parity_regardless_of_resolved_engine(
    scoped_context: SkyvernContext, engine_override: RunEngine | None
) -> None:
    """Contract A: a batch mixing a fully Jinja-rendered branch with a pure natural-language
    branch returns the same rendered expressions and results regardless of the resolved engine."""
    _pin_engine_override(scoped_context, "wr_mixed_parity", engine_override)

    branches = [
        BranchCondition(criteria=PromptBranchCriteria(expression='{{tier}} == "gold"'), next_block_label="gold"),
        BranchCondition(
            criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="premium"
        ),
    ]
    evaluation_context = BranchEvaluationContext(
        workflow_run_context=None,
        template_renderer=lambda expr: expr.replace("{{tier}}", "gold"),
    )
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={"plan": "premium"})  # type: ignore[method-assign]

    async def _execute(self: ExtractionBlock, *args: object, **kwargs: object) -> BlockResult:
        return _extraction_result(
            self.output_parameter,
            [
                {"condition_index": 1, "reasoning": "ok", "result": True},
                {"condition_index": 2, "reasoning": "ok", "result": False},
            ],
        )

    with patch.object(ExtractionBlock, "execute", _execute):
        results, rendered_expressions, _, _ = await block_module._evaluate_prompt_branch_conditions_batch(
            log_label="cond",
            branches=branches,
            evaluation_context=evaluation_context,
            workflow_run_id="wr_mixed_parity",
            workflow_run_block_id="wrb",
            organization_id="org_1",
            browser_session_id=None,
            workflow_id="wf_1",
        )

    assert results == [True, False]
    assert rendered_expressions == ['gold == "gold"', "user selected premium plan"]


@pytest.mark.asyncio
async def test_failed_extraction_raises_without_a_different_engine_retry(scoped_context: SkyvernContext) -> None:
    """Contract C: a failing extraction raises ConditionalBranchEvaluationError immediately, with
    no second execute attempt on a different engine. Extraction-level failures already retry at
    the step level inside the task (block.py:14703-14714), so the batch function must not
    silently re-roll on a different engine as a fallback."""
    _pin_engine_override(scoped_context, "wr_no_fallback", RunEngine.skyvern_v3)

    branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="user selected premium plan"), next_block_label="x"
    )
    evaluation_context = BranchEvaluationContext(workflow_run_context=None, template_renderer=lambda expr: expr)
    evaluation_context.build_llm_safe_context_snapshot = MagicMock(return_value={})  # type: ignore[method-assign]

    captured_engines: list[RunEngine] = []

    async def _execute(self: ExtractionBlock, *args: object, **kwargs: object) -> BlockResult:
        captured_engines.append(self.resolve_engine("wr_no_fallback"))
        return _failed_extraction_result(self.output_parameter, "LLM rate limited")

    with (
        patch.object(ExtractionBlock, "execute", _execute),
        pytest.raises(ConditionalBranchEvaluationError, match="LLM rate limited"),
    ):
        await block_module._evaluate_prompt_branch_conditions_batch(
            log_label="cond",
            branches=[branch],
            evaluation_context=evaluation_context,
            workflow_run_id="wr_no_fallback",
            workflow_run_block_id="wrb",
            organization_id="org_1",
            browser_session_id=None,
            workflow_id="wf_1",
        )

    assert len(captured_engines) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_override", [None, RunEngine.skyvern_v3], ids=["no_override", "v3_override"])
async def test_jinja_only_conditional_never_constructs_extraction_block(
    scoped_context: SkyvernContext, engine_override: RunEngine | None
) -> None:
    """Contract D: a conditional whose branches are all JinjaBranchCriteria never builds or
    executes a synthetic ExtractionBlock, whether or not the run is pinned into the v3 A/B."""
    _pin_engine_override(scoped_context, "wr_jinja_only", engine_override)

    block = ConditionalBlock(
        label="cond",
        output_parameter=_output_parameter("out"),
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ 1 == 2 }}"), next_block_label="a"),
            BranchCondition(is_default=True, next_block_label="fallback_block"),
        ],
    )

    with (
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
        patch.object(
            block_module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            new=MagicMock(return_value=None),
        ),
        patch.object(ConditionalBlock, "build_block_result", new_callable=AsyncMock) as mock_build_result,
    ):
        await block.execute(workflow_run_id="wr_jinja_only", workflow_run_block_id="wrb", organization_id="org")

    mock_extraction_cls.assert_not_called()
    assert mock_build_result.await_args.kwargs["executed_branch_next_block"] == "fallback_block"
