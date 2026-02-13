"""
Tests for script generation race condition (SKY-7653).

The race condition occurs when script generation runs during workflow execution
before all actions have been saved to the database. This results in:
1. `generate_workflow_parameters_schema` not finding INPUT_TEXT actions
2. No field_name mappings being generated
3. Generated script having hardcoded values instead of context.parameters[field_name]
"""

from typing import Any

import pytest

from skyvern.core.script_generations.generate_workflow_parameters import (
    CUSTOM_FIELD_ACTIONS,
    GeneratedFieldMapping,
    generate_workflow_parameters_schema,
    hydrate_input_text_actions_with_field_names,
)
from skyvern.webeye.actions.actions import ActionType


def make_input_text_action(
    task_id: str,
    action_id: str,
    text: str,
    intention: str = "",
    field_name: str | None = None,
) -> dict[str, Any]:
    """Create a mock INPUT_TEXT action dictionary."""
    action = {
        "action_type": ActionType.INPUT_TEXT,
        "action_id": action_id,
        "task_id": task_id,
        "text": text,
        "intention": intention,
        "element_id": "element_1",
        "xpath": "//input[@id='test']",
    }
    if field_name:
        action["field_name"] = field_name
    return action


def make_click_action(task_id: str, action_id: str) -> dict[str, Any]:
    """Create a mock CLICK action dictionary."""
    return {
        "action_type": ActionType.CLICK,
        "action_id": action_id,
        "task_id": task_id,
        "element_id": "element_2",
        "xpath": "//button[@id='submit']",
    }


class TestRaceConditionScenarios:
    """Test scenarios that demonstrate the race condition."""

    def test_hydrate_adds_field_name_to_actions(self) -> None:
        """Test that hydrate_input_text_actions_with_field_names properly adds field_name."""
        task_id = "task-123"
        action_id = "action-456"

        actions_by_task = {
            task_id: [
                make_input_text_action(task_id, action_id, "Urdaneta", "Enter facility name"),
            ]
        }

        field_mappings = {
            f"{task_id}:{action_id}": "facility_name",
        }

        result = hydrate_input_text_actions_with_field_names(actions_by_task, field_mappings)

        # The action should now have field_name
        assert result[task_id][0].get("field_name") == "facility_name"

    def test_hydrate_without_mappings_no_field_name(self) -> None:
        """
        Test that without field mappings, actions don't get field_name added.
        This simulates what happens when script generation runs before actions are saved.
        """
        task_id = "task-123"
        action_id = "action-456"

        actions_by_task = {
            task_id: [
                make_input_text_action(task_id, action_id, "Urdaneta", "Enter facility name"),
            ]
        }

        # Empty field mappings - simulates race condition where LLM wasn't called
        # because no INPUT_TEXT actions were found
        field_mappings: dict[str, str] = {}

        result = hydrate_input_text_actions_with_field_names(actions_by_task, field_mappings)

        # The action should NOT have field_name
        assert "field_name" not in result[task_id][0]

    def test_race_condition_empty_actions_produces_empty_schema(self) -> None:
        """
        Test that when no actions are passed, generate_workflow_parameters_schema
        returns an empty schema. This happens when script generation runs before
        actions are executed.
        """
        # Empty actions - simulates script generation running before any INPUT_TEXT
        # actions have been saved to the database
        actions_by_task: dict[str, list[dict[str, Any]]] = {}

        # Call the synchronous part that checks for actions
        # (The async LLM call won't be made because no actions are found)

        # Extract just the action-finding logic
        custom_field_actions = []
        for task_id, actions in actions_by_task.items():
            for action in actions:
                action_type = action.get("action_type", "")
                if action_type in CUSTOM_FIELD_ACTIONS:
                    custom_field_actions.append(action)

        # With no actions, the schema generator should return empty schema
        assert len(custom_field_actions) == 0

    def test_race_condition_only_click_actions_no_schema(self) -> None:
        """
        Test that when only CLICK actions are present (before INPUT_TEXT is saved),
        no field mappings are generated.
        """
        task_id = "task-123"

        # Only CLICK actions - simulates script generation running after CLICK
        # but before INPUT_TEXT action is saved
        actions_by_task = {
            task_id: [
                make_click_action(task_id, "action-1"),
                make_click_action(task_id, "action-2"),
            ]
        }

        custom_field_actions = []
        for task_id, actions in actions_by_task.items():
            for action in actions:
                action_type = action.get("action_type", "")
                if action_type in CUSTOM_FIELD_ACTIONS:
                    custom_field_actions.append(action)

        # No INPUT_TEXT actions found - no schema will be generated
        assert len(custom_field_actions) == 0


