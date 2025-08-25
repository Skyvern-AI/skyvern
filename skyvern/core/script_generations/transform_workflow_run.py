from dataclasses import dataclass
from typing import Any

import structlog

from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.forge import app
from skyvern.schemas.workflows import BlockType
from skyvern.services import workflow_service

LOG = structlog.get_logger(__name__)


@dataclass
class CodeGenInput:
    file_name: str
    workflow_run: dict[str, Any]
    workflow: dict[str, Any]
    workflow_blocks: list[dict[str, Any]]
    actions_by_task: dict[str, list[dict[str, Any]]]


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
    definition_blocks_by_label = {block.label: block for block in workflow_definition_blocks if block.label}

    workflow_block_dump = []
    actions_by_task = {}

    # Loop through workflow run blocks and match to original definition blocks by label
    for run_block in workflow_run_blocks:
        if run_block.block_type == BlockType.TaskV2:
            raise ValueError("TaskV2 blocks are not supported yet")

        # Find corresponding definition block by label to get templated information
        definition_block = definition_blocks_by_label.get(run_block.label) if run_block.label else None

        if definition_block:
            # Start with the original templated definition block
            final_dump = definition_block.model_dump()
        else:
            # Fallback to run block data if no matching definition block found
            final_dump = run_block.model_dump()
            LOG.warning(f"No matching definition block found for run block with label: {run_block.label}")

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
                    action_dumps.append(action_dump)
                actions_by_task[run_block.task_id] = action_dumps
            else:
                LOG.warning(f"Task {run_block.task_id} not found")

        workflow_block_dump.append(final_dump)

    return CodeGenInput(
        file_name=f"{workflow_run_id}.py",
        workflow_run=workflow_run_request_json,
        workflow=workflow_json,
        workflow_blocks=workflow_block_dump,
        actions_by_task=actions_by_task,
    )
