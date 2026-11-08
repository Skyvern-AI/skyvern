from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.failure_classifier import FailureCategory, classify_from_failure_reason
from skyvern.forge.sdk.services.powerbi_query_service import PowerBIQueryError
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType, WorkflowParameter
from skyvern.forge.sdk.workflow.models.powerbi_query_block import (
    HUMAN_RUN_REQUIRED_REASON,
    INTEGRATION_UNAVAILABLE_REASON,
    INTERNAL_ERROR_REASON,
    NO_ACCESS_REASON,
    PowerBIQueryBlock,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import BlockResult, BlockStatus, BlockType, PowerBIQueryBlockYAML, WorkflowDefinitionYAML
from skyvern.utils.templating import get_jinja_template_variable_roots
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext

_NOW = datetime.now(UTC)
_DATASET = "cfafbeb1-8037-4d0c-896e-a46fb27ff229"
_ROWS = [{"[Total]": 12}]


def _output_parameter(label: str = "query_sales") -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=f"{label}_output",
        workflow_id="wf_test",
        output_parameter_id=f"op_{label}",
        created_at=_NOW,
        modified_at=_NOW,
    )


def _workflow_parameter(key: str = "dax_query", default_value: str | None = None) -> WorkflowParameter:
    return WorkflowParameter(
        parameter_type=ParameterType.WORKFLOW,
        key=key,
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type="string",
        workflow_id="wf_test",
        default_value=default_value,
        created_at=_NOW,
        modified_at=_NOW,
    )


def _block(**values: object) -> PowerBIQueryBlock:
    fields: dict[str, object] = {
        "label": "query_sales",
        "output_parameter": _output_parameter(),
        "connection_id": "conn_test",
        "dataset_id": _DATASET,
        "query": "EVALUATE VALUES('Sales')",
    }
    fields.update(values)
    return PowerBIQueryBlock(**fields)  # type: ignore[arg-type]


def _block_result(block: PowerBIQueryBlock, **kwargs: object) -> BlockResult:
    return BlockResult(
        success=kwargs.get("success", False),
        failure_reason=kwargs.get("failure_reason"),
        output_parameter=block.output_parameter,
        output_parameter_value=kwargs.get("output_parameter_value"),
        status=kwargs.get("status", BlockStatus.failed),
        workflow_run_block_id=kwargs.get("workflow_run_block_id"),
    )


async def _execute(
    block: PowerBIQueryBlock,
    *,
    context: FakeWorkflowRunContext | None = None,
    with_hop_token: bool = True,
    powerbi_offered: bool = False,
) -> BlockResult:
    context = context or FakeWorkflowRunContext(values={})
    context.organization_id = "org_test"
    if with_hop_token:
        context._powerbi_hop_tokens[block.label] = "test-hop-token"

    async def build_result(self: PowerBIQueryBlock, **kwargs: object) -> BlockResult:
        return _block_result(self, **kwargs)

    async def offered(self: PowerBIQueryBlock, _organization_id: str) -> bool:
        return powerbi_offered

    with (
        patch.object(PowerBIQueryBlock, "get_workflow_run_context", staticmethod(lambda _run_id: context)),
        patch.object(PowerBIQueryBlock, "record_output_parameter_value", new=AsyncMock()),
        patch.object(PowerBIQueryBlock, "build_block_result", new=build_result),
        patch.object(PowerBIQueryBlock, "_powerbi_offered", new=offered),
    ):
        return await block.execute(
            workflow_run_id="wr_test",
            workflow_run_block_id="wfrb_test",
            organization_id="org_test",
        )


def _assert_safe_denial(result: BlockResult, error_code: str, category: FailureCategory | None = None) -> None:
    """The failure output carries the safe error code and its label, if any, and nothing else."""
    output = result.output_parameter_value
    assert isinstance(output, dict)
    if category is None:
        assert output == {"error": error_code}
        return
    assert set(output) == {"error", "failure_category"}
    assert output["error"] == error_code
    assert [entry["category"] for entry in output["failure_category"]] == [category.value]


