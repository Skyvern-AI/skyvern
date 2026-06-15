from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import WorkflowDefinitionValidationException
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockStep
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
from skyvern.schemas.workflows import CodeBlockYAML
from skyvern.webeye.actions.action_types import ActionType


def _output_parameter() -> OutputParameter:
    return OutputParameter(
        output_parameter_id="op_1",
        key="code_output",
        workflow_id="w_1",
        created_at="2026-01-01T00:00:00",
        modified_at="2026-01-01T00:00:00",
    )


def test_code_block_yaml_accepts_code_first_fields() -> None:
    block_yaml = CodeBlockYAML(
        label="code_1",
        code="x = 1",
        prompt="Open {{ url }} and read the total",
        steps=[
            {"description": "Open the page", "action_type": "goto_url", "line_start": 1, "line_end": 2},
            {"description": "Read the total", "action_type": "extract"},
        ],
    )
    assert block_yaml.prompt == "Open {{ url }} and read the total"
    assert block_yaml.steps is not None and block_yaml.steps[0].action_type == "goto_url"


def test_code_block_yaml_defaults_keep_legacy_shape() -> None:
    block_yaml = CodeBlockYAML(label="code_1", code="x = 1")
    assert block_yaml.prompt is None
    assert block_yaml.steps is None


def test_converter_coerces_step_action_type_to_enum() -> None:
    block_yaml = CodeBlockYAML(
        label="code_1",
        code="x = 1",
        steps=[
            {"description": "Open the page", "action_type": "goto_url"},
            {"description": "No explicit action type"},
        ],
    )
    block = block_yaml_to_block(block_yaml, {"code_1_output": _output_parameter()})
    assert isinstance(block, CodeBlock)
    assert block.steps is not None
    assert block.steps[0].action_type is ActionType.GOTO_URL
    assert block.steps[1].action_type is ActionType.NULL_ACTION


def test_converter_rejects_invalid_step_action_type_with_block_context() -> None:
    block_yaml = CodeBlockYAML(
        label="code_1",
        code="x = 1",
        steps=[
            {"description": "Open the page", "action_type": "goto_url"},
            {"description": "Bad step", "action_type": "not_a_real_action"},
        ],
    )
    with pytest.raises(WorkflowDefinitionValidationException) as exc_info:
        block_yaml_to_block(block_yaml, {"code_1_output": _output_parameter()})
    message = str(exc_info.value)
    assert "code_1" in message
    assert "index 1" in message
    assert "action_type" in message


def test_code_block_model_roundtrip() -> None:
    block = CodeBlock(
        label="code_1",
        output_parameter=_output_parameter(),
        code="x = 1",
        prompt="g",
        steps=[CodeBlockStep(description="d", action_type=ActionType.CLICK)],
    )
    dumped = block.model_dump()
    assert dumped["prompt"] == "g"
    assert dumped["steps"][0]["action_type"] == "click"


def _make_workflow_run_context(values: dict | None = None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_run_id="wr_1",
        aws_client=MagicMock(),
    )
    if values:
        ctx.values.update(values)
    return ctx


def test_format_potential_template_parameters_renders_prompt() -> None:
    # Regression: prompt must be jinja-rendered before it reaches the task v1
    # (mirrors the task block), not passed through raw.
    block = CodeBlock(
        label="code_1",
        output_parameter=_output_parameter(),
        code="x = {{ count }}",
        prompt="Open {{ url }}",
    )
    ctx = _make_workflow_run_context({"count": "1", "url": "https://example.com"})

    block.format_potential_template_parameters(ctx)

    assert block.code == "x = 1"
    assert block.prompt == "Open https://example.com"
