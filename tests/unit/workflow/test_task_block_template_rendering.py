from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _make_output_parameter(key: str = "task_output") -> OutputParameter:
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id="op_task_template_test",
        workflow_id="w_task_template_test",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def _make_workflow_run_context(values: dict | None = None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_task_template_test",
        workflow_permanent_id="wpid_task_template_test",
        workflow_run_id="wr_task_template_test",
        aws_client=MagicMock(),
    )
    if values:
        ctx.values.update(values)
    return ctx


def test_format_potential_template_parameters_renders_error_code_mapping() -> None:
    block = TaskBlock(
        label="task_with_error_codes",
        output_parameter=_make_output_parameter(),
        title="task title",
        error_code_mapping={
            "ERR_{{ region }}": "{{ reason }} for {{ region }}",
            "STATIC_CODE": "static description",
        },
    )
    ctx = _make_workflow_run_context({"region": "US", "reason": "login failed"})

    block.format_potential_template_parameters(ctx)

    assert block.error_code_mapping == {
        "ERR_US": "login failed for US",
        "STATIC_CODE": "static description",
    }


def test_format_potential_template_parameters_with_no_error_code_mapping() -> None:
    block = TaskBlock(
        label="task_without_error_codes",
        output_parameter=_make_output_parameter(),
        title="task title",
        error_code_mapping=None,
    )
    ctx = _make_workflow_run_context({"region": "US"})

    block.format_potential_template_parameters(ctx)

    assert block.error_code_mapping is None


def test_malformed_jinja_in_title_raises_with_template_context() -> None:
    """Syntax error in title template should raise FailedToFormatJinjaStyleParameter with the template string."""
    block = TaskBlock(
        label="bad_title",
        output_parameter=_make_output_parameter(),
        title="{{ unclosed",
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="unclosed"):
        block.format_potential_template_parameters(ctx)


def test_malformed_jinja_in_navigation_goal_raises_with_template_context() -> None:
    """Syntax error in navigation_goal should raise FailedToFormatJinjaStyleParameter."""
    block = TaskBlock(
        label="bad_nav",
        output_parameter=_make_output_parameter(),
        title="ok title",
        navigation_goal="{{ {% bad }}",
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="bad"):
        block.format_potential_template_parameters(ctx)


def test_malformed_jinja_in_error_code_mapping_raises_with_template_context() -> None:
    """Syntax error in error_code_mapping value should raise FailedToFormatJinjaStyleParameter."""
    block = TaskBlock(
        label="bad_ecm",
        output_parameter=_make_output_parameter(),
        title="ok title",
        error_code_mapping={"ERR_1": "{{ unclosed"},
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="unclosed"):
        block.format_potential_template_parameters(ctx)


def test_render_error_raises_with_template_context() -> None:
    """A template that compiles but fails at render time should also raise with template context."""
    block = TaskBlock(
        label="render_err",
        output_parameter=_make_output_parameter(),
        title="{{ foo | no_such_filter }}",
    )
    ctx = _make_workflow_run_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter, match="no_such_filter"):
        block.format_potential_template_parameters(ctx)