def _install_persisted_inputs(
    monkeypatch: pytest.MonkeyPatch,
    block: PowerBIQueryBlock,
    parameter: WorkflowParameter,
    *,
    other_parameters: list[WorkflowParameter] | None = None,
    other_blocks: list[object] | None = None,
) -> AsyncMock:
    workflow = SimpleNamespace(
        organization_id="org_test",
        workflow_id="wf_test",
        workflow_definition=SimpleNamespace(
            parameters=[parameter, *(other_parameters or [])], blocks=[block, *(other_blocks or [])]
        ),
    )
    run_parameter = SimpleNamespace(
        workflow_run_id="wr_test", workflow_parameter_id=parameter.workflow_parameter_id, value=_DATASET
    )
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow_for_workflow_run", AsyncMock(return_value=workflow))
    read_parameters = AsyncMock(return_value=[(parameter, run_parameter)])
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", read_parameters)
    return read_parameters


def test_powerbi_query_yaml_round_trips_through_workflow_union_and_converter() -> None:
    definition = WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [],
            "blocks": [
                {
                    "block_type": "powerbi_query",
                    "label": "query_sales",
                    "connection_id": "conn_test",
                    "credential_source": "nango",
                    "dataset_id": _DATASET,
                    "query": "EVALUATE VALUES('Sales')",
                }
            ],
            "vault_name": "",
            "username_key": "",
            "password_key": "",
            "created_at": _NOW,
            "modified_at": _NOW,
        }
    )
    block_yaml = definition.blocks[0]
    assert isinstance(block_yaml, PowerBIQueryBlockYAML)
    assert block_yaml.block_type == BlockType.POWERBI_QUERY
    assert block_yaml.credential_source == "nango"
    assert block_yaml.max_rows == 1_000
    assert block_yaml.model_dump(mode="json")["connection_id"] == "conn_test"

    output = _output_parameter()
    runtime_block = block_yaml_to_block(block_yaml, {output.key: output})
    assert isinstance(runtime_block, PowerBIQueryBlock)
    assert runtime_block.connection_id == "conn_test"
    assert runtime_block.credential_source == "nango"
    assert runtime_block.dataset_id == _DATASET
    assert runtime_block.query == "EVALUATE VALUES('Sales')"
    assert runtime_block.max_rows == 1_000


def test_powerbi_query_credential_source_defaults_to_nango_and_rejects_native() -> None:
    block_yaml = PowerBIQueryBlockYAML(
        label="query_sales",
        connection_id="conn_test",
        dataset_id=_DATASET,
        query="EVALUATE VALUES('Sales')",
    )

    assert block_yaml.credential_source == "nango"
    assert _block().credential_source == "nango"
    with pytest.raises(ValidationError):
        PowerBIQueryBlockYAML(
            label="query_sales",
            connection_id="conn_test",
            credential_source="native",  # type: ignore[arg-type]
            dataset_id=_DATASET,
            query="EVALUATE VALUES('Sales')",
        )
    with pytest.raises(ValidationError):
        _block(credential_source="native")


@pytest.mark.parametrize("max_rows", [0, 100_001, True, 1.0, "1000", "{{ row_cap }}"])
def test_powerbi_query_rejects_non_strict_or_out_of_range_row_cap(max_rows: object) -> None:
    with pytest.raises(ValidationError):
        PowerBIQueryBlockYAML(
            label="query_sales",
            connection_id="conn_test",
            dataset_id=_DATASET,
            query="EVALUATE VALUES('Sales')",
            max_rows=max_rows,  # type: ignore[arg-type]
        )


def test_powerbi_query_requires_connection_dataset_and_query() -> None:
    with pytest.raises(ValidationError):
        PowerBIQueryBlockYAML(label="query_sales", dataset_id=_DATASET, query="EVALUATE VALUES('Sales')")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PowerBIQueryBlockYAML(label="query_sales", connection_id="conn_test", query="EVALUATE 1")  # type: ignore[call-arg]


