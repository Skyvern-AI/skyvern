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
from skyvern.schemas.workflows import BlockStatus, BlockType


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
        self.browser_session_id: str | None = None

    def update_block_metadata(self, label: str, metadata: dict) -> None:
        self.blocks_metadata[label] = metadata

    def get_block_metadata(self, label: str | None) -> dict:
        if label is None:
            return {}
        return self.blocks_metadata.get(label, {})

    def mask_secrets_in_data(self, data: object, mask: str = "*****") -> object:
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


def test_build_workflow_graph_stores_conditional_own_next_block_label() -> None:
    """Verify default_next_map stores the conditional block's own next_block_label.

    SKY-8571: This is the prerequisite for the DAG fallback — when a matched
    branch has next_block_label=None, the engine must be able to look up the
    conditional block's own next_block_label in default_next_map.
    """
    service = WorkflowService()

    conditional = _conditional_block(
        "conditional",
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ flag }}"), next_block_label="branch_target"),
            BranchCondition(criteria=None, next_block_label=None, is_default=True),
        ],
        next_block_label="after_conditional",
    )
    branch_target = _navigation_block("branch_target", next_block_label="after_conditional")
    after = _navigation_block("after_conditional")

    _, _, default_next_map = service._build_workflow_graph([conditional, branch_target, after])

    assert default_next_map["conditional"] == "after_conditional"


def test_build_workflow_graph_patches_terminal_blocks_in_conditional_branches() -> None:
    """SKY-8571: Terminal blocks at the end of a conditional branch chain
    should be patched to point to the conditional's successor (merge-point).

    This models the real customer bug: a conditional routes to a chain of blocks
    (branch_start → middle → terminal), and the terminal block has no
    next_block_label set.  The conditional's own next_block_label IS the
    merge-point, so the terminal should be connected to it automatically.
    """
    service = WorkflowService()

    conditional = _conditional_block(
        "cond",
        branch_conditions=[
            BranchCondition(
                criteria=JinjaBranchCriteria(expression="{{ flag }}"),
                next_block_label="branch_start",
            ),
            # Default branch goes directly to merge point (no inner blocks)
            BranchCondition(criteria=None, next_block_label="merge_point", is_default=True),
        ],
        next_block_label="merge_point",
    )
    # Branch chain: branch_start → middle → terminal (no next_block_label!)
    branch_start = _navigation_block("branch_start", next_block_label="middle")
    middle = _extraction_block("middle", next_block_label="terminal")
    terminal = _navigation_block("terminal")  # No next_block_label
    merge_point = _navigation_block("merge_point")

    _, _, default_next_map = service._build_workflow_graph([conditional, branch_start, middle, terminal, merge_point])

    # The terminal block should be patched to point to the merge point
    assert default_next_map["terminal"] == "merge_point"
    # Existing connections should be preserved
    assert default_next_map["branch_start"] == "middle"
    assert default_next_map["middle"] == "terminal"
    assert default_next_map["merge_point"] is None


def test_build_workflow_graph_patches_nested_conditional_no_merge_point() -> None:
    """SKY-8571: This models the actual customer bug.

    outer_cond (next=outer_merge) has a branch leading to:
      block_29 → inner_cond (next=null)
    inner_cond has NO merge point.  Its branches lead to:
      branch 0 → loop → wait → validate (terminal)
      default  → other_loop (terminal)

    After iterative patching:
      Pass 1: outer_cond patches inner_cond.next → outer_merge
      Pass 2: inner_cond (now has successor) patches validate.next → outer_merge
              AND other_loop.next → outer_merge
    """
    service = WorkflowService()

    outer_cond = _conditional_block(
        "outer_cond",
        branch_conditions=[
            BranchCondition(
                criteria=JinjaBranchCriteria(expression="{{ flag }}"),
                next_block_label="block_29",
            ),
            BranchCondition(criteria=None, next_block_label="outer_merge", is_default=True),
        ],
        next_block_label="outer_merge",
    )
    block_29 = _navigation_block("block_29", next_block_label="inner_cond")
    inner_cond = _conditional_block(
        "inner_cond",
        branch_conditions=[
            BranchCondition(
                criteria=JinjaBranchCriteria(expression="{{ x }}"),
                next_block_label="loop_block",
            ),
            BranchCondition(criteria=None, next_block_label="other_loop", is_default=True),
        ],
        next_block_label=None,  # No merge point!
    )
    loop_block = _navigation_block("loop_block", next_block_label="wait_block")
    wait_block = _extraction_block("wait_block", next_block_label="validate")
    validate = _navigation_block("validate")  # Terminal, next=None
    other_loop = _navigation_block("other_loop")  # Terminal, next=None
    outer_merge = _navigation_block("outer_merge")

    _, _, default_next_map = service._build_workflow_graph(
        [outer_cond, block_29, inner_cond, loop_block, wait_block, validate, other_loop, outer_merge]
    )

    # Pass 1: outer_cond patches inner_cond (terminal in its branch chain)
    assert default_next_map["inner_cond"] == "outer_merge"
    # Pass 2: inner_cond now has successor, patches its own branch terminals
    assert default_next_map["validate"] == "outer_merge"
    assert default_next_map["other_loop"] == "outer_merge"
    # outer_merge itself stays terminal
    assert default_next_map["outer_merge"] is None


