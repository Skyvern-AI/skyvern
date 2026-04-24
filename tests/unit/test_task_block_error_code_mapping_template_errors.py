from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.workflow.exceptions import FailedToFormatJinjaStyleParameter
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType


def _build_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="task_output",
        description=None,
        output_parameter_id="output-1",
        workflow_id="workflow-1",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )


def _build_mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.values = {}
    ctx.secrets = {}
    ctx.include_secrets_in_templates = False
    ctx.get_block_metadata = MagicMock(return_value={})
    ctx.workflow_title = "workflow-title"
    ctx.workflow_id = "workflow-id"
    ctx.workflow_permanent_id = "workflow-perm-id"
    ctx.workflow_run_id = "workflow-run-id"
    ctx.browser_session_id = None
    ctx.workflow_run_outputs = {}
    ctx.build_workflow_run_summary = MagicMock(return_value={})
    ctx.workflow = None
    return ctx


def test_error_code_mapping_value_reports_precise_jinja_syntax_error() -> None:
    block = TaskBlock(
        label="block_1",
        block_type="task",
        title="Test block",
        output_parameter=_build_output_parameter(),
        error_code_mapping={
            "ACCOUNT_GROUP_NOT_FOUND": (
                "return this error when {{ current_value.account_number }} and {{account group}} are missing"
            )
        },
    )
    ctx = _build_mock_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter) as exc_info:
        block.format_potential_template_parameters(ctx)

    error_message = str(exc_info.value)
    assert "error_code_mapping value for key 'ACCOUNT_GROUP_NOT_FOUND'" in error_message
    assert "expected token 'end of print statement'" in error_message


def test_error_code_mapping_key_reports_precise_jinja_syntax_error() -> None:
    block = TaskBlock(
        label="block_1",
        block_type="task",
        title="Test block",
        output_parameter=_build_output_parameter(),
        error_code_mapping={
            "{{ error code }}": "return this error",
        },
    )
    ctx = _build_mock_context()

    with pytest.raises(FailedToFormatJinjaStyleParameter) as exc_info:
        block.format_potential_template_parameters(ctx)

    error_message = str(exc_info.value)
    assert "error_code_mapping key '{{ error code }}'" in error_message
    assert "expected token 'end of print statement'" in error_message
