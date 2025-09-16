from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.schemas.runs import CUA_ENGINES, CUA_RUN_TYPES


async def is_cua_task(
    *,
    task: Task,
) -> bool:
    """Return True if the run, engine, or task indicates a CUA task."""

    if task.workflow_run_id:
        # it's a task based block, should look up the block run to see if it's a CUA task
        block = await app.DATABASE.get_workflow_run_block_by_task_id(
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        if block.engine is not None and block.engine in CUA_ENGINES:
            return True

    run = await app.DATABASE.get_run(
        run_id=task.task_id,
        organization_id=task.organization_id,
    )
    if run and run.task_run_type in CUA_RUN_TYPES:
        return True

    return False