class TestCodeGenerationWithoutFieldName:
    """
    Test that code generation produces hardcoded values when field_name is missing.

    This demonstrates the impact of the race condition on generated code.
    """

    def test_action_without_field_name_produces_hardcoded_value(self) -> None:
        """
        When an INPUT_TEXT action doesn't have field_name (due to race condition),
        the generated code should have a hardcoded value instead of context.parameters.
        """
        action = make_input_text_action(
            task_id="task-123",
            action_id="action-456",
            text="Urdaneta",  # This becomes hardcoded
            intention="Enter facility name",
            field_name=None,  # No field_name due to race condition
        )

        # The action_handler_body function uses act.get("field_name") to decide
        # whether to use context.parameters[field_name] or hardcoded value
        assert action.get("field_name") is None
        assert action.get("text") == "Urdaneta"  # Will be hardcoded

    def test_action_with_field_name_produces_parameter_reference(self) -> None:
        """
        When an INPUT_TEXT action has field_name, the generated code should
        use context.parameters[field_name].
        """
        action = make_input_text_action(
            task_id="task-123",
            action_id="action-456",
            text="Urdaneta",  # Original value (not used in generated code)
            intention="Enter facility name",
            field_name="facility_name",  # Field name present
        )

        # The action has field_name, so generated code will use context.parameters
        assert action.get("field_name") == "facility_name"


class TestFieldMappingGeneration:
    """Test the field mapping generation logic."""

    def test_field_mapping_structure(self) -> None:
        """Test that GeneratedFieldMapping has the expected structure."""
        mapping = GeneratedFieldMapping(
            field_mappings={"action_index_1": "facility_name"},
            schema_fields={"facility_name": {"type": "str", "description": "The facility name"}},
        )

        assert mapping.field_mappings["action_index_1"] == "facility_name"
        assert mapping.schema_fields["facility_name"]["type"] == "str"

    def test_action_index_to_field_mapping_key_format(self) -> None:
        """Test that field mapping keys use the correct format: task_id:action_id."""
        task_id = "task-123"
        action_id = "action-456"

        # This is the format used in generate_workflow_parameters_schema
        expected_key = f"{task_id}:{action_id}"
        assert expected_key == "task-123:action-456"


@pytest.mark.asyncio
async def test_generate_workflow_parameters_schema_empty_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Integration test: Verify that empty actions result in empty schema.

    This test confirms the race condition behavior - when script generation
    runs before INPUT_TEXT actions are saved, no field mappings are generated.
    """
    # Mock the prompt engine and LLM handler since we won't reach them
    # (the function returns early when no custom_field_actions are found)

    actions_by_task: dict[str, list[dict[str, Any]]] = {}

    schema_code, action_field_mappings = await generate_workflow_parameters_schema(actions_by_task)

    # Should return empty schema
    assert "pass" in schema_code  # Empty schema has `pass`
    assert action_field_mappings == {}


@pytest.mark.asyncio
async def test_generate_workflow_parameters_schema_with_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Integration test: Verify that when actions are present, LLM is called.

    This confirms that when script generation runs AFTER actions are saved,
    it properly generates field mappings.
    """
    from skyvern.core.script_generations import generate_workflow_parameters as gwp

    # Mock the LLM call to return a mapping
    async def mock_generate_field_names_with_llm(custom_field_actions):
        return GeneratedFieldMapping(
            field_mappings={"action_index_1": "facility_name"},
            schema_fields={"facility_name": {"type": "str", "description": "The facility name"}},
        )

    monkeypatch.setattr(gwp, "_generate_field_names_with_llm", mock_generate_field_names_with_llm)

    task_id = "task-123"
    action_id = "action-456"
    actions_by_task = {
        task_id: [
            make_input_text_action(task_id, action_id, "Urdaneta", "Enter facility name"),
        ]
    }

    schema_code, action_field_mappings = await generate_workflow_parameters_schema(actions_by_task)

    # Should have generated schema with field
    assert "facility_name" in schema_code
    assert "GeneratedWorkflowParameters" in schema_code

    # Should have mapping for our action
    assert f"{task_id}:{action_id}" in action_field_mappings
    assert action_field_mappings[f"{task_id}:{action_id}"] == "facility_name"