@pytest.mark.parametrize("connection_id", ["{{ selected_connection }}", "{% set x = connection %}", "   "])
def test_powerbi_query_requires_a_literal_connection_id(connection_id: str) -> None:
    with pytest.raises(ValidationError, match="literal credential ID"):
        PowerBIQueryBlockYAML(
            label="query_sales",
            connection_id=connection_id,
            dataset_id="{{ dataset }}",
            query="{{ dax_query }}",
        )
    with pytest.raises(ValidationError, match="literal credential ID"):
        _block(connection_id=connection_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dataset_id", "{{ trigger_output.dataset_id }}"),
        ("query", "{{ prior_step_output.query }}"),
        ("query", "{{"),
    ],
)
def test_workflow_yaml_preserves_powerbi_templates_for_runtime(field: str, value: str) -> None:
    block = {
        "block_type": "powerbi_query",
        "label": "query_sales",
        "connection_id": "conn_test",
        "dataset_id": _DATASET,
        "query": "EVALUATE VALUES('Sales')",
    }
    block[field] = value

    definition = WorkflowDefinitionYAML.model_validate({"parameters": [], "blocks": [block]})
    assert getattr(definition.blocks[0], field) == value


def test_jinja_template_roots_include_names_that_match_environment_globals() -> None:
    assert get_jinja_template_variable_roots("{{ dict.output }}") == {"dict"}


def test_workflow_yaml_preserves_runtime_dependent_parameter_defaults_for_runtime() -> None:
    definition = WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [
                {"parameter_type": "workflow", "key": "dataset", "workflow_parameter_type": "string"},
                {
                    "parameter_type": "workflow",
                    "key": "dax_query",
                    "workflow_parameter_type": "string",
                    "default_value": "{{ trigger_output.query }}",
                },
            ],
            "blocks": [
                {
                    "block_type": "powerbi_query",
                    "label": "query_sales",
                    "connection_id": "conn_test",
                    "dataset_id": "{{ dataset }}",
                    "query": "{{ dax_query }}",
                }
            ],
        }
    )

    assert isinstance(definition.blocks[0], PowerBIQueryBlockYAML)
    assert definition.blocks[0].query == "{{ dax_query }}"


def test_workflow_yaml_allows_parameter_defaults_that_resolve_to_caller_inputs() -> None:
    definition = WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [
                {"parameter_type": "workflow", "key": "dataset", "workflow_parameter_type": "string"},
                {"parameter_type": "workflow", "key": "caller_query", "workflow_parameter_type": "string"},
                {
                    "parameter_type": "workflow",
                    "key": "dax_query",
                    "workflow_parameter_type": "string",
                    "default_value": "{{ caller_query }}",
                },
            ],
            "blocks": [
                {
                    "block_type": "powerbi_query",
                    "label": "query_sales",
                    "connection_id": "conn_test",
                    "dataset_id": "{{ dataset }}",
                    "query": "{{ dax_query }}",
                }
            ],
        }
    )

    assert definition.blocks[0].label == "query_sales"


@pytest.mark.asyncio
async def test_powerbi_query_resolves_persisted_dataset_and_returns_direct_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(
        values={
            "dataset": _DATASET,
            "dax_query": "EVALUATE VALUES('Sales')",
        }
    )
    context.parameters = {
        "dataset": _workflow_parameter("dataset"),
        "dax_query": _workflow_parameter("dax_query"),
    }
    context.organization_id = "org_test"
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))
    block = _block(
        connection_id="conn_test",
        dataset_id="{{ dataset }}",
        query="EVALUATE VALUES('Sales')\r\n",
        max_rows=20,
        parameters=[_workflow_parameter()],
    )
    _install_persisted_inputs(monkeypatch, block, _workflow_parameter("dataset"))

    result = await _execute(block, context=context)

    assert result.success is True
    assert result.output_parameter_value == _ROWS
    assert context.get_powerbi_hop_token("query_sales") == "test-hop-token"
    run_query.assert_awaited_once_with(
        organization_id="org_test",
        workflow_run_id="wr_test",
        workflow_run_block_id="wfrb_test",
        connection_id="conn_test",
        hop_token="test-hop-token",
        credential_source="nango",
        dataset_id=_DATASET,
        query="EVALUATE VALUES('Sales')",
        max_rows=20,
    )


