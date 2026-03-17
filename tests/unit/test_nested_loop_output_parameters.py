"""Tests for nested loop output parameter collision fix (SKY-7375).

Ensures that:
1. Block labels are validated for uniqueness across ALL nesting levels (not just top-level)
2. WorkflowRunContext.init() gracefully handles duplicate output parameter keys
3. Nested loop workflows with unique labels pass validation correctly
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.exceptions import (
    OutputParameterKeyCollisionError,
    WorkflowDefinitionHasDuplicateBlockLabels,
)
from skyvern.forge.sdk.workflow.models.block import ForLoopBlock, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.forge.sdk.workflow.workflow_definition_converter import (
    _collect_all_block_labels,
    _create_all_output_parameters_for_workflow,
    convert_workflow_definition,
)
from skyvern.schemas.workflows import (
    ForLoopBlockYAML,
    TaskBlockYAML,
    WorkflowDefinitionYAML,
)


class TestCollectAllBlockLabels:
    """Tests for _collect_all_block_labels helper function."""

    def test_flat_blocks(self) -> None:
        """Test collecting labels from flat (non-nested) blocks."""
        blocks = [
            TaskBlockYAML(label="block_1", url="https://example.com"),
            TaskBlockYAML(label="block_2", url="https://example.com"),
        ]
        labels = _collect_all_block_labels(blocks)
        assert labels == ["block_1", "block_2"]

    def test_nested_loop_blocks(self) -> None:
        """Test collecting labels from blocks nested inside a loop."""
        blocks = [
            ForLoopBlockYAML(
                label="outer_loop",
                loop_blocks=[
                    TaskBlockYAML(label="inner_task", url="https://example.com"),
                ],
            ),
        ]
        labels = _collect_all_block_labels(blocks)
        assert labels == ["outer_loop", "inner_task"]

    def test_deeply_nested_loop_blocks(self) -> None:
        """Test collecting labels from deeply nested loops (loop inside loop)."""
        blocks = [
            ForLoopBlockYAML(
                label="outer_loop",
                loop_blocks=[
                    ForLoopBlockYAML(
                        label="inner_loop",
                        loop_blocks=[
                            TaskBlockYAML(label="deep_task", url="https://example.com"),
                        ],
                    ),
                ],
            ),
        ]
        labels = _collect_all_block_labels(blocks)
        assert labels == ["outer_loop", "inner_loop", "deep_task"]

    def test_mixed_flat_and_nested(self) -> None:
        """Test collecting labels from a mix of flat and nested blocks."""
        blocks = [
            TaskBlockYAML(label="top_task", url="https://example.com"),
            ForLoopBlockYAML(
                label="loop_1",
                loop_blocks=[
                    TaskBlockYAML(label="loop_1_task", url="https://example.com"),
                ],
            ),
            TaskBlockYAML(label="bottom_task", url="https://example.com"),
        ]
        labels = _collect_all_block_labels(blocks)
        assert labels == ["top_task", "loop_1", "loop_1_task", "bottom_task"]

    def test_detects_duplicate_across_nesting_levels(self) -> None:
        """Test that duplicate labels across nesting levels are collected (not deduplicated)."""
        blocks = [
            TaskBlockYAML(label="shared_label", url="https://example.com"),
            ForLoopBlockYAML(
                label="outer_loop",
                loop_blocks=[
                    TaskBlockYAML(label="shared_label", url="https://example.com"),
                ],
            ),
        ]
        labels = _collect_all_block_labels(blocks)
        assert labels.count("shared_label") == 2


class TestCreateAllOutputParametersForNestedLoops:
    """Tests for _create_all_output_parameters_for_workflow with nested loops."""

    def test_creates_parameters_for_nested_blocks(self) -> None:
        """Test that output parameters are created for all nested blocks."""
        blocks = [
            ForLoopBlockYAML(
                label="outer_loop",
                loop_blocks=[
                    TaskBlockYAML(label="inner_task", url="https://example.com"),
                ],
            ),
        ]
        params = _create_all_output_parameters_for_workflow(
            workflow_id="test_wf",
            block_yamls=blocks,
        )
        assert "outer_loop" in params
        assert "inner_task" in params
        assert params["outer_loop"].key == "outer_loop_output"
        assert params["inner_task"].key == "inner_task_output"

    def test_creates_parameters_for_deeply_nested_blocks(self) -> None:
        """Test that output parameters are created for deeply nested loops."""
        blocks = [
            ForLoopBlockYAML(
                label="outer_loop",
                loop_blocks=[
                    ForLoopBlockYAML(
                        label="inner_loop",
                        loop_blocks=[
                            TaskBlockYAML(label="deep_task", url="https://example.com"),
                        ],
                    ),
                ],
            ),
        ]
        params = _create_all_output_parameters_for_workflow(
            workflow_id="test_wf",
            block_yamls=blocks,
        )
        assert len(params) == 3
        assert "outer_loop" in params
        assert "inner_loop" in params
        assert "deep_task" in params


class TestConvertWorkflowDefinitionNestedLoopValidation:
    """Tests for convert_workflow_definition with nested loop duplicate label detection."""

    def test_rejects_duplicate_labels_across_nesting_levels(self) -> None:
        """Test that duplicate labels across nesting levels are rejected."""
        workflow_yaml = WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                TaskBlockYAML(label="task_a", url="https://example.com"),
                ForLoopBlockYAML(
                    label="loop_1",
                    loop_blocks=[
                        TaskBlockYAML(label="task_a", url="https://example.com"),
                    ],
                ),
            ],
        )
        with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
            convert_workflow_definition(workflow_yaml, workflow_id="test_wf")

    def test_rejects_duplicate_labels_within_nested_loops(self) -> None:
        """Test that duplicate labels within two different nested loops are rejected."""
        workflow_yaml = WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                ForLoopBlockYAML(
                    label="loop_1",
                    loop_blocks=[
                        TaskBlockYAML(label="shared_task", url="https://example.com"),
                    ],
                ),
                ForLoopBlockYAML(
                    label="loop_2",
                    loop_blocks=[
                        TaskBlockYAML(label="shared_task", url="https://example.com"),
                    ],
                ),
            ],
        )
        with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
            convert_workflow_definition(workflow_yaml, workflow_id="test_wf")

    def test_accepts_unique_labels_in_nested_loops(self) -> None:
        """Test that nested loops with unique labels pass validation."""
        workflow_yaml = WorkflowDefinitionYAML(
            parameters=[],
            blocks=[
                ForLoopBlockYAML(
                    label="outer_loop",
                    loop_variable_reference="outer_var",
                    loop_blocks=[
                        ForLoopBlockYAML(
                            label="inner_loop",
                            loop_variable_reference="inner_var",
                            loop_blocks=[
                                TaskBlockYAML(label="deep_task", url="https://example.com"),
                            ],
                        ),
                    ],
                ),
            ],
        )
        result = convert_workflow_definition(workflow_yaml, workflow_id="test_wf")
        assert result is not None
        assert len(result.blocks) == 1


class TestWorkflowDefinitionValidateNestedLabels:
    """Tests for WorkflowDefinition.validate() with nested labels."""

    def _make_output_param(self, label: str) -> OutputParameter:
        now = datetime.now(UTC)
        return OutputParameter(
            output_parameter_id=f"op_{label}",
            key=f"{label}_output",
            workflow_id="test_wf",
            created_at=now,
            modified_at=now,
        )

    def test_validate_catches_duplicate_nested_labels(self) -> None:
        """Test that validate() catches duplicate labels across nesting levels."""
        inner_task = TaskBlock(
            label="shared_label",
            output_parameter=self._make_output_param("shared_label_inner"),
        )
        outer_task = TaskBlock(
            label="shared_label",
            output_parameter=self._make_output_param("shared_label_outer"),
        )
        loop_block = ForLoopBlock(
            label="loop_1",
            output_parameter=self._make_output_param("loop_1"),
            loop_blocks=[inner_task],
        )
        definition = WorkflowDefinition(
            parameters=[],
            blocks=[outer_task, loop_block],
        )
        with pytest.raises(WorkflowDefinitionHasDuplicateBlockLabels):
            definition.validate()

    def test_validate_passes_with_unique_nested_labels(self) -> None:
        """Test that validate() passes with unique labels across all nesting levels."""
        inner_task = TaskBlock(
            label="inner_task",
            output_parameter=self._make_output_param("inner_task"),
        )
        outer_task = TaskBlock(
            label="outer_task",
            output_parameter=self._make_output_param("outer_task"),
        )
        loop_block = ForLoopBlock(
            label="loop_1",
            output_parameter=self._make_output_param("loop_1"),
            loop_blocks=[inner_task],
        )
        definition = WorkflowDefinition(
            parameters=[],
            blocks=[outer_task, loop_block],
        )
        definition.validate()  # Should not raise


class TestWorkflowGetOutputParameterNested:
    """Tests for Workflow.get_output_parameter() searching nested loop blocks (SKY-8397)."""

    def _make_output_param(self, label: str) -> OutputParameter:
        now = datetime.now(UTC)
        return OutputParameter(
            output_parameter_id=f"op_{label}",
            key=f"{label}_output",
            workflow_id="test_wf",
            created_at=now,
            modified_at=now,
        )

    def _make_workflow(self, blocks: list) -> Workflow:
        now = datetime.now(UTC)
        return Workflow(
            workflow_id="wf_test",
            organization_id="org_test",
            title="Test",
            workflow_permanent_id="wpid_test",
            version=1,
            is_saved_task=False,
            workflow_definition=WorkflowDefinition(parameters=[], blocks=blocks),
            created_at=now,
            modified_at=now,
        )

    def test_finds_top_level_block(self) -> None:
        task = TaskBlock(label="top_task", output_parameter=self._make_output_param("top_task"))
        workflow = self._make_workflow([task])
        assert workflow.get_output_parameter("top_task") is task.output_parameter

    def test_finds_block_inside_loop(self) -> None:
        inner_task = TaskBlock(label="inner_task", output_parameter=self._make_output_param("inner_task"))
        loop = ForLoopBlock(
            label="loop_1", output_parameter=self._make_output_param("loop_1"), loop_blocks=[inner_task]
        )
        workflow = self._make_workflow([loop])
        assert workflow.get_output_parameter("inner_task") is inner_task.output_parameter

    def test_finds_block_inside_nested_loop(self) -> None:
        deep_task = TaskBlock(label="deep_task", output_parameter=self._make_output_param("deep_task"))
        inner_loop = ForLoopBlock(
            label="inner_loop", output_parameter=self._make_output_param("inner_loop"), loop_blocks=[deep_task]
        )
        outer_loop = ForLoopBlock(
            label="outer_loop", output_parameter=self._make_output_param("outer_loop"), loop_blocks=[inner_loop]
        )
        workflow = self._make_workflow([outer_loop])
        assert workflow.get_output_parameter("deep_task") is deep_task.output_parameter

    def test_returns_none_for_nonexistent_label(self) -> None:
        task = TaskBlock(label="task_1", output_parameter=self._make_output_param("task_1"))
        workflow = self._make_workflow([task])
        assert workflow.get_output_parameter("nonexistent") is None


class TestWorkflowRunContextDuplicateOutputParameters:
    """Tests for WorkflowRunContext.init() raising on duplicate output parameter keys with a clear error message."""

    def _make_output_param(self, label: str, param_id: str) -> OutputParameter:
        now = datetime.now(UTC)
        return OutputParameter(
            output_parameter_id=param_id,
            key=f"{label}_output",
            workflow_id="test_wf",
            created_at=now,
            modified_at=now,
        )

    @pytest.mark.asyncio
    async def test_duplicate_output_parameter_keys_raises_with_helpful_message(self) -> None:
        """Test that duplicate output parameter keys raise OutputParameterKeyCollisionError with a clear message."""
        now = datetime.now(UTC)
        org = Organization(
            organization_id="org_test",
            organization_name="Test Org",
            created_at=now,
            modified_at=now,
        )
        aws_client = MagicMock()

        # Two output parameters with the same key but different IDs (simulates duplicate DB rows)
        param_1 = self._make_output_param("block_1", "op_first")
        param_2 = self._make_output_param("block_1", "op_second")

        with pytest.raises(OutputParameterKeyCollisionError) as exc_info:
            await WorkflowRunContext.init(
                aws_client=aws_client,
                organization=org,
                workflow_run_id="wr_test",
                workflow_title="Test Workflow",
                workflow_id="wf_test",
                workflow_permanent_id="wpid_test",
                workflow_parameter_tuples=[],
                workflow_output_parameters=[param_1, param_2],
                context_parameters=[],
                secret_parameters=[],
            )

        error_msg = str(exc_info.value)
        assert "block_1" in error_msg
        assert "unique label" in error_msg
        assert "rename" in error_msg.lower()

    @pytest.mark.asyncio
    async def test_unique_output_parameter_keys_all_registered(self) -> None:
        """Test that unique output parameter keys are all registered normally."""
        now = datetime.now(UTC)
        org = Organization(
            organization_id="org_test",
            organization_name="Test Org",
            created_at=now,
            modified_at=now,
        )
        aws_client = MagicMock()

        param_1 = self._make_output_param("block_1", "op_1")
        param_2 = self._make_output_param("block_2", "op_2")

        context = await WorkflowRunContext.init(
            aws_client=aws_client,
            organization=org,
            workflow_run_id="wr_test",
            workflow_title="Test Workflow",
            workflow_id="wf_test",
            workflow_permanent_id="wpid_test",
            workflow_parameter_tuples=[],
            workflow_output_parameters=[param_1, param_2],
            context_parameters=[],
            secret_parameters=[],
        )

        assert "block_1_output" in context.parameters
        assert "block_2_output" in context.parameters