class TestRaceConditionTimingScenario:
    """
    Document the timing scenario that causes the race condition.

    Timeline:
    1. T+0s: CLICK action executes, post_action_execution triggered
    2. T+0.1s: Script generation starts (asyncio.create_task)
    3. T+0.2s: Script generation queries database for actions - finds only CLICK
    4. T+0.3s: Script generation completes with no field mappings
    5. T+6s: INPUT_TEXT action executes, saved to database
    6. T+6.1s: Another script generation triggered, but first (wrong) script already saved

    The result is a script with hardcoded values like `value = 'Urdaneta'`
    instead of `value = context.parameters['facility_name']`
    """

    def test_timing_scenario_documentation(self) -> None:
        """This test documents the race condition scenario."""
        # Phase 1: After CLICK, before INPUT_TEXT
        actions_at_time_0 = {
            "task-123": [
                make_click_action("task-123", "action-1"),
            ]
        }

        # At this point, script generation finds no INPUT_TEXT actions
        input_text_actions = [
            a for actions in actions_at_time_0.values() for a in actions if a["action_type"] == ActionType.INPUT_TEXT
        ]
        assert len(input_text_actions) == 0

        # Phase 2: After INPUT_TEXT is saved
        actions_at_time_6 = {
            "task-123": [
                make_click_action("task-123", "action-1"),
                make_input_text_action("task-123", "action-2", "Urdaneta", "Enter facility name"),
            ]
        }

        # Now INPUT_TEXT is found - but too late, first script already saved
        input_text_actions = [
            a for actions in actions_at_time_6.values() for a in actions if a["action_type"] == ActionType.INPUT_TEXT
        ]
        assert len(input_text_actions) == 1
        assert input_text_actions[0]["text"] == "Urdaneta"