@pytest.mark.asyncio
async def test_powerbi_does_not_compile_an_unrelated_parameter_default(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _block(dataset_id="{{ dataset }}")
    dangerous = _workflow_parameter("unrelated", "{{ 'x' * 1000000000 }}")
    _install_persisted_inputs(monkeypatch, block, _workflow_parameter("dataset"), other_parameters=[dangerous])
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    with patch("jinja2.Environment.compile", side_effect=AssertionError("must not compile unrelated defaults")):
        result = await _execute(block)

    assert result.success is True
    assert run_query.await_args.kwargs["dataset_id"] == _DATASET


@pytest.mark.asyncio
async def test_powerbi_rejects_repeated_editor_output_interpolations_without_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(values={"p_output": "x" * 4096})
    context.carried_block_labels = {"p"}
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))
    read_workflow = AsyncMock(side_effect=AssertionError("invalid expressions must not be resolved"))
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow_for_workflow_run", read_workflow)

    with patch("jinja2.Environment.compile", side_effect=AssertionError("must not render repeated interpolation")):
        result = await _execute(_block(dataset_id="{{p_output}}" * 170), context=context)

    _assert_safe_denial(result, "unsafe_query_input_source")
    read_workflow.assert_not_awaited()
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_rejects_dataset_input_named_after_saved_block_output(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _block(dataset_id="{{ p_output }}")
    prior = SimpleNamespace(label="p", block_type=BlockType.POWERBI_QUERY)
    _install_persisted_inputs(monkeypatch, block, _workflow_parameter("p_output"), other_blocks=[prior])
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(block)

    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()
    app.DATABASE.workflow_runs.get_workflow_run_parameters.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "wrong_run", "templated_default", "lookup_failure"])
async def test_powerbi_rejects_unverified_dataset_inputs(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    block = _block(dataset_id="{{ dataset }}")
    parameter = _workflow_parameter("dataset", "{{ other }}" if case == "templated_default" else None)
    read_parameters = _install_persisted_inputs(monkeypatch, block, parameter)
    if case == "missing":
        read_parameters.return_value = []
    elif case == "wrong_run":
        read_parameters.return_value[0][1].workflow_run_id = "wr_other"
    elif case == "lookup_failure":
        read_parameters.side_effect = RuntimeError("database unavailable")
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(block)

    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset_id", [_DATASET[:-1] + "g", "x" * 256])
async def test_powerbi_rejects_resolved_dataset_that_is_not_a_uuid(
    monkeypatch: pytest.MonkeyPatch, dataset_id: str
) -> None:
    block = _block(dataset_id="{{ dataset }}")
    read_parameters = _install_persisted_inputs(monkeypatch, block, _workflow_parameter("dataset"))
    read_parameters.return_value[0][1].value = dataset_id
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(block)

    _assert_safe_denial(result, "unsafe_query_input_source")
    assert result.failure_reason == "Power BI dataset_id must resolve to a UUID from a workflow input or literal."
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_query_keeps_capability_for_loop_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    context = FakeWorkflowRunContext(values={}, block_metadata={"query_sales": {"current_index": 0}})
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(), context=context)

    assert result.success is True
    assert context.get_powerbi_hop_token("query_sales") == "test-hop-token"


