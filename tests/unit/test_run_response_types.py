from datetime import UTC, datetime

from pydantic import TypeAdapter

from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.schemas.runs import RunResponse, RunStatus, RunType, TaskRunResponse, WorkflowRunResponse


def test_every_workflow_run_status_maps_to_run_status() -> None:
    # A workflow run's status is projected onto the public RunStatus via
    # RunStatus(workflow_run.status); a WorkflowRunStatus value with no RunStatus
    # member raises ValueError on that projection (paused did, in production).
    for status in WorkflowRunStatus:
        assert RunStatus(status.value).value == status.value


def test_paused_run_status_is_non_final() -> None:
    assert RunStatus("paused") is RunStatus.paused
    assert RunStatus.paused.is_final() is False


def test_task_run_response_preserves_run_type_enum() -> None:
    response = TaskRunResponse.model_validate(
        {
            "run_id": "tr_123",
            "run_type": "task_v2",
            "status": "completed",
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
        }
    )

    assert response.run_type is RunType.task_v2
    assert response.run_type.value == "task_v2"
    assert response.status is RunStatus.completed


def test_workflow_run_response_preserves_run_type_enum() -> None:
    response = WorkflowRunResponse.model_validate(
        {
            "run_id": "wr_123",
            "run_type": "workflow_run",
            "status": "completed",
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
        }
    )

    assert response.run_type is RunType.workflow_run
    assert response.run_type.value == "workflow_run"


def test_run_response_discriminator_preserves_run_type_enum() -> None:
    response = TypeAdapter(RunResponse).validate_python(
        {
            "run_id": "tr_123",
            "run_type": "task_v2",
            "status": "completed",
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
        }
    )

    assert isinstance(response, TaskRunResponse)
    assert response.run_type is RunType.task_v2