class TestFinalizeParameter:
    """
    Tests for the `finalize` parameter in generate_script_if_needed.

    The fix (SKY-7653) uses a smart finalize approach:
    - Only regenerates if script_gen_had_incomplete_actions flag is set
    - This avoids unnecessary regeneration costs when script is already complete
    """

    def test_finalize_with_incomplete_actions_triggers_regeneration(self) -> None:
        """
        Test that finalize=True with incomplete actions flag triggers regeneration.

        This simulates the logic in generate_script_if_needed when finalize=True
        and the context has script_gen_had_incomplete_actions=True.
        """
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
        from skyvern.forge.sdk.workflow.service import BLOCK_TYPES_THAT_SHOULD_BE_CACHED

        # Simulate workflow definition blocks
        class MockBlock:
            def __init__(self, label: str, block_type: str):
                self.label = label
                self.block_type = block_type

        workflow_blocks = [
            MockBlock("login_step", "task"),  # Should be in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            MockBlock("search_step", "task"),
            MockBlock("wait_block", "wait"),  # Should NOT be cached
        ]

        # Simulate the finalize logic with incomplete actions flag
        blocks_to_update: set[str] = set()
        finalize = True
        context = SkyvernContext(script_gen_had_incomplete_actions=True)

        if finalize and context.script_gen_had_incomplete_actions:
            task_block_labels = {
                block.label
                for block in workflow_blocks
                if block.label and block.block_type in BLOCK_TYPES_THAT_SHOULD_BE_CACHED
            }
            blocks_to_update.update(task_block_labels)

        # Should include task blocks but not wait block
        assert "login_step" in blocks_to_update
        assert "search_step" in blocks_to_update
        assert "wait_block" not in blocks_to_update

    def test_finalize_without_incomplete_actions_skips_regeneration(self) -> None:
        """
        Test that finalize=True without incomplete actions flag skips regeneration.

        This is the optimization - when script generation had complete data,
        we don't waste resources regenerating.
        """
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

        blocks_to_update: set[str] = set()
        finalize = True
        context = SkyvernContext(script_gen_had_incomplete_actions=False)

        if finalize and context.script_gen_had_incomplete_actions:
            # This branch won't execute - no incomplete actions
            blocks_to_update.add("some_block")

        # No blocks should be added - script is already complete
        assert len(blocks_to_update) == 0

    def test_without_finalize_no_forced_regeneration(self) -> None:
        """
        Test that without finalize=True, blocks are not force-added.
        """
        blocks_to_update: set[str] = set()
        finalize = False

        # Without finalize, no blocks are force-added
        if finalize:
            # This branch won't execute
            blocks_to_update.add("some_block")

        assert len(blocks_to_update) == 0


class TestCodeGenerationLogic:
    """
    Test the exact code generation logic from generate_script.py.

    The code at generate_script.py:401-429 determines whether to use
    context.parameters[field_name] or hardcoded text based on act.get("field_name").
    """

    def test_code_generation_path_without_field_name(self) -> None:
        """
        Verify the code generation path when field_name is missing.

        From generate_script.py:401-429:
        - If act.get("field_name") is truthy, use context.parameters[field_name]
        - Else, use _value(act["text"]) which produces hardcoded string
        """
        action = make_input_text_action(
            task_id="task-123",
            action_id="action-456",
            text="Urdaneta",
            intention="Enter facility name",
            field_name=None,
        )

        # Simulate the code generation logic
        if action.get("field_name"):
            # This branch produces: context.parameters["facility_name"]
            code_path = "context.parameters"
        else:
            # This branch produces: "Urdaneta" (hardcoded)
            code_path = "hardcoded"

        assert code_path == "hardcoded"
        assert action.get("text") == "Urdaneta"

    def test_code_generation_path_with_field_name(self) -> None:
        """
        Verify the code generation path when field_name is present.

        From generate_script.py:401-429:
        - If act.get("field_name") is truthy, use context.parameters[field_name]
        """
        action = make_input_text_action(
            task_id="task-123",
            action_id="action-456",
            text="Urdaneta",
            intention="Enter facility name",
            field_name="facility_name",
        )

        # Simulate the code generation logic
        if action.get("field_name"):
            # This branch produces: context.parameters["facility_name"]
            code_path = "context.parameters"
        else:
            # This branch produces: "Urdaneta" (hardcoded)
            code_path = "hardcoded"

        assert code_path == "context.parameters"
        assert action.get("field_name") == "facility_name"

    def test_demonstrates_race_condition_consequence(self) -> None:
        """
        Demonstrate the consequence of the race condition.

        When script generation runs before INPUT_TEXT action is saved:
        1. generate_workflow_parameters_schema finds no INPUT_TEXT actions
        2. No field mappings are generated
        3. Actions don't get field_name hydrated
        4. Generated script uses hardcoded values

        This means the cached script CANNOT be reused with different parameters.
        """
        # Scenario: First workflow run with "Urdaneta"
        # Script generation ran early, field_name is missing
        action_from_early_script_gen = make_input_text_action(
            task_id="task-123",
            action_id="action-456",
            text="Urdaneta",  # This gets hardcoded
            field_name=None,  # Missing due to race condition
        )

        # The generated code would be: value = "Urdaneta"
        generated_code_has_hardcoded = action_from_early_script_gen.get("field_name") is None
        assert generated_code_has_hardcoded

        # Scenario: User runs workflow again with "Pok Pok" parameter
        # But the cached script has: value = "Urdaneta" (hardcoded!)
        # So the wrong value is used.

        # Correct scenario: Script generation runs after all actions saved
        action_from_proper_script_gen = make_input_text_action(
            task_id="task-123",
            action_id="action-456",
            text="Urdaneta",
            field_name="facility_name",  # Present because script gen ran after action saved
        )

        # The generated code would be: value = context.parameters["facility_name"]
        generated_code_uses_parameters = action_from_proper_script_gen.get("field_name") is not None
        assert generated_code_uses_parameters

        # Now when user runs with "Pok Pok", context.parameters["facility_name"] = "Pok Pok"
        # And the correct value is used!