@pytest.mark.asyncio
async def test_dag_conditional_fallback_to_own_next_block_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-8571: When a conditional block's matched branch has next_block_label=None,
    the DAG should fall back to the conditional block's own next_block_label
    instead of treating it as a terminal node and stopping execution.

    Scenario: conditional has two branches — one that points to an inner block
    and a default with no target. The conditional's own next_block_label serves
    as the merge/continuation point. At runtime the default branch matches,
    returning next_block_label=None. The engine should fall back.
    """
    from unittest.mock import MagicMock

    from skyvern.forge.sdk.workflow.models.block import WaitBlock
    from skyvern.schemas.workflows import BlockResult

    service = WorkflowService()

    # Workflow structure:
    #   conditional ──branch_a──► inner_block ──► wait_block
    #               └─default(None)              ▲
    #               └─(own next_block_label)─────┘
    # The default branch has next_block_label=None, but the conditional's
    # own next_block_label points to wait_block as the merge point.
    conditional = _conditional_block(
        "conditional",
        branch_conditions=[
            BranchCondition(criteria=JinjaBranchCriteria(expression="{{ flag }}"), next_block_label="inner_block"),
            BranchCondition(criteria=None, next_block_label=None, is_default=True),
        ],
        next_block_label="wait_block",
    )
    inner = _navigation_block("inner_block", next_block_label="wait_block")
    wait = WaitBlock(
        label="wait_block",
        output_parameter=_output_parameter("wait_output"),
        wait_sec=1,
    )

    workflow = MagicMock()
    workflow.workflow_definition = MagicMock()
    workflow.workflow_definition.blocks = [conditional, inner, wait]
    workflow.workflow_definition.finally_block_label = None
    workflow.workflow_permanent_id = "wpid_test"
    workflow.workflow_id = "wf_test"
    workflow.generate_script_on_terminal = False

    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_test"

    organization = MagicMock()
    organization.organization_id = "org_test"

    # Track which blocks were executed
    executed_blocks: list[str] = []

    async def mock_execute_single_block(
        *,
        workflow,
        block,
        block_idx,
        blocks_cnt,
        workflow_run,
        organization,
        workflow_run_id,
        browser_session_id,
        script_blocks_by_label,
        loaded_script_module,
        is_script_run,
        blocks_to_update,
        parent_workflow_run_block_id=None,
    ):
        executed_blocks.append(block.label)
        branch_metadata = None
        if block.block_type == BlockType.CONDITIONAL:
            # Simulate: default branch matched → next_block_label is None
            branch_metadata = {
                "branch_taken": None,
                "branch_index": 1,
                "next_block_label": None,
            }
        block_result = BlockResult(
            success=True,
            output_parameter=block.output_parameter,
            output_parameter_value=branch_metadata,
            status=BlockStatus.completed,
            workflow_run_block_id=f"wrb_{block.label}",
        )
        return workflow_run, blocks_to_update, block_result, False, branch_metadata

    monkeypatch.setattr(service, "_execute_single_block", mock_execute_single_block)

    result_run, _ = await service._execute_workflow_blocks_dag(
        workflow=workflow,
        workflow_run=workflow_run,
        organization=organization,
        browser_session_id=None,
        script_blocks_by_label={},
        loaded_script_module=None,
        is_script_run=False,
        blocks_to_update=set(),
    )

    # The critical assertion: both blocks should have been executed.
    # Before the fix, only "conditional" would be in the list because
    # the DAG treated next_block_label=None as a terminal node.
    assert executed_blocks == ["conditional", "wait_block"]


@pytest.mark.asyncio
async def test_dag_execution_continues_after_nested_conditional_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-8571: Models the actual customer bug — nested conditionals where the
    inner conditional has no merge point.

    Structure:
      outer_cond (next=merge) → branch → nav → inner_cond (next=null)
      inner_cond → branch → loop_block → validate (next=null)
      merge (wait block)

    Execution: outer_cond → nav → inner_cond → loop_block → validate → merge
    Before the fix, execution stalls at validate because both validate and
    inner_cond have next_block_label=null.
    """
    from unittest.mock import MagicMock

    from skyvern.forge.sdk.workflow.models.block import WaitBlock
    from skyvern.schemas.workflows import BlockResult

    service = WorkflowService()

    outer_cond = _conditional_block(
        "outer_cond",
        branch_conditions=[
            BranchCondition(
                criteria=JinjaBranchCriteria(expression="{{ flag }}"),
                next_block_label="nav",
            ),
            BranchCondition(criteria=None, next_block_label="merge", is_default=True),
        ],
        next_block_label="merge",
    )
    nav = _navigation_block("nav", next_block_label="inner_cond")
    inner_cond = _conditional_block(
        "inner_cond",
        branch_conditions=[
            BranchCondition(
                criteria=JinjaBranchCriteria(expression="{{ x }}"),
                next_block_label="loop_block",
            ),
            BranchCondition(criteria=None, next_block_label="other_path", is_default=True),
        ],
        next_block_label=None,  # No merge point!
    )
    loop_block = _navigation_block("loop_block", next_block_label="validate")
    validate = _extraction_block("validate")  # Terminal — next=None
    other_path = _navigation_block("other_path")  # Terminal — next=None
    merge = WaitBlock(
        label="merge",
        output_parameter=_output_parameter("merge_output"),
        wait_sec=1,
    )

    workflow = MagicMock()
    workflow.workflow_definition = MagicMock()
    workflow.workflow_definition.blocks = [outer_cond, nav, inner_cond, loop_block, validate, other_path, merge]
    workflow.workflow_definition.finally_block_label = None
    workflow.workflow_permanent_id = "wpid_test"
    workflow.workflow_id = "wf_test"
    workflow.generate_script_on_terminal = False

    workflow_run = MagicMock()
    workflow_run.workflow_run_id = "wr_test"

    organization = MagicMock()
    organization.organization_id = "org_test"

    executed_blocks: list[str] = []

    # Map conditional labels to the branch_metadata they should return
    branch_responses = {
        "outer_cond": {"branch_taken": "nav", "branch_index": 0, "next_block_label": "nav"},
        "inner_cond": {"branch_taken": "loop_block", "branch_index": 0, "next_block_label": "loop_block"},
    }

    async def mock_execute_single_block(
        *,
        workflow,
        block,
        block_idx,
        blocks_cnt,
        workflow_run,
        organization,
        workflow_run_id,
        browser_session_id,
        script_blocks_by_label,
        loaded_script_module,
        is_script_run,
        blocks_to_update,
        parent_workflow_run_block_id=None,
    ):
        executed_blocks.append(block.label)
        branch_metadata = branch_responses.get(block.label)
        block_result = BlockResult(
            success=True,
            output_parameter=block.output_parameter,
            output_parameter_value=branch_metadata,
            status=BlockStatus.completed,
            workflow_run_block_id=f"wrb_{block.label}",
        )
        return workflow_run, blocks_to_update, block_result, False, branch_metadata

    monkeypatch.setattr(service, "_execute_single_block", mock_execute_single_block)

    result_run, _ = await service._execute_workflow_blocks_dag(
        workflow=workflow,
        workflow_run=workflow_run,
        organization=organization,
        browser_session_id=None,
        script_blocks_by_label={},
        loaded_script_module=None,
        is_script_run=False,
        blocks_to_update=set(),
    )

    # All blocks should execute through to merge.
    # Before the fix, execution stalled at validate (next=null).
    assert executed_blocks == ["outer_cond", "nav", "inner_cond", "loop_block", "validate", "merge"]
