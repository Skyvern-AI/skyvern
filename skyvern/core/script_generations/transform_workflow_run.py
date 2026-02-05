from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import structlog

from skyvern.core.script_generations.constants import SCRIPT_TASK_BLOCKS
from skyvern.forge import app
from skyvern.schemas.workflows import BlockType
from skyvern.services import workflow_service
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action

LOG = structlog.get_logger(__name__)


@dataclass
class CodeGenInput:
    file_name: str
    workflow_run: dict[str, Any]
    workflow: dict[str, Any]
    workflow_blocks: list[dict[str, Any]]
    actions_by_task: dict[str, list[dict[str, Any]]]
    task_v2_child_blocks: dict[str, list[dict[str, Any]]]  # task_v2_label -> list of child blocks


def _process_action_for_block(
    action: Action,
    block_dump: dict[str, Any],
) -> dict[str, Any]:
    """Process a single action and add block-specific context like data extraction goal."""
    action_dump = action.model_dump()
    action_dump["xpath"] = action.get_xpath()
    action_dump["has_mini_agent"] = action.has_mini_agent
    if (
        "data_extraction_goal" in block_dump
        and block_dump["data_extraction_goal"]
        and action.action_type == ActionType.EXTRACT
    ):
        action_dump["data_extraction_goal"] = block_dump["data_extraction_goal"]
    if (
        "extracted_information_schema" in block_dump
        and block_dump["extracted_information_schema"]
        and action.action_type == ActionType.EXTRACT
    ):
        action_dump["data_extraction_schema"] = block_dump["extracted_information_schema"]
    return action_dump


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

    # Batch fetch all tasks and actions upfront to avoid N+1 queries
    # First pass: collect all task_ids from workflow run blocks
    all_task_ids: set[str] = set()
    for rb in workflow_run_blocks:
        if rb.block_type in SCRIPT_TASK_BLOCKS and rb.task_id:
            all_task_ids.add(rb.task_id)

    # Batch fetch all tasks and actions in 2 queries instead of N+1
    tasks_by_id: dict[str, Any] = {}
    actions_by_task_id: dict[str, list[Action]] = defaultdict(list)

    if all_task_ids:
        task_ids_list = list(all_task_ids)
        # Single query for all tasks
        tasks = await app.DATABASE.get_tasks_by_ids(task_ids=task_ids_list, organization_id=organization_id)
        tasks_by_id = {task.task_id: task for task in tasks}
        LOG.debug(
            "Batch fetched tasks for code gen",
            workflow_run_id=workflow_run_id,
            task_count=len(tasks),
        )

        # Single query for all actions
        all_actions = await app.DATABASE.get_tasks_actions(task_ids=task_ids_list, organization_id=organization_id)
        for action in all_actions:
            if action.task_id:
                actions_by_task_id[action.task_id].append(action)
        LOG.debug(
            "Batch fetched actions for code gen",
            workflow_run_id=workflow_run_id,
            action_count=len(all_actions),
        )

    workflow_block_dump = []
    actions_by_task: dict[str, list[dict[str, Any]]] = {}
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
            # Use pre-fetched task data (batch fetched)
            task = tasks_by_id.get(run_block.task_id)
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

                # Use pre-fetched actions (batch fetched)
                actions = actions_by_task_id.get(run_block.task_id, [])
                action_dumps = [_process_action_for_block(action, final_dump) for action in actions]
                actions_by_task[run_block.task_id] = action_dumps
            else:
                LOG.warning("Task not found", task_id=run_block.task_id)

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
                LOG.warning(
                    "Task v2 block does not have a child workflow run id",
                    task_v2_label=run_block.label,
                )

        if run_block.block_type == BlockType.FOR_LOOP:
            # Process ForLoop child blocks to get actions for task blocks inside the loop
            # Child blocks have parent_workflow_run_block_id pointing to the ForLoop's workflow_run_block_id
            child_run_blocks = [
                b for b in workflow_run_blocks if b.parent_workflow_run_block_id == run_block.workflow_run_block_id
            ]
            # Create mapping of child run blocks by label
            child_run_blocks_by_label = {b.label: b for b in child_run_blocks if b.label}

            # Warn about any unlabeled child blocks that won't be matched
            unlabeled_children = [b for b in child_run_blocks if not b.label]
            if unlabeled_children:
                LOG.warning(
                    "ForLoop has child blocks without labels - these will not be matched to loop_blocks definitions",
                    forloop_label=run_block.label,
                    unlabeled_count=len(unlabeled_children),
                )

            # Get loop_blocks from the definition block
            loop_blocks = final_dump.get("loop_blocks", [])

            if loop_blocks and not child_run_blocks:
                LOG.warning(
                    "ForLoop block has loop_blocks definitions but no child run blocks found",
                    forloop_label=run_block.label,
                    workflow_run_block_id=run_block.workflow_run_block_id,
                    loop_blocks_count=len(loop_blocks),
                )
            updated_loop_blocks = []

            for loop_block_def in loop_blocks:
                loop_block_dump = loop_block_def.copy() if isinstance(loop_block_def, dict) else loop_block_def
                loop_block_label = loop_block_dump.get("label")

                # Find matching child run block
                child_run_block = child_run_blocks_by_label.get(loop_block_label) if loop_block_label else None

                if child_run_block and child_run_block.block_type in SCRIPT_TASK_BLOCKS and child_run_block.task_id:
                    # Use pre-fetched task data (batch fetched)
                    task = tasks_by_id.get(child_run_block.task_id)
                    if task:
                        task_dump = task.model_dump()
                        loop_block_dump.update({k: v for k, v in task_dump.items() if k not in loop_block_dump})
                        loop_block_dump.update(
                            {
                                "task_id": child_run_block.task_id,
                                "status": child_run_block.status,
                                "output": child_run_block.output,
                            }
                        )

                        # Use pre-fetched actions (batch fetched)
                        actions = actions_by_task_id.get(child_run_block.task_id, [])
                        action_dumps = [_process_action_for_block(action, loop_block_dump) for action in actions]
                        actions_by_task[child_run_block.task_id] = action_dumps
                    else:
                        LOG.warning(
                            "Task not found for ForLoop child block",
                            task_id=child_run_block.task_id,
                            forloop_label=run_block.label,
                        )

                updated_loop_blocks.append(loop_block_dump)

            # Update final_dump with the processed loop_blocks
            final_dump["loop_blocks"] = updated_loop_blocks

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
