import json
from datetime import UTC, datetime

import httpx
import pytest

from skyvern.client import AsyncSkyvern, Skyvern
from skyvern.client.types.workflow_run_request import WorkflowRunRequest


def test_workflow_run_request_accepts_run_metadata() -> None:
    request = WorkflowRunRequest(workflow_id="wpid_123", run_metadata={"customer": "acme"})

    assert request.run_metadata == {"customer": "acme"}


def test_run_workflow_sends_run_metadata() -> None:
    captured_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(
            status_code=200,
            json={
                "run_id": "wr_123",
                "status": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "modified_at": datetime.now(UTC).isoformat(),
            },
        )

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.run_workflow(
        workflow_id="wpid_123",
        run_metadata={"customer": "acme", "tier": "enterprise"},
    )

    assert response.run_id == "wr_123"
    assert captured_bodies == [
        {
            "workflow_id": "wpid_123",
            "run_metadata": {"customer": "acme", "tier": "enterprise"},
        }
    ]


def _workflow_run_payload() -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "workflow_run_id": "wr_123",
        "workflow_id": "wf_123",
        "workflow_permanent_id": "wpid_123",
        "organization_id": "org_123",
        "status": "completed",
        "created_at": now,
        "modified_at": now,
    }


def _workflow_run_response_payload() -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "run_id": "wr_retry",
        "status": "queued",
        "created_at": now,
        "modified_at": now,
    }


def test_retry_workflow_run_uses_retry_route() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=_workflow_run_response_payload())

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.retry_workflow_run(
        "wr_original",
        max_steps_override=12,
        user_agent="skyvern-ui",
    )

    assert response.run_id == "wr_retry"
    assert captured_requests[0].method == "POST"
    assert captured_requests[0].url.path == "/v1/workflows/runs/wr_original/retry"
    assert captured_requests[0].headers["x-max-steps-override"] == "12"
    assert captured_requests[0].headers["x-user-agent"] == "skyvern-ui"


@pytest.mark.asyncio
async def test_async_retry_workflow_run_uses_retry_route() -> None:
    captured_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=_workflow_run_response_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as httpx_client:
        client = AsyncSkyvern(
            base_url="https://api.example.test",
            api_key="test-key",
            httpx_client=httpx_client,
        )
        response = await client.retry_workflow_run(
            "wr_original",
            max_steps_override=8,
            user_agent="skyvern-ui",
        )

    assert response.run_id == "wr_retry"
    assert captured_requests[0].method == "POST"
    assert captured_requests[0].url.path == "/v1/workflows/runs/wr_original/retry"
    assert captured_requests[0].headers["x-max-steps-override"] == "8"
    assert captured_requests[0].headers["x-user-agent"] == "skyvern-ui"


def test_get_workflow_runs_by_id_uses_workflow_scoped_route() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=[_workflow_run_payload()])

    client = Skyvern(
        base_url="https://api.example.test",
        api_key="test-key",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    runs = client.get_workflow_runs_by_id(
        "wpid_123",
        page=1,
        page_size=10,
        status="completed",
        search_key="acme",
        error_code="LOGIN_FAILED",
    )

    assert runs[0].workflow_run_id == "wr_123"
    assert captured_requests[0].method == "GET"
    assert captured_requests[0].url.path == "/v1/workflows/wpid_123/runs"
    assert captured_requests[0].url.params["page"] == "1"
    assert captured_requests[0].url.params["page_size"] == "10"
    assert captured_requests[0].url.params["status"] == "completed"
    assert captured_requests[0].url.params["search_key"] == "acme"
    assert captured_requests[0].url.params["error_code"] == "LOGIN_FAILED"


@pytest.mark.asyncio
async def test_async_get_workflow_runs_by_id_uses_workflow_scoped_route() -> None:
    captured_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code=200, json=[_workflow_run_payload()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as httpx_client:
        client = AsyncSkyvern(
            base_url="https://api.example.test",
            api_key="test-key",
            httpx_client=httpx_client,
        )
        runs = await client.get_workflow_runs_by_id(
            "wpid_123",
            page=2,
            page_size=5,
            status="failed",
            search_key="prod",
            error_code="TIMEOUT",
        )

    assert runs[0].workflow_run_id == "wr_123"
    assert captured_requests[0].method == "GET"
    assert captured_requests[0].url.path == "/v1/workflows/wpid_123/runs"
    assert captured_requests[0].url.params["page"] == "2"
    assert captured_requests[0].url.params["page_size"] == "5"
    assert captured_requests[0].url.params["status"] == "failed"
    assert captured_requests[0].url.params["search_key"] == "prod"
    assert captured_requests[0].url.params["error_code"] == "TIMEOUT"
