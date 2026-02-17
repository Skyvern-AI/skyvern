from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.workflow.exceptions import InvalidWorkflowDefinition
from skyvern.forge.sdk.workflow.models.block import (
    BranchCondition,
    ConditionalBlock,
    ExtractionBlock,
    JinjaBranchCriteria,
    NavigationBlock,
    PromptBranchCriteria,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import BlockStatus


def _output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"{key}_id",
        key=key,
        workflow_id="wf",
        created_at=now,
        modified_at=now,
    )


def _navigation_block(label: str, next_block_label: str | None = None) -> NavigationBlock:
    return NavigationBlock(
        url="https://example.com",
        label=label,
        title=label,
        navigation_goal="goal",
        output_parameter=_output_parameter(f"{label}_output"),
        next_block_label=next_block_label,
    )


def _extraction_block(label: str, next_block_label: str | None = None) -> ExtractionBlock:
    return ExtractionBlock(
        url="https://example.com",
        label=label,
        title=label,
        data_extraction_goal="extract data",
        output_parameter=_output_parameter(f"{label}_output"),
        next_block_label=next_block_label,
    )


def _conditional_block(
    label: str, branch_conditions: list[BranchCondition], next_block_label: str | None = None
) -> ConditionalBlock:
    return ConditionalBlock(
        label=label,
        output_parameter=_output_parameter(f"{label}_output"),
        branch_conditions=branch_conditions,
        next_block_label=next_block_label,
    )


class DummyContext:
    def __init__(self, workflow_run_id: str) -> None:
        self.blocks_metadata: dict[str, dict] = {}
        self.values: dict[str, object] = {}
        self.secrets: dict[str, object] = {}
        self.parameters: dict[str, object] = {}
        self.workflow_run_outputs: dict[str, object] = {}
        self.include_secrets_in_templates = False
        self.workflow_title = "test"
        self.workflow_id = "wf"
        self.workflow_permanent_id = "wf-perm"
        self.workflow_run_id = workflow_run_id

    def update_block_metadata(self, label: str, metadata: dict) -> None:
        self.blocks_metadata[label] = metadata

    def get_block_metadata(self, label: str | None) -> dict:
        if label is None:
            return {}
        return self.blocks_metadata.get(label, {})

    def mask_secrets_in_data(self, data: object) -> object:
        """Mock method - returns data as-is since no secrets in tests."""
        return data

    async def register_output_parameter_value_post_execution(self, parameter: OutputParameter, value: object) -> None:  # noqa: ARG002
        return None

    def build_workflow_run_summary(self) -> dict:
        return {}


def test_build_workflow_graph_infers_default_edges() -> None:
    service = WorkflowService()
    first = _navigation_block("first")
    second = _navigation_block("second")

    start_label, label_to_block, default_next_map = service._build_workflow_graph([first, second])

    assert start_label == "first"
    assert set(label_to_block.keys()) == {"first", "second"}
    assert default_next_map["first"] == "second"
    assert default_next_map["second"] is None


def test_build_workflow_graph_rejects_cycles() -> None:
    service = WorkflowService()
    first = _navigation_block("first", next_block_label="second")
    second = _navigation_block("second", next_block_label="first")

    with pytest.raises(InvalidWorkflowDefinition):
        service._build_workflow_graph([first, second])


def test_build_workflow_graph_requires_single_root() -> None:
    service = WorkflowService()
    first = _navigation_block("first")
    second = _navigation_block("second")

    with pytest.raises(InvalidWorkflowDefinition):
        service._build_workflow_graph([first, second, _navigation_block("third", next_block_label="second")])


