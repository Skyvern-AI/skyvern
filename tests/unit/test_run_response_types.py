from datetime import UTC, datetime

from pydantic import TypeAdapter

from skyvern.schemas.runs import RunResponse, RunStatus, RunType, TaskRunResponse, WorkflowRunResponse


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
