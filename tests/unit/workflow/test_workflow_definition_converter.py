"""Regression tests for workflow-definition conversion."""

import pytest

from skyvern.forge.sdk.workflow.exceptions import WorkflowDefinitionHasUndefinedParameters
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import ForLoopBlockYAML, TaskBlockYAML, WorkflowDefinitionYAML


def test_undefined_for_loop_parameter_is_a_422_validation_error() -> None:
    definition = WorkflowDefinitionYAML(
        parameters=[],
        blocks=[
            ForLoopBlockYAML(
                label="loop_items",
                loop_over_parameter_key="items",
                loop_blocks=[TaskBlockYAML(label="process_item", url="https://example.com")],
            )
        ],
    )

    with pytest.raises(WorkflowDefinitionHasUndefinedParameters) as exc_info:
        convert_workflow_definition(definition, workflow_id="wf_test")

    assert exc_info.value.status_code == 422
    assert "loop_items" in exc_info.value.message
    assert "'items'" in exc_info.value.message
