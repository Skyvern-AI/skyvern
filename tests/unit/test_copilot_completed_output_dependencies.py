"""Completed outputs retain upstream configuration and input custody."""

from datetime import UTC, datetime

import pytest

from skyvern.forge.sdk.copilot.repair_origin_run import (
    OriginExecutionSettings,
    OriginOutputRefusal,
    bank_completed_outputs,
)
from skyvern.forge.sdk.copilot.tools.frontier import _plan_frontier, selected_output_run_refusal
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    ContextParameter,
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from tests.unit.copilot_test_helpers import make_copilot_ctx

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def _workflow(*, declared: bool = False) -> Workflow:
    count = WorkflowParameter(
        key="count",
        workflow_parameter_id="p_count",
        workflow_parameter_type=WorkflowParameterType.INTEGER,
        workflow_id="workflow",
        default_value=7,
        created_at=NOW,
        modified_at=NOW,
    )
    outputs = [
        OutputParameter(
            key=f"{label}_output",
            output_parameter_id=f"p_{label}",
            workflow_id="workflow",
            created_at=NOW,
            modified_at=NOW,
        )
        for label in ["unrelated", "input", "double", "consumer"]
    ]
    return Workflow(
        workflow_id="workflow",
        workflow_permanent_id="wpid",
        organization_id="org",
        title="Dependency test",
        version=1,
        is_saved_task=False,
        created_at=NOW,
        modified_at=NOW,
        workflow_definition=WorkflowDefinition(
            parameters=[count],
            blocks=[
                CodeBlock(label="unrelated", code="result = 99", output_parameter=outputs[0]),
                CodeBlock(label="input", code="result = count", parameters=[count], output_parameter=outputs[1]),
                CodeBlock(label="double", code="result = {{ input_output }} * 2", output_parameter=outputs[2]),
                CodeBlock(
                    label="consumer",
                    code="raise RuntimeError('repair me')",
                    output_parameter=outputs[3],
                    parameters=[outputs[2]] if declared else [],
                ),
            ],
        ),
    )


def _plan(source: Workflow, *, declared: bool = False):
    ctx = make_copilot_ctx()
    rows = [
        WorkflowRunBlock(
            workflow_run_block_id=f"block_{label}",
            workflow_run_id="run",
            organization_id="org",
            block_type="code",
            label=label,
            status="completed",
            output={"count" if label == "input" else "doubled": value},
            created_at=NOW,
            modified_at=NOW,
        )
        for label, value in [("input", 7), ("double", 14)]
    ]
    ctx.repair_origin_outputs = bank_completed_outputs(
        None,
        workflow_run_id="run",
        created_at=NOW,
        definition=source.workflow_definition,
        run_blocks=rows,
        output_parameter_rows=[],
        seeded_only_labels=frozenset(),
        input_values={"count": 7},
        settings=OriginExecutionSettings.of(source),
    )
    candidate = source.model_copy(deep=True)
    candidate.workflow_definition.blocks[-1].code = (
        "result = double_output" if declared else "result = {{ double_output }}"
    )
    plan = _plan_frontier(
        ctx, ["input", "double", "consumer"], source.workflow_definition, candidate.workflow_definition
    )
    return ctx, candidate, plan


@pytest.mark.parametrize("declared", [False, True])
def test_changed_transitive_input_refuses_stale_completed_value(declared: bool) -> None:
    ctx, candidate, plan = _plan(_workflow(declared=declared), declared=declared)
    assert plan[0] == ["consumer"] and plan[1] == {"double": {"doubled": 14}}
    refusal = selected_output_run_refusal(
        ctx,
        ctx.frontier_selected_output_sources,
        candidate,
        {"count": 8},
        "consumer",
        execution_settings=OriginExecutionSettings.of(candidate),
    )
    assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_INPUT
    assert refusal.block_label == "double" and refusal.parameter_key == "count"


def test_suffix_only_repair_keeps_upstream_outputs() -> None:
    ctx, candidate, plan = _plan(_workflow())
    assert plan[1] == {"double": {"doubled": 14}}
    assert (
        selected_output_run_refusal(
            ctx,
            ctx.frontier_selected_output_sources,
            candidate,
            {"count": 7},
            "consumer",
            execution_settings=OriginExecutionSettings.of(candidate),
        )
        is None
    )


