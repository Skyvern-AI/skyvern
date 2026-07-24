"""Maps parent WorkflowRunStatus to child entity terminal statuses."""

from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.workflows import BlockStatus

BLOCK_STATUS_MAP: dict[WorkflowRunStatus, BlockStatus] = {
    WorkflowRunStatus.timed_out: BlockStatus.timed_out,
    WorkflowRunStatus.failed: BlockStatus.failed,
}

TASK_STATUS_MAP: dict[WorkflowRunStatus, TaskStatus] = {
    WorkflowRunStatus.timed_out: TaskStatus.timed_out,
    WorkflowRunStatus.failed: TaskStatus.failed,
}

# StepStatus has no timed_out; use canceled for timeout cleanup.
STEP_STATUS_MAP: dict[WorkflowRunStatus, StepStatus] = {
    WorkflowRunStatus.timed_out: StepStatus.canceled,
    WorkflowRunStatus.failed: StepStatus.failed,
}

_TERMINAL_BLOCK_STATUSES = {
    # Keep this set in sync if BlockStatus gains additional terminal values.
    BlockStatus.completed,
    BlockStatus.failed,
    BlockStatus.terminated,
    BlockStatus.canceled,
    BlockStatus.timed_out,
    BlockStatus.skipped,
}
NONFINAL_BLOCK_STATUSES: list[str] = [s.value for s in BlockStatus if s not in _TERMINAL_BLOCK_STATUSES]