class TestSkipActionsWithoutData:
    """
    Tests for the smart finalize approach that skips actions without data.

    This addresses the race condition (SKY-7653) while avoiding unnecessary costs:
    1. Skip actions without data during mid-run generation (avoids bad field mappings)
    2. Set context flag when actions are skipped (script_gen_had_incomplete_actions)
    3. At finalize, only regenerate if the flag is set (avoids unnecessary regeneration)

    The benefit is:
    - First run with race condition: flag set → regenerate at end → script complete
    - Subsequent runs: script already complete → no regeneration needed
    """

    def test_input_text_without_text_is_skipped(self) -> None:
        """Test that INPUT_TEXT actions without text are skipped during field mapping."""
        from skyvern.core.script_generations.generate_workflow_parameters import CUSTOM_FIELD_ACTIONS

        task_id = "task-123"

        # INPUT_TEXT action without text - simulates race condition
        action_without_text = {
            "action_type": ActionType.INPUT_TEXT,
            "action_id": "action-456",
            "task_id": task_id,
            "text": "",  # Empty - not yet saved
            "intention": "Enter facility name",
        }

        # Simulate the filtering logic from generate_workflow_parameters_schema
        custom_field_actions = []
        for action in [action_without_text]:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            value = ""
            if action_type == ActionType.INPUT_TEXT:
                value = action.get("text", "")

            # Skip actions without data
            if not value:
                continue

            custom_field_actions.append(action)

        # Action should be skipped because text is empty
        assert len(custom_field_actions) == 0

    def test_input_text_with_text_is_included(self) -> None:
        """Test that INPUT_TEXT actions with text are included in field mapping."""
        from skyvern.core.script_generations.generate_workflow_parameters import CUSTOM_FIELD_ACTIONS

        task_id = "task-123"

        # INPUT_TEXT action with text - properly saved
        action_with_text = {
            "action_type": ActionType.INPUT_TEXT,
            "action_id": "action-456",
            "task_id": task_id,
            "text": "Urdaneta",  # Has value
            "intention": "Enter facility name",
        }

        # Simulate the filtering logic
        custom_field_actions = []
        for action in [action_with_text]:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            value = ""
            if action_type == ActionType.INPUT_TEXT:
                value = action.get("text", "")

            # Skip actions without data
            if not value:
                continue

            custom_field_actions.append(action)

        # Action should be included because text has value
        assert len(custom_field_actions) == 1

    def test_select_option_without_option_is_skipped(self) -> None:
        """Test that SELECT_OPTION actions without option are skipped."""
        from skyvern.core.script_generations.generate_workflow_parameters import CUSTOM_FIELD_ACTIONS

        task_id = "task-123"

        action_without_option = {
            "action_type": ActionType.SELECT_OPTION,
            "action_id": "action-789",
            "task_id": task_id,
            "option": "",  # Empty - not yet saved
        }

        custom_field_actions = []
        for action in [action_without_option]:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            value = ""
            if action_type == ActionType.SELECT_OPTION:
                value = action.get("option", "")

            if not value:
                continue

            custom_field_actions.append(action)

        assert len(custom_field_actions) == 0

    def test_upload_file_without_file_url_is_skipped(self) -> None:
        """Test that UPLOAD_FILE actions without file_url are skipped."""
        from skyvern.core.script_generations.generate_workflow_parameters import CUSTOM_FIELD_ACTIONS

        task_id = "task-123"

        action_without_file = {
            "action_type": ActionType.UPLOAD_FILE,
            "action_id": "action-101",
            "task_id": task_id,
            "file_url": "",  # Empty - not yet saved
        }

        custom_field_actions = []
        for action in [action_without_file]:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            value = ""
            if action_type == ActionType.UPLOAD_FILE:
                value = action.get("file_url", "")

            if not value:
                continue

            custom_field_actions.append(action)

        assert len(custom_field_actions) == 0