@pytest.mark.asyncio
async def test_powerbi_runtime_rejects_block_output_templates_before_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(values={"dataset": _DATASET, "prior_step_output": {"query": "EVALUATE 1"}})
    context.parameters = {"dataset": _workflow_parameter("dataset")}
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{ prior_step_output.query }}"), context=context)

    assert result.success is False
    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_runtime_rejects_caller_input_shadowed_by_block_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(values={"q": "EVALUATE UNSAFE"}, workflow_run_outputs={"q": "EVALUATE UNSAFE"})
    context.parameters = {"q": _workflow_parameter("q")}
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{ q }}"), context=context)

    assert result.success is False
    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_runtime_rejects_caller_input_shadowed_by_loop_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(
        values={"current_item": "EVALUATE UNSAFE"},
        block_metadata={"query_sales": {"current_item": "EVALUATE UNSAFE"}},
    )
    context.parameters = {"current_item": _workflow_parameter("current_item")}
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{ current_item }}"), context=context)

    assert result.success is False
    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_runtime_rejects_global_name_shadowed_by_block_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {"output": "EVALUATE UNSAFE"}
    context = FakeWorkflowRunContext(values={"dict": output}, workflow_run_outputs={"dict": output})
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{ dict.output }}"), context=context)

    assert result.success is False
    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_runtime_reports_malformed_template_as_safe_block_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(values={})
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{"), context=context)

    assert result.success is False
    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_rejects_templated_query_without_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    context = FakeWorkflowRunContext(values={"dax_query": "EVALUATE VALUES('Private_Sales_Table')"})
    context.parameters = {"dax_query": _workflow_parameter("dax_query")}
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{ dax_query + 1 }}", parameters=[_workflow_parameter()]), context=context)

    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "invalid_shape"),
    [
        ({"query": "{{ dax_query }}"}, True),
        ({"query": "{% if true %}EVALUATE 1{% endif %}"}, True),
        ({"query": "{# comment #}EVALUATE 1"}, True),
        ({"dataset_id": "{{ dataset.value }}"}, True),
        ({"dataset_id": "{{ dataset }} suffix"}, True),
        ({"dataset_id": "not-a-uuid"}, True),
        ({}, False),
        ({"dataset_id": "{{ dataset }}"}, False),
    ],
)
async def test_powerbi_input_shape_is_checked_before_missing_capability(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, str], invalid_shape: bool
) -> None:
    read_workflow = AsyncMock()
    read_parameters = AsyncMock()
    run_query = AsyncMock()
    monkeypatch.setattr(app.DATABASE.workflows, "get_workflow_for_workflow_run", read_workflow)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run_parameters", read_parameters)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(**values), with_hop_token=False)

    if invalid_shape:
        _assert_safe_denial(result, "unsafe_query_input_source")
        assert (
            result.failure_reason == "Power BI dataset_id and query may use only workflow input parameters or literals."
        )
    else:
        _assert_safe_denial(result, "integration_unavailable")
        assert result.failure_reason == INTEGRATION_UNAVAILABLE_REASON
    read_workflow.assert_not_awaited()
    read_parameters.assert_not_awaited()
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_without_a_capability_on_a_live_deployment_names_the_editor_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_query = AsyncMock()
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(), with_hop_token=False, powerbi_offered=True)

    assert result.success is False
    _assert_safe_denial(result, "human_run_required")
    assert result.failure_reason == NO_ACCESS_REASON
    run_query.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", [AgentFunction(), SimpleNamespace()])
async def test_powerbi_offered_is_false_without_a_working_cloud_gate(
    monkeypatch: pytest.MonkeyPatch, gate: object
) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", gate)

    assert await _block()._powerbi_offered("org_test") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_category"),
    [
        ("event_trigger_run", FailureCategory.CREDENTIAL_ERROR),
        ("trigger_parent_owner_mismatch", FailureCategory.CREDENTIAL_ERROR),
    ],
)
async def test_refused_run_is_labeled_by_the_block_not_by_its_wording(
    monkeypatch: pytest.MonkeyPatch, code: str, expected_category: FailureCategory
) -> None:
    async def denied(**_: object) -> list[dict[str, object]]:
        raise PowerBIQueryError(code)

    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=denied))
    block = _block()

    result = await _execute(block)

    run_status, run_failure_reason, run_category = WorkflowService._resolve_block_terminal_outcome(
        block=block, block_result=result
    )
    assert run_status == WorkflowRunStatus.failed
    assert run_category is not None and [entry["category"] for entry in run_category] == [expected_category.value]
    wording_alone = classify_from_failure_reason(run_failure_reason, fallback_to_unknown=True) or []
    assert expected_category.value not in {entry["category"] for entry in wording_alone}


