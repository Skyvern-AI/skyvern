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
    mock_llm_handler = AsyncMock()
    mock_llm_handler.return_value = {
        "evaluations": [{"rendered_condition": 'Joint == "Joint"', "reasoning": "ok", "result": True}]
    }

    with (
        patch.dict(block_module.app.__dict__, {"LLM_API_HANDLER": mock_llm_handler}),
        patch("skyvern.forge.sdk.workflow.models.block.prompt_engine.load_prompt", return_value="goal") as mock_prompt,
        patch("skyvern.forge.sdk.workflow.models.block.ExtractionBlock") as mock_extraction_cls,
    ):
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
    assert llm_response == {
        "evaluations": [{"rendered_condition": 'Joint == "Joint"', "reasoning": "ok", "result": True}]
    }
    mock_llm_handler.assert_awaited_once_with(
        prompt="goal",
        prompt_name="conditional-prompt-branch-evaluation",
        force_dict=True,
    )
    mock_extraction_cls.assert_not_called()
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
