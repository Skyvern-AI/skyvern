"""Tests for POST /workflows/{wpid}/browser_session/refresh."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import SkyvernHTTPException, WorkflowNotFound
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.services import org_auth_service


def _build_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock, AsyncMock]:
    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    get_workflow = AsyncMock()
    delete_browser_session = AsyncMock()
    monkeypatch.setattr(forge_app.WORKFLOW_SERVICE, "get_workflow_by_permanent_id", get_workflow)
    monkeypatch.setattr(forge_app.STORAGE, "delete_browser_session", delete_browser_session)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")

    @fastapi_app.exception_handler(SkyvernHTTPException)
    async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    return TestClient(fastapi_app), get_workflow, delete_browser_session


def test_refresh_browser_session_clears_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    client, get_workflow, delete_browser_session = _build_client(monkeypatch)
    get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")

    response = client.post("/v1/workflows/wpid_123/browser_session/refresh")

    assert response.status_code == 204
    get_workflow.assert_awaited_once_with(workflow_permanent_id="wpid_123", organization_id="org_oss")
    delete_browser_session.assert_awaited_once_with(
        organization_id="org_oss",
        workflow_permanent_id="wpid_123",
    )


def test_refresh_browser_session_workflow_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, get_workflow, delete_browser_session = _build_client(monkeypatch)
    get_workflow.side_effect = WorkflowNotFound(workflow_permanent_id="wpid_missing")

    response = client.post("/v1/workflows/wpid_missing/browser_session/refresh")

    assert response.status_code == 404
    delete_browser_session.assert_not_awaited()


def test_refresh_browser_session_storage_failure_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    client, get_workflow, delete_browser_session = _build_client(monkeypatch)
    get_workflow.return_value = SimpleNamespace(workflow_permanent_id="wpid_123")
    delete_browser_session.side_effect = RuntimeError("s3 AccessDenied")

    response = client.post("/v1/workflows/wpid_123/browser_session/refresh")

    assert response.status_code == 500
    assert "retry" in response.json()["detail"].lower()
