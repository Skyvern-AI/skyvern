"""Regression tests for the ``update_workflow`` route's error handling.

A ``POST /v1/workflows/{workflow_id}`` body carrying neither ``yaml_definition`` nor
``json_definition`` is a client error and must return 422. The inline ``HTTPException(422)``
used to be swallowed by the handler's catch-all ``except Exception`` and re-wrapped as a 500
(``FailedToUpdateWorkflow``), tripping the production zero-threshold 5xx monitor.
"""

from __future__ import annotations

import datetime as dt
import importlib

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service

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
