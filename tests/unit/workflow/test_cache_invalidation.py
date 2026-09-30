"""
Tests for workflow cache invalidation logic (SKY-7016).

Verifies that changes to the model field (both at workflow settings level and block level)
do not trigger cache invalidation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.workflow.models.block import BlockType, CodeBlock, CodeBlockStep, TaskBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.forge.sdk.workflow.service import WorkflowService, _get_workflow_definition_core_data
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.run_enums import RunEngine
from skyvern.schemas.workflows import WorkflowDefinitionYAML
from skyvern.webeye.actions.action_types import ActionType


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


def make_code_block(
    label: str,
    goal: str | None = None,
    steps: list[CodeBlockStep] | None = None,
) -> CodeBlock:
    """Create a test code block with optional code-first annotation fields."""
    return CodeBlock(
        label=label,
        block_type=BlockType.CODE,
        output_parameter=make_output_parameter(f"{label}_output"),
        code="x = 1",
        prompt=goal,
        steps=steps,
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

    def test_code_block_annotation_edits_excluded_from_comparison(self) -> None:
        """Editing display/derived code-block annotations must not invalidate the cached script."""
        plain = make_code_block("code1")
        annotated = make_code_block(
            "code1",
            steps=[CodeBlockStep(description="Open the page", action_type=ActionType.GOTO_URL)],
        )

        core_data_plain = _get_workflow_definition_core_data(WorkflowDefinition(parameters=[], blocks=[plain]))
        core_data_annotated = _get_workflow_definition_core_data(WorkflowDefinition(parameters=[], blocks=[annotated]))

        assert core_data_plain == core_data_annotated, (
            "Code block steps are a display/derived annotation and should be excluded from comparison."
        )

    def test_code_block_annotation_fields_not_in_core_data(self) -> None:
        block = make_code_block(
            "code1",
            steps=[CodeBlockStep(description="Click go", action_type=ActionType.CLICK)],
        )
        core_data = _get_workflow_definition_core_data(WorkflowDefinition(parameters=[], blocks=[block]))

        for block_data in core_data.get("blocks", []):
            assert "steps" not in block_data

    def test_code_block_goal_change_still_detected(self) -> None:
        """A goal reprompt regenerates code and steps, so a goal edit must keep invalidating."""
        definition1 = WorkflowDefinition(parameters=[], blocks=[make_code_block("code1", goal="Goal A")])
        definition2 = WorkflowDefinition(parameters=[], blocks=[make_code_block("code1", goal="Goal B")])

        core_data1 = _get_workflow_definition_core_data(definition1)
        core_data2 = _get_workflow_definition_core_data(definition2)

        assert core_data1 != core_data2, "Code block goal changes must still trigger cache invalidation"

    def test_task_block_criteria_changes_still_detected(self) -> None:
        """Criteria on task-family blocks are runtime-consumed; edits there must keep invalidating."""
        block1 = make_task_block("task1")
        block1.complete_criterion = "Criterion A"

        block2 = make_task_block("task1")
        block2.complete_criterion = "Criterion B"

        core_data1 = _get_workflow_definition_core_data(WorkflowDefinition(parameters=[], blocks=[block1]))
        core_data2 = _get_workflow_definition_core_data(WorkflowDefinition(parameters=[], blocks=[block2]))

        assert core_data1 != core_data2, "Task block criteria changes must still trigger cache invalidation"

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


_TASK_BLOCK_LABELS = ("open", "approve", "nav")
_CUTOFF = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _yaml_definition(nav_engine: str | None) -> WorkflowDefinitionYAML:
    nav: dict = {"block_type": "navigation", "label": "nav", "url": "https://example.com", "navigation_goal": "Go"}
    if nav_engine is not None:
        nav["engine"] = nav_engine
    return WorkflowDefinitionYAML.model_validate(
        {
            "parameters": [],
            "blocks": [
                {"block_type": "goto_url", "label": "open", "url": "https://example.com"},
                {"block_type": "human_interaction", "label": "approve", "recipients": ["ops@example.com"]},
                nav,
            ],
        }
    )


def _as_the_previous_image_stored_it(definition: WorkflowDefinition) -> dict:
    # Before SKY-17436 every task block's engine defaulted to skyvern-1.0 and was always written.
    stored = definition.model_dump(mode="json")
    for block in stored["blocks"]:
        if block["label"] in _TASK_BLOCK_LABELS and block.get("engine") is None:
            block["engine"] = RunEngine.skyvern_v1.value
    return stored


async def _resave(
    stored: dict,
    resaved: WorkflowDefinition,
    monkeypatch: pytest.MonkeyPatch,
    *,
    born_at: datetime | None,
    cutoff: datetime | None = _CUTOFF,
) -> AsyncMock:
    """Save ``resaved`` over a workflow whose previous version holds ``stored``; returns the cache clear."""
    monkeypatch.setattr(settings, "TASK_V3_CHOSEN_ENGINE_CUTOFF", cutoff)
    now = datetime.now(timezone.utc)
    previous = Workflow(
        workflow_id="w_1",
        organization_id="o_1",
        title="t",
        workflow_permanent_id="wpid_1",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition.model_validate(stored),
        created_at=now,
        modified_at=now,
    )
    service = WorkflowService()
    database = MagicMock()
    database.workflows.get_workflow_by_permanent_id = AsyncMock(return_value=previous)
    with (
        patch("skyvern.forge.sdk.workflow.service.app") as mock_app,
        patch("skyvern.forge.sdk.experimentation.workflow_block_engine.app") as engine_app,
        patch.object(service, "_partition_cached_blocks", AsyncMock(return_value=([MagicMock()], []))),
        patch.object(service, "_clear_cached_block_groups", AsyncMock()) as clear_groups,
    ):
        mock_app.DATABASE = database
        engine_app.DATABASE.workflows.get_workflow_permanent_id_created_at = AsyncMock(return_value=born_at)
        await service.maybe_delete_cached_code(
            previous.model_copy(update={"version": 2}), workflow_definition=resaved, organization_id="o_1"
        )
    return clear_groups


@pytest.mark.parametrize(
    ("born_at", "cutoff"),
    [
        (_CUTOFF - timedelta(days=30), _CUTOFF),
        # No cutoff set: the two route alike whatever the birth lookup returns.
        (None, None),
    ],
)
@pytest.mark.parametrize(
    "nav_engine",
    [
        # The editor, which sends the stored skyvern-1.0 back.
        RunEngine.skyvern_v1.value,
        # An API caller re-sending the YAML it created the workflow with, which omitted the engine.
        None,
    ],
)
@pytest.mark.asyncio
async def test_an_unchanged_resave_of_a_previously_stored_definition_keeps_its_cached_scripts(
    monkeypatch: pytest.MonkeyPatch, nav_engine: str | None, born_at: datetime | None, cutoff: datetime | None
) -> None:
    resaved = convert_workflow_definition(_yaml_definition(nav_engine), "w_1")
    stored = _as_the_previous_image_stored_it(resaved)

    clear_groups = await _resave(stored, resaved, monkeypatch, born_at=born_at, cutoff=cutoff)

    clear_groups.assert_not_awaited()
    if nav_engine is not None:
        # What the editor saves is what was stored, in every engine field.
        assert resaved.model_dump(mode="json") == stored


@pytest.mark.parametrize(
    "born_at",
    [
        _CUTOFF + timedelta(days=1),
        # The birth lookup failed: the workflow may be past the cutoff, so the two stay distinct.
        None,
    ],
)
@pytest.mark.asyncio
async def test_past_the_cutoff_switching_a_legacy_pin_to_default_is_a_change(
    monkeypatch: pytest.MonkeyPatch, born_at: datetime | None
) -> None:
    # Unset runs on v3 there and skyvern-1.0 is an explicit pin, so the cached script must be cleared;
    # the engine-inert blocks around it still compare equal.
    stored = convert_workflow_definition(_yaml_definition(RunEngine.skyvern_v1.value), "w_1").model_dump(mode="json")
    resaved = convert_workflow_definition(_yaml_definition(None), "w_1")

    clear_groups = await _resave(stored, resaved, monkeypatch, born_at=born_at)

    clear_groups.assert_awaited_once()
    assert clear_groups.await_args.kwargs["plan"].label == "nav"
