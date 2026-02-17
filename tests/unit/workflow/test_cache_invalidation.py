"""
Tests for workflow cache invalidation logic (SKY-7016).

Verifies that changes to the model field (both at workflow settings level and block level)
do not trigger cache invalidation.
"""

from datetime import datetime, timezone

from skyvern.forge.sdk.workflow.models.block import BlockType, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.service import _get_workflow_definition_core_data


def make_output_parameter(key: str) -> OutputParameter:
    """Create a test output parameter."""
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="Test output parameter",
        output_parameter_id="test-output-id",
        workflow_id="test-workflow-id",
        created_at=datetime.now(timezone.utc),
        modified_at=datetime.now(timezone.utc),
    )


def make_task_block(label: str, model: dict | None = None) -> TaskBlock:
    """Create a test task block with optional model configuration."""
    return TaskBlock(
        label=label,
        block_type=BlockType.TASK,
        output_parameter=make_output_parameter(f"{label}_output"),
        url="https://example.com",
        title="Test Task",
        navigation_goal="Complete the task",
        model=model,
    )


class TestCacheInvalidation:
    """Tests for the _get_workflow_definition_core_data function."""

    def test_model_field_excluded_from_block_comparison(self) -> None:
        """
        SKY-7016: Verify that block-level model changes don't trigger cache invalidation.

        The model field should be excluded from the comparison data.
        """
        # Create two identical blocks, differing only in the model field
        block_without_model = make_task_block("task1", model=None)
        block_with_model = make_task_block("task1", model={"model_name": "gpt-4o"})

        # Create workflow definitions with these blocks
        definition_without_model = WorkflowDefinition(
            parameters=[],
            blocks=[block_without_model],
        )
        definition_with_model = WorkflowDefinition(
            parameters=[],
            blocks=[block_with_model],
        )

        # Get the core data used for comparison
        core_data_without = _get_workflow_definition_core_data(definition_without_model)
        core_data_with = _get_workflow_definition_core_data(definition_with_model)

        # The core data should be identical (model field excluded)
        assert core_data_without == core_data_with, (
            "Model field should be excluded from comparison. "
            "Changing block-level model should not trigger cache invalidation."
        )

    def test_model_field_not_in_core_data(self) -> None:
        """Verify that the model field is completely removed from the core data."""
        block = make_task_block("task1", model={"model_name": "claude-3-sonnet"})
        definition = WorkflowDefinition(
            parameters=[],
            blocks=[block],
        )

        core_data = _get_workflow_definition_core_data(definition)

        # Check that model is not present in any block
        for block_data in core_data.get("blocks", []):
            assert "model" not in block_data, "Model field should be removed from block data"

    def test_other_block_changes_still_detected(self) -> None:
        """Verify that non-model block changes are still detected."""
        # Create two blocks with different navigation goals
        block1 = make_task_block("task1")
        block1.navigation_goal = "Goal A"

        block2 = make_task_block("task1")
        block2.navigation_goal = "Goal B"

        definition1 = WorkflowDefinition(parameters=[], blocks=[block1])
        definition2 = WorkflowDefinition(parameters=[], blocks=[block2])

        core_data1 = _get_workflow_definition_core_data(definition1)
        core_data2 = _get_workflow_definition_core_data(definition2)

        # These should be different (navigation_goal is not excluded)
        assert core_data1 != core_data2, "Non-model changes should still be detected for cache invalidation"

    def test_different_models_same_core_data(self) -> None:
        """Verify that switching between different models produces same core data."""
        models = [
            None,
            {"model_name": "gpt-4o"},
            {"model_name": "claude-3-opus"},
            {"model_name": "gemini-pro", "extra_param": "value"},
        ]

        definitions = []
        for model in models:
            block = make_task_block("task1", model=model)
            definition = WorkflowDefinition(parameters=[], blocks=[block])
            definitions.append(_get_workflow_definition_core_data(definition))

        # All core data should be identical
        for i in range(1, len(definitions)):
            assert definitions[0] == definitions[i], (
                f"Core data should be identical regardless of model. Definition 0 vs {i} differ."
            )

    def test_timestamps_excluded_from_comparison(self) -> None:
        """Verify that timestamps are properly excluded from comparison."""
        # Create two blocks with different timestamps
        block1 = make_task_block("task1")
        block2 = make_task_block("task1")

        # Simulate different timestamps by recreating output parameters
        block2.output_parameter = OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="task1_output",
            description="Test output parameter",
            output_parameter_id="different-output-id",  # Different ID
            workflow_id="different-workflow-id",  # Different workflow ID
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),  # Different timestamp
            modified_at=datetime(2024, 6, 1, tzinfo=timezone.utc),  # Different timestamp
        )

        definition1 = WorkflowDefinition(parameters=[], blocks=[block1])
        definition2 = WorkflowDefinition(parameters=[], blocks=[block2])

        core_data1 = _get_workflow_definition_core_data(definition1)
        core_data2 = _get_workflow_definition_core_data(definition2)

        # These should be identical (timestamps and IDs are excluded)
        assert core_data1 == core_data2, "Timestamps and IDs should be excluded from comparison"
