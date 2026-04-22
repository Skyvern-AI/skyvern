"""
Module for generating GeneratedWorkflowParameters schema from workflow run
input_text actions.

SKY-8965 Phase 2: the LLM call (`_generate_field_names_with_llm`) has been
replaced by a deterministic 3-rule picker. See
`deterministic_field_naming.py` for the picker and
`parameter_reference_guard.py` for the post-generation validation guard.
"""

from typing import Any, Dict, List, Tuple

import structlog

from skyvern.core.script_generations.deterministic_field_naming import (
    pick_field_names_for_actions,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.actions.actions import ActionType

LOG = structlog.get_logger(__name__)

CUSTOM_FIELD_ACTIONS = [ActionType.INPUT_TEXT, ActionType.UPLOAD_FILE, ActionType.SELECT_OPTION]


# async retained for caller-signature compat; no I/O inside after Phase 2
# removed the LLM call.
async def generate_workflow_parameters_schema(
    actions_by_task: Dict[str, List[Dict[str, Any]]],
    existing_field_assignments: Dict[int, str] | None = None,
    *,
    declared_param_keys: frozenset[str] = frozenset(),
    upstream_schema_keys: frozenset[str] = frozenset(),
    goal_template_by_task: Dict[str, str] | None = None,
) -> Tuple[str, Dict[str, str]]:
    """
    Generate a GeneratedWorkflowParameters Pydantic schema based on input_text actions.

    SKY-8965 Phase 2: field names are picked deterministically (no LLM call).
    The three-rule picker in ``deterministic_field_naming.py`` decides each
    name based on: (1) jinja refs in the goal, (2) upstream schema keys,
    (3) sanitized action intention.

    New keyword-only args (optional for backwards compat — callers that don't
    pass them get empty frozensets, which makes the picker fall through to
    Rule 3 for every action):
        declared_param_keys: Workflow-declared parameter keys.
        upstream_schema_keys: Keys from upstream blocks' extraction schemas.
        goal_template_by_task: Unrendered navigation_goal per task_id.

    Returns:
        Tuple of (schema_code, field_mappings) where:
        - schema_code: Python code for the GeneratedWorkflowParameters class
        - field_mappings: Dictionary mapping ``"{task_id}:{action_id}"`` to field
          names, used by ``hydrate_input_text_actions_with_field_names``
    """
    goal_template_by_task = goal_template_by_task or {}

    # --- mark incomplete actions (SKY-7653 race-condition mitigation) ----
    _mark_incomplete_actions(actions_by_task)

    # --- deterministic pick (replaces the LLM call) ---------------------
    picks = pick_field_names_for_actions(
        actions_by_task=actions_by_task,
        goal_template_by_task=goal_template_by_task,
        declared_param_keys=declared_param_keys,
        upstream_schema_keys=upstream_schema_keys,
        existing_field_assignments=existing_field_assignments,
    )

    if not picks:
        LOG.info("No custom-field actions found — empty schema")
        return _generate_empty_schema(), {}

    # Build schema and field mappings from the picks
    schema_fields: Dict[str, Dict[str, str]] = {}
    action_field_mappings: Dict[str, str] = {}

    for action_key, pick in picks.items():
        action_field_mappings[action_key] = pick.field_name
        if pick.field_name not in schema_fields:
            schema_fields[pick.field_name] = {
                "type": "str",
                "description": pick.description or f"Value for {pick.field_name}",
            }

    LOG.info(
        "deterministic_field_naming_complete",
        total_picks=len(picks),
        rules_used=sorted({pick.rule for pick in picks.values()}),
        field_names=sorted(schema_fields.keys()),
    )

    schema_code = _generate_pydantic_schema(schema_fields)
    return schema_code, action_field_mappings


def _mark_incomplete_actions(actions_by_task: Dict[str, List[Dict[str, Any]]]) -> None:
    """Flag the skyvern context if any custom-field actions lack data (SKY-7653).

    This preserves the race-condition mitigation from the original LLM path:
    when script generation runs before action data is fully saved to the DB,
    the ``script_gen_had_incomplete_actions`` flag triggers a finalize
    regeneration.
    """
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
            if not value:
                ctx = skyvern_context.current()
                if ctx:
                    ctx.script_gen_had_incomplete_actions = True
                return  # one flag is enough


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
