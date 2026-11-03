from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import TypeAdapter

from skyvern.client import AsyncSkyvern, GetRunResponse
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.library import skyvern_browser_page_agent
from skyvern.library.skyvern_browser_page_agent import SkyvernBrowserPageAgent
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
    response: TaskRunResponse | WorkflowRunResponse = TypeAdapter(RunResponse).validate_python(
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


def _run_payload(run_type: RunType, status: RunStatus) -> dict[str, Any]:
    return (
        TypeAdapter(RunResponse)
        .validate_python(
            {
                "run_id": "run_synthetic",
                "run_type": run_type,
                "status": status,
                "created_at": "2026-01-01T00:00:00Z",
                "modified_at": "2026-01-01T00:00:00Z",
            }
        )
        .model_dump(mode="json")
    )


@pytest.mark.parametrize("run_type", [RunType.task_v1, RunType.task_v2, RunType.task_v3, RunType.workflow_run])
@pytest.mark.parametrize("status", [RunStatus.running, RunStatus.completed])
@pytest.mark.asyncio
async def test_sdk_get_run_parses_backend_response(run_type: RunType, status: RunStatus) -> None:
    payload = _run_payload(run_type, status)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http:
        sdk = AsyncSkyvern(api_key="synthetic", httpx_client=http)
        response = await sdk.get_run(payload["run_id"])

    assert response.run_type == run_type.value
    assert response.status == status.value
    assert response.run_id == payload["run_id"]


@pytest.mark.parametrize("run_type", [RunType.task_v1, RunType.task_v2, RunType.task_v3, RunType.workflow_run])
@pytest.mark.asyncio
async def test_agent_polls_running_to_completed(run_type: RunType, monkeypatch: pytest.MonkeyPatch) -> None:
    responses: list[GetRunResponse] = [
        TypeAdapter(GetRunResponse).validate_python(_run_payload(run_type, status))
        for status in (RunStatus.running, RunStatus.completed)
    ]
    browser = MagicMock()
    browser.skyvern.get_run = AsyncMock(side_effect=responses)
    monkeypatch.setattr(skyvern_browser_page_agent, "DEFAULT_AGENT_HEARTBEAT_INTERVAL", 0)
    agent = SkyvernBrowserPageAgent(browser, MagicMock())

    response = await agent._wait_for_run_completion("run_synthetic", timeout=1)

    assert response.run_type == run_type.value
    assert response.status == "completed"
