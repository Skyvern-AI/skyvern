"""
Module for generating GeneratedWorkflowParameters schema from workflow run input_text actions.
"""

from typing import Any, Dict, List, Tuple

import structlog
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.sdk.prompting import PromptEngine
from skyvern.webeye.actions.actions import ActionType

LOG = structlog.get_logger(__name__)

# Initialize prompt engine
prompt_engine = PromptEngine("skyvern")
CUSTOM_FIELD_ACTIONS = [ActionType.INPUT_TEXT, ActionType.UPLOAD_FILE, ActionType.SELECT_OPTION]


class GeneratedFieldMapping(BaseModel):
    """Mapping of action indices to field names."""

    field_mappings: Dict[str, str]
    schema_fields: Dict[str, Dict[str, str]]


async def generate_workflow_parameters_schema(
    actions_by_task: Dict[str, List[Dict[str, Any]]],
) -> Tuple[str, Dict[str, str]]:
    """
    Generate a GeneratedWorkflowParameters Pydantic schema based on input_text actions.

    Args:
        actions_by_task: Dictionary mapping task IDs to lists of action dictionaries

    Returns:
        Tuple of (schema_code, field_mappings) where:
        - schema_code: Python code for the GeneratedWorkflowParameters class
        - field_mappings: Dictionary mapping action indices to field names for hydration
    """
    # Extract all input_text actions
    custom_field_actions = []
    action_index_map = {}
    action_counter = 1

    for task_id, actions in actions_by_task.items():
        for action in actions:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            value = ""
            if action_type == ActionType.INPUT_TEXT:
                value = action.get("text", "")
            elif action_type == ActionType.UPLOAD_FILE:
                value = action.get("file_url", "")
            elif action_type == ActionType.SELECT_OPTION:
                value = action.get("option", "")
            custom_field_actions.append(
                {
                    "action_type": action_type,
                    "value": value,
                    "intention": action.get("intention", ""),
                    "task_id": task_id,
                    "action_id": action.get("action_id", ""),
                }
            )
            action_index_map[f"action_index_{action_counter}"] = {
                "task_id": task_id,
                "action_id": action.get("action_id", ""),
            }
            action_counter += 1

    if not custom_field_actions:
        LOG.warning("No field_name_actions found in workflow run")
        return _generate_empty_schema(), {}

    # Generate field names using LLM
    try:
        field_mapping = await _generate_field_names_with_llm(custom_field_actions)

        # Generate the Pydantic schema code
        schema_code = _generate_pydantic_schema(field_mapping.schema_fields)

        # Create field mappings for action hydration
        action_field_mappings = {}
        for action_idx, field_name in field_mapping.field_mappings.items():
            if action_idx in action_index_map:
                action_info = action_index_map[action_idx]
                key = f"{action_info['task_id']}:{action_info['action_id']}"
                action_field_mappings[key] = field_name

        return schema_code, action_field_mappings

    except Exception as e:
        LOG.error("Failed to generate workflow parameters schema", error=str(e), exc_info=True)
        return _generate_empty_schema(), {}


async def _generate_field_names_with_llm(custom_field_actions: List[Dict[str, Any]]) -> GeneratedFieldMapping:
    """
    Use LLM to generate field names from input actions.

    Args:
        input_actions: List of input_text action dictionaries

    Returns:
        GeneratedFieldMapping with field mappings and schema definitions
    """
    prompt = prompt_engine.load_prompt(
        template="generate-workflow-parameters", custom_field_actions=custom_field_actions
    )

    response = await app.SCRIPT_GENERATION_LLM_API_HANDLER(prompt=prompt, prompt_name="generate-workflow-parameters")

    return GeneratedFieldMapping.model_validate(response)


def _generate_pydantic_schema(schema_fields: Dict[str, Dict[str, str]]) -> str:
    """
    Generate Pydantic schema code from field definitions.

    Args:
        schema_fields: Dictionary of field names to their type and description

    Returns:
        Python code string for the GeneratedWorkflowParameters class
    """
    if not schema_fields:
        return _generate_empty_schema()

    lines = [
        "class GeneratedWorkflowParameters(BaseModel):",
        '    """Generated schema representing all input_text action values from the workflow run."""',
        "",
    ]

    for field_name, field_info in schema_fields.items():
        field_type = field_info.get("type", "str")
        description = field_info.get("description", f"Value for {field_name}")

        # Escape quotes in description
        description = description.replace('"', '\\"')

        lines.append(f'    {field_name}: {field_type} = Field(description="{description}", default="")')

    return "\n".join(lines)


def _generate_empty_schema() -> str:
    """Generate an empty schema when no input_text actions are found."""
    return '''from pydantic import BaseModel


class GeneratedWorkflowParameters(BaseModel):
    """Generated schema representing all input_text action values from the workflow run."""
    pass
'''


def hydrate_input_text_actions_with_field_names(
    actions_by_task: Dict[str, List[Dict[str, Any]]], field_mappings: Dict[str, str]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Add field_name to input_text actions based on generated mappings.

    Args:
        actions_by_task: Dictionary mapping task IDs to lists of action dictionaries
        field_mappings: Dictionary mapping "task_id:action_id" to field names

    Returns:
        Updated actions_by_task with field_name added to input_text actions
    """
    updated_actions_by_task = {}

    for task_id, actions in actions_by_task.items():
        updated_actions = []

        for action in actions:
            action_copy = action.copy()

            if action.get("action_type") in CUSTOM_FIELD_ACTIONS:
                action_id = action.get("action_id", "")
                mapping_key = f"{task_id}:{action_id}"

                if mapping_key in field_mappings:
                    action_copy["field_name"] = field_mappings[mapping_key]
                # else:
                #     # Fallback field name if mapping not found
                #     intention = action.get("intention", "")
                #     if intention:
                #         # Simple field name generation from intention
                #         field_name = intention.lower().replace(" ", "_").replace("?", "").replace("'", "")
                #         field_name = "".join(c for c in field_name if c.isalnum() or c == "_")
                #         action_copy["field_name"] = field_name or "unknown_field"
                #     else:
                #         action_copy["field_name"] = "unknown_field"

            updated_actions.append(action_copy)

        updated_actions_by_task[task_id] = updated_actions

    return updated_actions_by_task