@pytest.mark.parametrize("change", ["code", "order", "insert"])
def test_upstream_browser_predecessor_changes_refuse_cached_outputs(change: str) -> None:
    ctx, candidate, _ = _plan(_workflow())
    blocks = candidate.workflow_definition.blocks
    if change == "code":
        blocks[0].code = "await page.goto('https://example.com/other-account')"
    elif change == "order":
        blocks[0], blocks[1] = blocks[1], blocks[0]
    else:
        blocks.insert(
            0,
            CodeBlock(
                label="new_navigation",
                code="await page.goto('https://example.com')",
                output_parameter=blocks[0].output_parameter.model_copy(
                    update={"key": "new_navigation_output", "output_parameter_id": "p_new_navigation"}
                ),
            ),
        )
    refusal = selected_output_run_refusal(
        ctx,
        ctx.frontier_selected_output_sources,
        candidate,
        {"count": 7},
        "consumer",
        execution_settings=OriginExecutionSettings.of(candidate),
    )
    assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_PRODUCER
    assert refusal.block_label == "double"
    labels, seed, _, _ = _plan_frontier(
        ctx, ["unrelated", "input", "double", "consumer"], None, candidate.workflow_definition
    )
    assert labels == ["unrelated", "input", "double", "consumer"] and seed == {}


@pytest.mark.parametrize("missing_custody", [False, True])
def test_implicit_browser_predecessor_inputs_require_matching_recorded_values(missing_custody: bool) -> None:
    source = _workflow()
    # Only the earlier navigation block consumes this input; the cached producer
    # has no declared output dependency on that browser operation.
    source.workflow_definition.blocks[0].parameters = source.workflow_definition.parameters[:]
    source.workflow_definition.blocks[0].code = "await page.goto(str(count))"
    source.workflow_definition.blocks[1].parameters = []
    source.workflow_definition.blocks[1].code = "result = 7"
    ctx, candidate, _ = _plan(source)
    if missing_custody:
        # A matching default is not evidence of the inputs used by the source run.
        ctx.frontier_selected_output_sources["double"].snapshot.input_values.clear()
    refusal = selected_output_run_refusal(
        ctx,
        ctx.frontier_selected_output_sources,
        candidate,
        {"count": 7 if missing_custody else 8},
        "consumer",
        execution_settings=OriginExecutionSettings.of(candidate),
    )
    assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_INPUT
    assert refusal.parameter_key == "count"


@pytest.mark.parametrize("templated", [False, True])
def test_context_parameter_source_preserves_transitive_input_custody(templated: bool) -> None:
    source = _workflow()
    alias = ContextParameter(key="input_alias", source=source.workflow_definition.blocks[1].output_parameter)
    source.workflow_definition.parameters.append(alias)
    middle = source.workflow_definition.blocks[2]
    middle.parameters = [] if templated else [alias]
    middle.code = "result = {{ input_alias }} * 2" if templated else "result = input_alias * 2"
    ctx, candidate, plan = _plan(source)
    assert plan[1] == {"double": {"doubled": 14}}
    refusal = selected_output_run_refusal(
        ctx,
        ctx.frontier_selected_output_sources,
        candidate,
        {"count": 8},
        "consumer",
        execution_settings=OriginExecutionSettings.of(candidate),
    )
    assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_INPUT
    assert refusal.parameter_key == "count"


def test_a_bank_does_not_change_frontier_policy_when_no_output_is_reused() -> None:
    source = _workflow()
    source.workflow_definition.blocks[2].code = "result = 2"
    ctx, candidate, _ = _plan(source)
    candidate.workflow_definition.blocks[2].code = "result = 4"
    labels, seed, _, _ = _plan_frontier(
        ctx, ["input", "double", "consumer"], source.workflow_definition, candidate.workflow_definition
    )
    assert labels == ["input", "double", "consumer"]
    assert seed == {} and not ctx.frontier_selected_output_sources


