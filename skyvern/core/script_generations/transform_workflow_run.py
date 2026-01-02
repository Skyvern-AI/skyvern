from dataclasses import dataclass
from typing import Any

import structlog

from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.forge import app
from skyvern.schemas.workflows import BlockType
from skyvern.services import workflow_service
from skyvern.webeye.actions.action_types import ActionType

LOG = structlog.get_logger(__name__)


@dataclass
class CodeGenInput:
    file_name: str
    workflow_run: dict[str, Any]
    workflow: dict[str, Any]
    workflow_blocks: list[dict[str, Any]]
    actions_by_task: dict[str, list[dict[str, Any]]]
    task_v2_child_blocks: dict[str, list[dict[str, Any]]]  # task_v2_label -> list of child blocks


async def transform_workflow_run_to_code_gen_input(workflow_run_id: str, organization_id: str) -> CodeGenInput:
    # get the workflow run request
    workflow_run_resp = await workflow_service.get_workflow_run_response(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    if not workflow_run_resp:
        raise ValueError(f"Workflow run {workflow_run_id} not found")
    run_request = workflow_run_resp.run_request
    if not run_request:
        raise ValueError(f"Workflow run {workflow_run_id} has no run request")
    workflow_run_request_json = run_request.model_dump()

    # get the workflow
    workflow = await app.WORKFLOW_SERVICE.get_workflow_by_permanent_id(
        workflow_permanent_id=run_request.workflow_id, organization_id=organization_id
    )
    if not workflow:
        raise ValueError(f"Workflow {run_request.workflow_id} not found")
    workflow_json = workflow.model_dump()

    # get the original workflow definition blocks (with templated information)
    workflow_definition_blocks = workflow.workflow_definition.blocks

    # get workflow run blocks for task execution data
    workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    workflow_run_blocks.sort(key=lambda x: x.created_at)

    # Create mapping from definition blocks by label for quick lookup
    workflow_run_blocks_by_label = {block.label: block for block in workflow_run_blocks if block.label}

    workflow_block_dump = []
    actions_by_task = {}
    task_v2_child_blocks = {}

    # Loop through workflow run blocks and match to original definition blocks by label
    for definition_block in workflow_definition_blocks:
        # if definition_block.block_type == BlockType.TaskV2:
        #     raise ValueError("TaskV2 blocks are not supported yet")

        run_block = workflow_run_blocks_by_label.get(definition_block.label) if definition_block.label else None

        final_dump = {}
        if run_block:
            # Start with the original templated definition block
            final_dump = definition_block.model_dump()
        else:
            # the run_block is not a top level block - for now we will skip non top level blocks, like any blocks defined inside a loop block
            continue

        # For task blocks, add execution data while preserving templated information
        if run_block.block_type in SCRIPT_TASK_BLOCKS and run_block.task_id:
            task = await app.DATABASE.get_task(task_id=run_block.task_id, organization_id=organization_id)
            if task:
                # Add task execution data but preserve original templated fields
                task_dump = task.model_dump()
                # Update with execution data, but keep templated values from definition
                if definition_block:
                    final_dump.update({k: v for k, v in task_dump.items() if k not in final_dump})
                else:
                    final_dump.update(task_dump)

                # Add run block execution metadata
                final_dump.update(
                    {
                        "task_id": run_block.task_id,
                        "status": run_block.status,
                        "output": run_block.output,
                    }
                )

                # Get task actions
                actions = await app.DATABASE.get_task_actions_hydrated(
                    task_id=run_block.task_id, organization_id=organization_id
                )
                action_dumps = []
                for action in actions:
                    action_dump = action.model_dump()
                    action_dump["xpath"] = action.get_xpath()
                    action_dump["has_mini_agent"] = action.has_mini_agent
                    if (
                        "data_extraction_goal" in final_dump
                        and final_dump["data_extraction_goal"]
                        and action.action_type == ActionType.EXTRACT
                    ):
                        # use the right data extraction goal for the extract action
                        action_dump["data_extraction_goal"] = final_dump["data_extraction_goal"]
                    if (
                        "extracted_information_schema" in final_dump
                        and final_dump["extracted_information_schema"]
                        and action.action_type == ActionType.EXTRACT
                    ):
                        action_dump["data_extraction_schema"] = final_dump["extracted_information_schema"]
                    action_dumps.append(action_dump)
                actions_by_task[run_block.task_id] = action_dumps
            else:
                LOG.warning(f"Task {run_block.task_id} not found")

        if run_block.block_type == BlockType.TaskV2:
            # Merge child workflow run data for task v2 blocks
            if run_block.block_workflow_run_id:
                try:
                    # Recursively get child workflow run data
                    child_code_gen_input = await transform_workflow_run_to_code_gen_input(
                        workflow_run_id=run_block.block_workflow_run_id, organization_id=organization_id
                    )

                    # Store child blocks for this task_v2 block
                    task_v2_label = run_block.label or f"task_v2_{run_block.workflow_run_block_id}"
                    task_v2_child_blocks[task_v2_label] = child_code_gen_input.workflow_blocks

                    # Merge child workflow blocks into the current workflow_block_dump (but mark them as child blocks)
                    # for child_block in child_code_gen_input.workflow_blocks:
                    #     child_block["parent_task_v2_label"] = task_v2_label
                    #     workflow_block_dump.append(child_block)

                    # Merge child actions_by_task into current actions_by_task
                    for task_id, child_actions in child_code_gen_input.actions_by_task.items():
                        actions_by_task[task_id] = child_actions

                    # Also merge nested task_v2 child blocks if any
                    for nested_label, nested_blocks in child_code_gen_input.task_v2_child_blocks.items():
                        task_v2_child_blocks[nested_label] = nested_blocks

                except Exception as e:
                    LOG.warning(
                        "Failed to merge child workflow run data for task v2 block",
                        task_v2_workflow_run_id=run_block.block_workflow_run_id,
                        error=str(e),
                    )
            else:
                LOG.warning(f"Task v2 block {run_block.label} does not have a child workflow run id")

        final_dump["workflow_run_id"] = workflow_run_id
        if run_block:
            final_dump["workflow_run_block_id"] = run_block.workflow_run_block_id
        else:
            final_dump["workflow_run_block_id"] = None
        workflow_block_dump.append(final_dump)

    return CodeGenInput(
        file_name=f"{workflow_run_id}.py",
        workflow_run=workflow_run_request_json,
        workflow=workflow_json,
        workflow_blocks=workflow_block_dump,
        actions_by_task=actions_by_task,
        task_v2_child_blocks=task_v2_child_blocks,
    )
