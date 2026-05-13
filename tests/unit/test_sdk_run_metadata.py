import json
from datetime import UTC, datetime

import httpx

from skyvern.client import Skyvern
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