@pytest.mark.asyncio
async def test_powerbi_runtime_rejects_runtime_output_hidden_in_parameter_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeWorkflowRunContext(values={"dax_query": "EVALUATE 1"})
    context.parameters = {"dax_query": _workflow_parameter(default_value="{{ trigger_output.query }}")}
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))

    result = await _execute(_block(query="{{ dax_query }}"), context=context)

    assert result.success is False
    _assert_safe_denial(result, "unsafe_query_input_source")
    run_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_powerbi_query_failures_are_safe_and_oss_hook_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())

    result = await _execute(_block())

    assert result.success is False
    _assert_safe_denial(result, "unsupported_runtime")
    assert "Cloud" not in (result.failure_reason or "")

    async def provider_failure(**_: object) -> list[dict[str, object]]:
        raise RuntimeError("raw provider response and EVALUATE private_table")

    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=provider_failure))
    result = await _execute(_block())
    assert result.success is False
    _assert_safe_denial(result, "internal_error", FailureCategory.INFRASTRUCTURE_ERROR)
    assert result.failure_reason == INTERNAL_ERROR_REASON
    assert "raw provider" not in str(result.output_parameter_value)
    assert "private_table" not in (result.failure_reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_error", "expected_reason", "expected_category"),
    [
        (
            "principal_unavailable",
            "human_run_required",
            HUMAN_RUN_REQUIRED_REASON,
            None,
        ),
        (
            "event_trigger_run",
            "human_run_required",
            HUMAN_RUN_REQUIRED_REASON,
            FailureCategory.CREDENTIAL_ERROR,
        ),
        (
            "trigger_parent_owner_mismatch",
            "trigger_chain_owner_required",
            "Power BI queries require every triggering workflow to be saved by the connection owner.",
            FailureCategory.CREDENTIAL_ERROR,
        ),
        (
            "row_limit_exceeded",
            "row_limit_exceeded",
            "Power BI query exceeded the row limit.",
            None,
        ),
        (
            "integration_unavailable",
            "integration_unavailable",
            INTEGRATION_UNAVAILABLE_REASON,
            None,
        ),
        (
            "provider_rejected_query",
            "query_failed",
            "Power BI query failed. Check the connection, dataset ID, query, and row limit.",
            None,
        ),
        (
            "workflow_owner_resave_required",
            "workflow_owner_resave_required",
            "Re-save this workflow as the Power BI connection owner before running it.",
            None,
        ),
    ],
)
async def test_powerbi_query_surfaces_safe_runtime_denials(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected_error: str,
    expected_reason: str,
    expected_category: FailureCategory | None,
) -> None:
    async def denied(**_: object) -> list[dict[str, object]]:
        raise PowerBIQueryError(code)

    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=denied))

    result = await _execute(_block())

    assert result.success is False
    _assert_safe_denial(result, expected_error, expected_category)
    assert result.failure_reason == expected_reason


@pytest.mark.asyncio
async def test_powerbi_runtime_rejects_templated_connection_even_if_model_was_mutated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_query = AsyncMock(return_value=_ROWS)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(execute_powerbi_query_for_run=run_query))
    block = _block()
    block.connection_id = "{{ connection }}"

    result = await _execute(block)

    _assert_safe_denial(result, "connection_id_must_be_literal")
    run_query.assert_not_awaited()


def test_powerbi_query_converter_binds_declared_parameters() -> None:
    output = _output_parameter()
    parameter = _workflow_parameter()
    block_yaml = PowerBIQueryBlockYAML(
        label="query_sales",
        connection_id="conn_test",
        dataset_id=_DATASET,
        query="{{ dax_query }}",
        parameter_keys=[parameter.key],
    )

    runtime_block = block_yaml_to_block(block_yaml, {output.key: output, parameter.key: parameter})

    assert isinstance(runtime_block, PowerBIQueryBlock)
    assert runtime_block.get_all_parameters("wr_test") == [parameter]