def _loop_workflow(*, nested_dependency: bool = False) -> Workflow:
    from skyvern.forge.sdk.workflow.models.block import ForLoopBlock

    source = _workflow()
    outputs = {
        label: source.workflow_definition.blocks[0].output_parameter.model_copy(
            update={"key": f"{label}_output", "output_parameter_id": f"p_{label}"}
        )
        for label in ["rows", "iterate", "child", "tail"]
    }
    source.workflow_definition.blocks = [
        CodeBlock(label="rows", code="result = [1, 2]", output_parameter=outputs["rows"]),
        ForLoopBlock(
            label="iterate",
            loop_over=outputs["rows"],
            output_parameter=outputs["iterate"],
            loop_blocks=[CodeBlock(label="child", code="result = 1", output_parameter=outputs["child"])],
        ),
        CodeBlock(
            label="tail",
            code="result = {{ child_output }}" if nested_dependency else "result = 1",
            output_parameter=outputs["tail"],
        ),
    ]
    return source


def _bank_loop_rows(source: Workflow):
    ctx = make_copilot_ctx()
    row = WorkflowRunBlock(
        workflow_run_block_id="rows_block",
        workflow_run_id="run",
        organization_id="org",
        block_type="code",
        label="rows",
        status="completed",
        output=[1, 2],
        created_at=NOW,
        modified_at=NOW,
    )
    ctx.repair_origin_outputs = bank_completed_outputs(
        None,
        workflow_run_id="run",
        created_at=NOW,
        definition=source.workflow_definition,
        run_blocks=[row],
        output_parameter_rows=[],
        seeded_only_labels=frozenset(),
        settings=OriginExecutionSettings.of(source),
    )
    return ctx


@pytest.mark.parametrize("context_alias", [False, True])
def test_loop_over_output_is_seeded_when_only_the_loop_is_repaired(context_alias: bool) -> None:
    source = _loop_workflow()
    if context_alias:
        alias = ContextParameter(key="rows_alias", source=source.workflow_definition.blocks[0].output_parameter)
        source.workflow_definition.parameters.append(alias)
        source.workflow_definition.blocks[1].loop_over = alias
    ctx = _bank_loop_rows(source)
    candidate = source.model_copy(deep=True)
    candidate.workflow_definition.blocks[1].loop_blocks[0].code = "result = 2"
    labels, seed, start, _ = _plan_frontier(
        ctx, ["rows", "iterate"], source.workflow_definition, candidate.workflow_definition
    )
    assert labels == ["iterate"] and start == "iterate", ctx.frontier_origin_output_refusal
    assert seed == {"rows": [1, 2]}
    assert ctx.frontier_selected_output_sources["rows"].workflow_run_id == "run"


@pytest.mark.parametrize("verified_resume", [False, True])
def test_missing_nested_producer_receipt_restores_the_complete_requested_prefix(verified_resume: bool) -> None:
    source = _loop_workflow(nested_dependency=True)
    ctx = _bank_loop_rows(source)
    candidate = source.model_copy(deep=True)
    candidate.workflow_definition.blocks[-1].code += " + 1"
    if verified_resume:
        ctx.browser_session_id = "pbs_verified"
        ctx.verified_prefix_labels = ["rows", "iterate"]
        ctx.verified_prefix_current_url = "https://example.com/loop-result"
        ctx.verified_prefix_terminal_label = "iterate"
        ctx.verified_prefix_block_end_urls = {"iterate": "https://example.com/loop-result"}
        ctx.verified_prefix_block_end_session_id = ctx.browser_session_id
    requested = ["rows", "iterate", "tail"]
    labels, seed, start, _ = _plan_frontier(
        ctx,
        requested,
        source.workflow_definition,
        candidate.workflow_definition,
        runtime_page_url="https://example.com/loop-result" if verified_resume else None,
    )
    assert labels == requested and start == "rows" and seed == {}
    assert ctx.frontier_selected_output_sources == {}
    assert ctx.frontier_origin_output_refusal is not None
    assert ctx.frontier_origin_output_refusal.block_label == "child"


