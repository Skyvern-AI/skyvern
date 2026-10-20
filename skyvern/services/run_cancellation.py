from typing import NamedTuple

import structlog

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import WorkflowRunStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskStatus

LOG = structlog.get_logger()


class RunCancellation(NamedTuple):
    task_status: TaskStatus
    # A canceled parent run owns its own teardown, so the two sources are not interchangeable.
    from_workflow_run: bool


async def read_run_cancellation(task: Task) -> RunCancellation | None:
    """The terminal status a mid-flight task has to take, or None while its run is still active.

    Each read fails open on its own: a DB blip must not stop a healthy run, nor hide a cancel the other row shows.
    """
    if task.workflow_run_id:
        try:
            workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(
                workflow_run_id=task.workflow_run_id,
                organization_id=task.organization_id,
            )
        except Exception:
            LOG.warning("Cancellation poll failed to read the workflow run", task_id=task.task_id, exc_info=True)
            workflow_run = None
        if workflow_run and workflow_run.status == WorkflowRunStatus.canceled:
            return RunCancellation(TaskStatus.canceled, from_workflow_run=True)
        if workflow_run and workflow_run.status == WorkflowRunStatus.timed_out:
            return RunCancellation(TaskStatus.timed_out, from_workflow_run=True)

    try:
        refreshed_task = await app.DATABASE.tasks.get_task(task_id=task.task_id, organization_id=task.organization_id)
    except Exception:
        LOG.warning("Cancellation poll failed to read the task", task_id=task.task_id, exc_info=True)
        return None
    if refreshed_task and refreshed_task.status in (TaskStatus.canceled, TaskStatus.timed_out):
        return RunCancellation(refreshed_task.status, from_workflow_run=False)
    return None
