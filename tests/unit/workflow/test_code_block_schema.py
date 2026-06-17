from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import WorkflowDefinitionValidationException
from skyvern.forge.sdk.workflow.models.block import CodeBlock, CodeBlockStep
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.workflow_definition_converter import _code_block_step_span_issue, block_yaml_to_block
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


def _convert_with_steps(code: str, steps: list[dict]) -> CodeBlock:
    block_yaml = CodeBlockYAML(label="code_1", code=code, steps=steps)
    block = block_yaml_to_block(block_yaml, {"code_1_output": _output_parameter()})
    assert isinstance(block, CodeBlock)
    return block


def test_converter_accepts_valid_line_spans() -> None:
    block = _convert_with_steps(
        "a = 1\nb = 2\nc = 3",
        [
            {"description": "first", "action_type": "goto_url", "line_start": 1, "line_end": 2},
            {"description": "second", "action_type": "click", "line_start": 3, "line_end": 3},
        ],
    )
    assert block.steps is not None
    assert (block.steps[0].line_start, block.steps[0].line_end) == (1, 2)
    assert (block.steps[1].line_start, block.steps[1].line_end) == (3, 3)


def test_converter_accepts_steps_without_line_spans() -> None:
    block = _convert_with_steps(
        "x = 1",
        [
            {"description": "no span", "action_type": "goto_url"},
            {"description": "still no span", "action_type": "extract"},
        ],
    )
    assert block.steps is not None
    assert block.steps[0].line_start is None and block.steps[0].line_end is None


def test_converter_accepts_line_start_only_step() -> None:
    block = _convert_with_steps(
        "a = 1\nb = 2",
        [{"description": "lone start", "action_type": "click", "line_start": 2}],
    )
    assert block.steps is not None
    assert block.steps[0].line_start == 2 and block.steps[0].line_end is None


@pytest.mark.parametrize(
    "line_start, line_end, code_line_count, expect_issue",
    [
        (1, 2, 3, False),
        (2, None, 3, False),
        (None, None, 3, False),
        (None, 2, 3, True),
        (3, 2, 3, True),
        (0, 1, 3, True),
        (1, -2, 3, True),
        (1, 4, 3, True),
        (5, None, 3, True),
    ],
)
def test_code_block_step_span_issue(
    line_start: int | None, line_end: int | None, code_line_count: int, expect_issue: bool
) -> None:
    step = CodeBlockStep(line_start=line_start, line_end=line_end)
    assert (_code_block_step_span_issue(step, code_line_count) is not None) == expect_issue


def test_converter_snaps_out_of_range_span_to_synthesized_span() -> None:
    # Line 1 (x = 1) is not an action; the click is the only synthesized step, on line 2.
    block = _convert_with_steps(
        "x = 1\nawait page.click('#go')",
        [{"description": "click go", "action_type": "click", "line_start": 1, "line_end": 99}],
    )
    assert block.steps is not None
    # Snapped to the synthesized span (2, 2) rather than clamped to (1, 2).
    assert (block.steps[0].line_start, block.steps[0].line_end) == (2, 2)
    assert block.steps[0].description == "click go"


@pytest.mark.parametrize(
    "step",
    [
        {"description": "lone end", "action_type": "click", "line_end": 2},
        {"description": "inverted", "action_type": "click", "line_start": 3, "line_end": 2},
        {"description": "zero", "action_type": "click", "line_start": 0, "line_end": 1},
        {"description": "negative", "action_type": "click", "line_start": 1, "line_end": -2},
        {"description": "beyond end", "action_type": "click", "line_start": 1, "line_end": 9},
        {"description": "lone start beyond end", "action_type": "click", "line_start": 5},
    ],
)
def test_converter_drops_unrepairable_span_to_null(step: dict) -> None:
    # Action-less code has no synthesized step to snap to, so an invalid span is dropped to
    # null (the step survives as display-only metadata) instead of 422-ing the whole save.
    block = _convert_with_steps("a = 1\nb = 2\nc = 3", [step])
    assert block.steps is not None
    assert block.steps[0].line_start is None and block.steps[0].line_end is None
    assert block.steps[0].description == step["description"]


def test_converter_preserves_valid_span_and_repairs_invalid_neighbor() -> None:
    block = _convert_with_steps(
        "await page.goto('https://example.com')\nawait page.click('#go')",
        [
            {"description": "open", "action_type": "goto_url", "line_start": 1, "line_end": 1},
            {"description": "click", "action_type": "click", "line_start": 2, "line_end": 50},
        ],
    )
    assert block.steps is not None
    # A valid span is left untouched; only the invalid neighbor is reconciled.
    assert (block.steps[0].line_start, block.steps[0].line_end) == (1, 1)
    assert (block.steps[1].line_start, block.steps[1].line_end) == (2, 2)
    assert block.steps[1].action_type is ActionType.CLICK


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
