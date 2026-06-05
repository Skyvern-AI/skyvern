"""Tests for prompt-based conditional branch evaluation behavior."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.exceptions import ConditionalBranchEvaluationError
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    BranchEvaluationContext,
    ConditionalBlock,
    PromptBranchCriteria,
    _build_branch_evaluation_schema,
    _coerce_condition_index,
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
