from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.services import workflow_service

LOG = structlog.get_logger(__name__)


@dataclass
class CodeGenInput:
    file_name: str
    workflow_run: Mapping[str, Any]
    workflow: Mapping[str, Any]
    workflow_blocks: Iterable[Mapping[str, Any]]
    actions_by_task: Mapping[str, Iterable[Mapping[str, Any]]]


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

    # get the tasks
    ## first, get all the workflow run blocks
    workflow_run_blocks = await app.DATABASE.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id, organization_id=organization_id
    )
    workflow_run_blocks.sort(key=lambda x: x.created_at)
    workflow_block_dump = []
    # Hydrate blocks with task data
    # TODO: support task v2
    actions_by_task = {}
    for block in workflow_run_blocks:
        block_dump = block.model_dump()
        if block.block_type == BlockType.TaskV2:
            raise ValueError("TaskV2 blocks are not supported yet")
        if (
            block.block_type
            in [BlockType.TASK, BlockType.ACTION, BlockType.EXTRACTION, BlockType.LOGIN, BlockType.NAVIGATION]
            and block.task_id
        ):
            task = await app.DATABASE.get_task(task_id=block.task_id, organization_id=organization_id)
            if not task:
                LOG.warning(f"Task {block.task_id} not found")
                continue
            block_dump.update(task.model_dump())
            actions = await app.DATABASE.get_task_actions(task_id=block.task_id, organization_id=organization_id)
            action_dumps = []
            for action in actions:
                action_dump = action.model_dump()
                action_dump["xpath"] = action.get_xpath()
                action_dumps.append(action_dump)
            actions_by_task[block.task_id] = action_dumps
        workflow_block_dump.append(block_dump)

    return CodeGenInput(
        file_name=f"{workflow_run_id}.py",
        workflow_run=workflow_run_request_json,
        workflow=workflow_json,
        workflow_blocks=workflow_block_dump,
        actions_by_task=actions_by_task,
    )
