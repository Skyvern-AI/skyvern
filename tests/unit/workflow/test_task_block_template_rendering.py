from datetime import datetime, timezone
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
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