@pytest.mark.parametrize("change", ["nested_config", "nested_input"])
def test_completed_value_with_unprovable_nested_dependency_is_not_reused(change: str) -> None:
    from skyvern.forge.sdk.copilot.repair_origin_run import SelectedOutputSource

    source = _loop_workflow(nested_dependency=True)
    count = source.workflow_definition.parameters[0]
    child = source.workflow_definition.blocks[1].loop_blocks[0]
    child.parameters = [count]
    child.code = "result = count"
    ctx = _bank_loop_rows(source)
    tail_row = WorkflowRunBlock(
        workflow_run_block_id="tail_block",
        workflow_run_id="run",
        organization_id="org",
        block_type="code",
        label="tail",
        status="completed",
        output={"value": 7},
        created_at=NOW,
        modified_at=NOW,
    )
    ctx.repair_origin_outputs = bank_completed_outputs(
        ctx.repair_origin_outputs,
        workflow_run_id="run",
        created_at=NOW,
        definition=source.workflow_definition,
        run_blocks=[tail_row],
        output_parameter_rows=[],
        seeded_only_labels=frozenset(),
        input_values={"count": 7},
        settings=OriginExecutionSettings.of(source),
    )
    receipt = SelectedOutputSource("tail", "run", "banked", ctx.repair_origin_outputs.sources["run"].snapshot)
    candidate = source.model_copy(deep=True)
    if change == "nested_config":
        candidate.workflow_definition.blocks[1].loop_blocks[0].code = "result = count + 1"
    resolved = {"count": 8 if change == "nested_input" else 7}
    refusal = selected_output_run_refusal(
        ctx,
        {"tail": receipt},
        candidate,
        resolved,
        "tail",
        execution_settings=OriginExecutionSettings.of(candidate),
    )
    assert refusal is not None and refusal.reason == OriginOutputRefusal.CHANGED_PRODUCER
    assert refusal.block_label == "tail" and refusal.origin_workflow_run_id == "run"


@pytest.mark.parametrize("verified", [False, True])
def test_later_completed_producer_cannot_seed_an_earlier_scoped_consumer(verified: bool) -> None:
    from skyvern.forge.sdk.copilot.repair_origin_run import SelectedOutputSource
    from skyvern.forge.sdk.copilot.tools.frontier import resolve_suffix_outputs

    source = _workflow()
    ctx, _, _ = _plan(source)
    source.workflow_definition.blocks[0].code = "result = {{ double_output }}"
    # Keep the source definition identical to the candidate to isolate ordering.
    snapshot = ctx.repair_origin_outputs.sources["run"].snapshot
    snapshot.definition.blocks[0].code = source.workflow_definition.blocks[0].code
    if verified:
        ctx.verified_block_outputs = {"double": {"doubled": 14}}
        ctx.repair_origin_outputs.verified_sources["double"] = SelectedOutputSource(
            "double", "run", "verified", snapshot
        )
    labels, seed, _ = resolve_suffix_outputs(ctx, ["unrelated"], source.workflow_definition, ["unrelated"])
    assert labels == ["unrelated"] and seed == {}
    assert ctx.frontier_origin_output_refusal.reason == OriginOutputRefusal.ORDER_UNPROVABLE


@pytest.mark.parametrize("context_alias", [False, True])
def test_loop_child_declared_output_dependency_is_seeded(context_alias: bool) -> None:
    source = _loop_workflow()
    loop = source.workflow_definition.blocks[1]
    rows = source.workflow_definition.blocks[0].output_parameter
    loop.loop_over = source.workflow_definition.parameters[0]
    parameter = ContextParameter(key="rows_alias", source=rows) if context_alias else rows
    if context_alias:
        source.workflow_definition.parameters.append(parameter)
    loop.loop_blocks[0].parameters = [parameter]
    ctx = _bank_loop_rows(source)
    candidate = source.model_copy(deep=True)
    candidate.workflow_definition.blocks[1].loop_blocks[0].code = "result = 2"
    labels, seed, start, _ = _plan_frontier(
        ctx, ["rows", "iterate"], source.workflow_definition, candidate.workflow_definition
    )
    assert labels == ["iterate"] and start == "iterate", ctx.frontier_origin_output_refusal
    assert seed == {"rows": [1, 2]}