def test_build_workflow_graph_conditional_blocks_no_sequential_defaulting() -> None:
    """
    Test that workflows with conditional blocks do not apply sequential defaulting.

    This prevents cycles when blocks are ordered differently than execution order.
    For example, if a terminal block appears before branch targets in the blocks array,
    sequential defaulting would incorrectly create a cycle.
    """
    service = WorkflowService()

    # Simulate a workflow where execution order differs from block array order
    # Execution: start -> extract -> conditional -> (branch_a OR branch_b) -> terminal
    # Array order: [start, extract, conditional, terminal, branch_a, branch_b]
    start = _navigation_block("start", next_block_label="extract")
    extract = _extraction_block("extract", next_block_label="conditional")
    conditional = _conditional_block(
        "conditional",
        branch_conditions=[
            BranchCondition(
                criteria=JinjaBranchCriteria(expression="{{ true }}"), next_block_label="branch_a", is_default=False
            ),
            BranchCondition(criteria=None, next_block_label="branch_b", is_default=True),
        ],
        next_block_label="terminal",  # This should be ignored for conditional blocks
    )
    terminal = _extraction_block("terminal", next_block_label=None)  # Terminal block with explicit None
    branch_a = _navigation_block("branch_a", next_block_label="terminal")
    branch_b = _navigation_block("branch_b", next_block_label="terminal")

    # Block array has terminal before branch_a and branch_b
    blocks = [start, extract, conditional, terminal, branch_a, branch_b]

    # This should succeed without creating a cycle
    start_label, label_to_block, default_next_map = service._build_workflow_graph(blocks)

    assert start_label == "start"
    assert set(label_to_block.keys()) == {"start", "extract", "conditional", "terminal", "branch_a", "branch_b"}

    # Verify that sequential defaulting was NOT applied
    # terminal should remain None, not be defaulted to branch_a
    assert default_next_map["terminal"] is None
    assert default_next_map["branch_a"] == "terminal"
    assert default_next_map["branch_b"] == "terminal"


@pytest.mark.asyncio
async def test_evaluate_conditional_block_records_branch_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    output_param = _output_parameter("conditional_output")
    block = ConditionalBlock(
        label="cond",
        output_parameter=output_param,
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ flag }}"), next_block_label="next"),
            BranchCondition(is_default=True, next_block_label=None),
        ],
    )

    ctx = DummyContext(workflow_run_id="run-1")
    ctx.values["flag"] = True
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda workflow_run_id: ctx)

    app.DATABASE.update_workflow_run_block.reset_mock()
    app.DATABASE.create_or_update_workflow_run_output_parameter.reset_mock()

    result = await block.execute(
        workflow_run_id="run-1",
        workflow_run_block_id="wrb-1",
        organization_id="org-1",
    )

    metadata = result.output_parameter_value
    assert metadata["branch_taken"] == "next"
    assert metadata["next_block_label"] == "next"
    assert result.status == BlockStatus.completed
    assert ctx.blocks_metadata["cond"]["branch_taken"] == "next"

    # Get the actual call arguments
    call_args = app.DATABASE.update_workflow_run_block.call_args
    assert call_args.kwargs["workflow_run_block_id"] == "wrb-1"
    assert call_args.kwargs["output"] == metadata
    assert call_args.kwargs["status"] == BlockStatus.completed
    assert call_args.kwargs["failure_reason"] is None
    assert call_args.kwargs["organization_id"] == "org-1"

    # Verify the new execution tracking fields are present
    assert call_args.kwargs["executed_branch_expression"] == "{{ flag }}"
    assert call_args.kwargs["executed_branch_result"] is True
    assert call_args.kwargs["executed_branch_next_block"] == "next"
    # executed_branch_id should be a UUID string
    assert isinstance(call_args.kwargs["executed_branch_id"], str)


@pytest.mark.asyncio
async def test_prompt_branch_uses_batched_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    output_param = _output_parameter("conditional_output_prompt")
    prompt_branch = BranchCondition(
        criteria=PromptBranchCriteria(expression="Check if urgent"), next_block_label="next"
    )
    default_branch = BranchCondition(is_default=True, next_block_label=None)
    block = ConditionalBlock(
        label="cond_prompt",
        output_parameter=output_param,
        branch_conditions=[prompt_branch, default_branch],
    )

    ctx = DummyContext(workflow_run_id="run-2")
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "get_workflow_run_context", lambda workflow_run_id: ctx)
    # Return tuple: (results, rendered_expressions, extraction_goal, llm_response)
    prompt_eval_mock = AsyncMock(return_value=([True], ["Check if urgent"], "test prompt", None))
    monkeypatch.setattr(ConditionalBlock, "_evaluate_prompt_branches", prompt_eval_mock)

    result = await block.execute(
        workflow_run_id="run-2",
        workflow_run_block_id="wrb-2",
        organization_id="org-2",
    )

    assert result.status == BlockStatus.completed
    metadata = result.output_parameter_value
    assert metadata["branch_taken"] == "next"
    assert metadata["criteria_type"] == "prompt"
    prompt_eval_mock.assert_awaited_once()
