"""Regression coverage for the workflow yaml-import path silently dropping `engine` and
`max_steps_per_run` on validation/action blocks: WorkflowCreateYAMLRequest.model_validate
lacked the fields entirely, so a customer's yaml keys never reached the domain block.

Drives the real path end to end: a raw dict (as a customer's yaml/json body would look) through
WorkflowCreateYAMLRequest.model_validate, then through convert_workflow_definition, asserting on
the resulting domain blocks rather than the intermediate yaml model.
"""

from __future__ import annotations

from skyvern.forge.sdk.workflow.models.block import ActionBlock, Block, ValidationBlock
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.runs import RunEngine
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest


def _blocks_by_label(*, validation_block: dict[str, object], action_block: dict[str, object]) -> dict[str, Block]:
    request = WorkflowCreateYAMLRequest.model_validate(
        {
            "title": "t",
            "workflow_definition": {
                "parameters": [],
                "blocks": [validation_block, action_block],
            },
        }
    )
    converted = convert_workflow_definition(
        workflow_definition_yaml=request.workflow_definition,
        workflow_id="wid_test",
    )
    return {block.label: block for block in converted.blocks}


def test_yaml_import_threads_engine_and_max_steps_per_run() -> None:
    """SILENT-DROP REGRESSION. Values are chosen distinct from every legacy hardcoded default
    (engine skyvern-1.0, max_steps_per_run 2/1) so this only passes if the yaml keys genuinely
    reach the domain blocks, not by coincidence."""
    blocks = _blocks_by_label(
        validation_block={
            "block_type": "validation",
            "label": "v",
            "complete_criterion": "billing date within range",
            "engine": "skyvern-3.0",
            "max_steps_per_run": 6,
        },
        action_block={
            "block_type": "action",
            "label": "a",
            "navigation_goal": "click submit",
            "max_steps_per_run": 4,
        },
    )

    validation_block = blocks["v"]
    assert isinstance(validation_block, ValidationBlock)
    assert validation_block.engine == RunEngine.skyvern_v3
    assert validation_block.max_steps_per_run == 6

    action_block = blocks["a"]
    assert isinstance(action_block, ActionBlock)
    assert action_block.max_steps_per_run == 4


def test_yaml_import_defaults_when_new_keys_omitted() -> None:
    """ZERO-BEHAVIOR-CHANGE. Today's defaults come from the converter's historical hardcoded
    fallback (2 for validation, 1 for action), not from the yaml field's own None default."""
    blocks = _blocks_by_label(
        validation_block={
            "block_type": "validation",
            "label": "v",
            "complete_criterion": "billing date within range",
        },
        action_block={
            "block_type": "action",
            "label": "a",
            "navigation_goal": "click submit",
        },
    )

    validation_block = blocks["v"]
    assert isinstance(validation_block, ValidationBlock)
    assert validation_block.engine == RunEngine.skyvern_v1
    assert validation_block.max_steps_per_run == 2

    action_block = blocks["a"]
    assert isinstance(action_block, ActionBlock)
    assert action_block.max_steps_per_run == 1
