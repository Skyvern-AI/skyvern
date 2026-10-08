"""Regression tests for the ``update_workflow`` route's error handling.

A ``POST /v1/workflows/{workflow_id}`` body carrying neither ``yaml_definition`` nor
``json_definition`` is a client error and must return 422. The inline ``HTTPException(422)``
used to be swallowed by the handler's catch-all ``except Exception`` and re-wrapped as a 500
(``FailedToUpdateWorkflow``), tripping the production zero-threshold 5xx monitor.
"""

from __future__ import annotations

import datetime as dt
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.runs import RunEngine

ORG_ID = "o_test"


def _make_org() -> Organization:
    now = dt.datetime.now(dt.timezone.utc)
    return Organization(
        organization_id=ORG_ID,
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


@pytest.fixture(scope="module")
def client() -> TestClient:
    importlib.import_module("skyvern.forge.sdk.routes.agent_protocol")

    app = FastAPI()
    app.include_router(base_router, prefix="/v1")

    # Mirror api_app.py so a raised SkyvernHTTPException renders as its own status code
    # (e.g. the pre-fix FailedToUpdateWorkflow would render as 500 here, not be re-raised).
    @app.exception_handler(SkyvernHTTPException)
    async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    app.dependency_overrides[org_auth_service.get_current_org] = _make_org
    app.dependency_overrides[org_auth_service.get_current_user_id_or_none] = lambda: None

    return TestClient(app)


def test_update_workflow_without_definition_returns_422(client: TestClient) -> None:
    resp = client.post("/v1/workflows/wpid_test", json={})

    assert resp.status_code == 422, resp.text
    assert "json" in resp.json()["detail"].lower()


def _stored_workflow() -> Workflow:
    now = dt.datetime.now(dt.timezone.utc)
    return Workflow(
        workflow_id="w_test",
        organization_id=ORG_ID,
        title="t",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
    )


@pytest.mark.parametrize("computed", [None, RunEngine.skyvern_v3])
def test_only_the_detail_get_carries_the_effective_default_engine(
    client: TestClient, computed: RunEngine | None
) -> None:
    # A null means routing decides the engine, so a response that never computed it must omit the key.
    mock_app = MagicMock()
    mock_app.WORKFLOW_SERVICE.get_workflow_by_permanent_id = AsyncMock(return_value=_stored_workflow())
    mock_app.WORKFLOW_SERVICE.get_workflow_versions_by_permanent_id = AsyncMock(return_value=[_stored_workflow()])
    mock_app.WORKFLOW_SERVICE.create_workflow_from_request = AsyncMock(return_value=(_stored_workflow(), ()))
    mock_app.WORKFLOW_SERVICE.get_workflows_by_organization_id = AsyncMock(return_value=[_stored_workflow()])
    mock_app.DATABASE.workflows.is_workflow_copilot_authored = AsyncMock(return_value=False)
    mock_app.AGENT_FUNCTION.on_workflow_updated_by_user = AsyncMock()
    mock_app.AGENT_FUNCTION.record_audit_event = AsyncMock()
    with (
        patch("skyvern.forge.sdk.routes.agent_protocol.app", mock_app),
        patch("skyvern.forge.sdk.routes.agent_protocol.effective_default_engine", AsyncMock(return_value=computed)),
    ):
        detail = client.get("/v1/workflows/wpid_test")
        versions = client.get("/v1/workflows/wpid_test/versions")
        listed = client.get("/v1/workflows")
        saved = client.post(
            "/v1/workflows/wpid_test",
            json={"yaml_definition": "title: t\nworkflow_definition:\n  parameters: []\n  blocks: []\n"},
        )

    assert detail.status_code == 200, detail.text
    assert detail.json()["effective_default_engine"] == (computed and computed.value)
    assert versions.status_code == 200, versions.text
    assert "effective_default_engine" not in versions.json()[0]
    assert listed.status_code == 200, listed.text
    assert "effective_default_engine" not in listed.json()[0]
    assert saved.status_code == 200, saved.text
    assert "effective_default_engine" not in saved.json()