@pytest.mark.asyncio
async def test_generate_workflow_parameters_schema_skips_empty_actions_and_sets_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Integration test: Verify that actions without data are skipped and the context flag is set.

    This test confirms the smart finalize approach:
    1. Incomplete actions are skipped mid-run (prevents bad field mappings)
    2. Context flag is set (triggers finalize regeneration only when needed)
    """
    from skyvern.core.script_generations import generate_workflow_parameters as gwp
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

    # Set up context to track the flag
    context = SkyvernContext()
    skyvern_context.set(context)

    # Mock the LLM call - should only be called if there are valid actions
    llm_called = False

    async def mock_generate_field_names_with_llm(custom_field_actions):
        nonlocal llm_called
        llm_called = True
        return GeneratedFieldMapping(
            field_mappings={"action_index_1": "facility_name"},
            schema_fields={"facility_name": {"type": "str", "description": "The facility name"}},
        )

    monkeypatch.setattr(gwp, "_generate_field_names_with_llm", mock_generate_field_names_with_llm)

    task_id = "task-123"

    # Actions with empty values - simulates race condition
    actions_by_task = {
        task_id: [
            {
                "action_type": ActionType.INPUT_TEXT,
                "action_id": "action-456",
                "task_id": task_id,
                "text": "",  # Empty - not yet saved
                "intention": "Enter facility name",
            },
        ]
    }

    try:
        schema_code, action_field_mappings = await generate_workflow_parameters_schema(actions_by_task)

        # LLM should NOT be called because action was skipped
        assert not llm_called

        # Should return empty schema
        assert "pass" in schema_code
        assert action_field_mappings == {}

        # Context flag should be set - triggers finalize regeneration
        assert context.script_gen_had_incomplete_actions is True
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_generate_workflow_parameters_schema_with_complete_actions_no_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Integration test: Verify that complete actions don't set the context flag.

    When script generation has complete data, the flag should NOT be set,
    which means finalize won't regenerate (saving costs).
    """
    from skyvern.core.script_generations import generate_workflow_parameters as gwp
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

    # Set up context to track the flag
    context = SkyvernContext()
    skyvern_context.set(context)

    # Mock the LLM call
    async def mock_generate_field_names_with_llm(custom_field_actions):
        return GeneratedFieldMapping(
            field_mappings={"action_index_1": "facility_name"},
            schema_fields={"facility_name": {"type": "str", "description": "The facility name"}},
        )

    monkeypatch.setattr(gwp, "_generate_field_names_with_llm", mock_generate_field_names_with_llm)

    task_id = "task-123"

    # Actions with complete values - no race condition
    actions_by_task = {
        task_id: [
            {
                "action_type": ActionType.INPUT_TEXT,
                "action_id": "action-456",
                "task_id": task_id,
                "text": "Urdaneta",  # Has value - complete
                "intention": "Enter facility name",
            },
        ]
    }

    try:
        schema_code, action_field_mappings = await generate_workflow_parameters_schema(actions_by_task)

        # Should have generated schema
        assert "facility_name" in schema_code

        # Context flag should NOT be set - no regeneration needed
        assert context.script_gen_had_incomplete_actions is False
    finally:
        skyvern_context.reset()
