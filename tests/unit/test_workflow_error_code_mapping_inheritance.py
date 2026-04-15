"""Tests for workflow-level error_code_mapping inheritance into blocks at execution time."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="task1_output",
        description="test output",
        output_parameter_id="op_task1",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_task_block(error_code_mapping: dict[str, str] | None = None) -> TaskBlock:
    return TaskBlock(
        label="task1",
        output_parameter=_make_output_parameter(),
        title="task title",
        error_code_mapping=error_code_mapping,
    )


def _make_workflow(error_code_mapping: dict[str, str] | None) -> Workflow:
    workflow_definition = WorkflowDefinition(
        parameters=[],
        blocks=[],
        error_code_mapping=error_code_mapping,
    )
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=workflow_definition,
        created_at=now,
        modified_at=now,
    )


def _make_workflow_run_context(workflow_error_code_mapping: dict[str, str] | None) -> WorkflowRunContext:
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
        workflow=_make_workflow(workflow_error_code_mapping),
    )
    return ctx


class TestWorkflowLevelErrorCodeMappingInheritance:
    def test_block_inherits_workflow_mapping_when_none(self) -> None:
        block = _make_task_block(error_code_mapping=None)
        ctx = _make_workflow_run_context({"ACCOUNT_NOT_FOUND": "If no records found, terminate"})

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"ACCOUNT_NOT_FOUND": "If no records found, terminate"}

    def test_block_merges_with_workflow_mapping(self) -> None:
        block = _make_task_block(error_code_mapping={"BLOCK_ERROR": "block-level error"})
        ctx = _make_workflow_run_context({"WORKFLOW_ERROR": "workflow-level error"})

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {
            "WORKFLOW_ERROR": "workflow-level error",
            "BLOCK_ERROR": "block-level error",
        }

    def test_block_level_overrides_workflow_on_conflict(self) -> None:
        block = _make_task_block(error_code_mapping={"SHARED_KEY": "block wins"})
        ctx = _make_workflow_run_context({"SHARED_KEY": "workflow loses"})

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"SHARED_KEY": "block wins"}

    def test_no_workflow_mapping_preserves_block(self) -> None:
        block = _make_task_block(error_code_mapping={"BLOCK_ERROR": "only block"})
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping == {"BLOCK_ERROR": "only block"}

    def test_both_none_stays_none(self) -> None:
        block = _make_task_block(error_code_mapping=None)
        ctx = _make_workflow_run_context(workflow_error_code_mapping=None)

        block.format_potential_template_parameters(ctx)

        assert block.error_code_mapping is None

    def test_sanitizer_rewrites_references_in_workflow_error_code_mapping(self) -> None:
        """Auto-sanitized labels/param keys must be rewritten inside workflow-level error_code_mapping."""
        from skyvern.schemas.workflows import sanitize_workflow_yaml_with_references

        workflow_yaml = {
            "workflow_definition": {
                "parameters": [{"key": "bad-key", "parameter_type": "workflow", "workflow_parameter_type": "string"}],
                "blocks": [{"label": "block-1", "block_type": "task", "url": "https://example.com"}],
                "error_code_mapping": {
                    "ERR": "reason {{ bad-key }} from {{ block-1_output }}",
                    "ERR_{{ bad-key }}": "key-side ref to {{ block-1_output }}",
                },
            }
        }
        sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
        mapping = sanitized["workflow_definition"]["error_code_mapping"]
        assert mapping == {
            "ERR": "reason {{ bad_key }} from {{ block_1_output }}",
            "ERR_{{ bad_key }}": "key-side ref to {{ block_1_output }}",
        }

    def test_sanitizer_does_not_chain_rewrites_in_error_code_mapping(self) -> None:
        """Chained substitutions must not occur when one sanitized label collides with another's final name."""
        from skyvern.schemas.workflows import sanitize_workflow_yaml_with_references

        # Both labels need sanitization; the first normalizes to "foo_bar", colliding
        # with the second whose normalization is "foo_bar", so it becomes "foo_bar_2".
        workflow_yaml = {
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"label": "foo/bar", "block_type": "task", "url": "https://example.com"},
                    {"label": "foo-bar", "block_type": "task", "url": "https://example.com"},
                ],
                "error_code_mapping": {
                    "ERR": "first {{ foo/bar_output }}, second {{ foo-bar_output }}",
                },
            }
        }
        sanitized = sanitize_workflow_yaml_with_references(workflow_yaml)
        # foo/bar -> foo_bar should stay as foo_bar (not chain-rewrite to foo_bar_2).
        mapping = sanitized["workflow_definition"]["error_code_mapping"]
        assert mapping == {"ERR": "first {{ foo_bar_output }}, second {{ foo_bar_2_output }}"}

    def test_round_trip_does_not_bake_workflow_defaults(self) -> None:
        """Regression: converted blocks must not persist workflow-level keys.

        Without this guarantee, removing a workflow-level code would leave stale copies in each block
        after a read-modify-write round-trip.
        """
        from skyvern.forge.sdk.workflow.workflow_definition_converter import block_yaml_to_block
        from skyvern.schemas.workflows import TaskBlockYAML

        block_yaml = TaskBlockYAML(
            label="task1",
            url="https://example.com",
            navigation_goal="Do something",
            error_code_mapping={"BLOCK_ERROR": "only block"},
        )
        output_param = _make_output_parameter()
        parameters = {output_param.key: output_param}

        block = block_yaml_to_block(block_yaml, parameters)
        assert isinstance(block, TaskBlock)
        assert block.error_code_mapping == {"BLOCK_ERROR": "only block"}
