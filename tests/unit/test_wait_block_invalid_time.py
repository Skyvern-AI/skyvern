"""Regression tests for ``InvalidWaitBlockTime`` (SKY-14616).

An out-of-range wait block is client input, but the exception subclassed plain
``SkyvernException``, so both HTTP entry paths reported it as a 5xx. These assert the status
code rather than that something raised: ``pytest.raises(InvalidWaitBlockTime)`` alone passes
against the unfixed code, because which class is raised is the entire defect.
"""

from __future__ import annotations

import datetime as dt
import importlib
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from skyvern.config import settings
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.exceptions import (
    InvalidWaitBlockTime,
    WorkflowDefinitionValidationException,
)
from skyvern.forge.sdk.workflow.models.block import WaitBlock
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import WaitBlockYAML, WorkflowDefinitionYAML

ORG_ID = "o_test"
BLOCK_LABEL = "let_page_settle"

# The copilot route validates the string as a bare WorkflowDefinitionYAML, not a create request.
WAIT_BLOCK_YAML = """
parameters: []
blocks:
  - block_type: wait
    label: let_page_settle
    wait_sec: 0
"""


def _definition(wait_sec: int) -> WorkflowDefinitionYAML:
    return WorkflowDefinitionYAML(
        parameters=[],
        blocks=[WaitBlockYAML(label=BLOCK_LABEL, wait_sec=wait_sec)],
    )


def _make_org() -> Organization:
    now = dt.datetime.now(dt.timezone.utc)
    return Organization(
        organization_id=ORG_ID,
        organization_name="Test Org",
        created_at=now,
        modified_at=now,
    )


@pytest.mark.parametrize("wait_sec", [0, -1, settings.WORKFLOW_WAIT_BLOCK_MAX_SEC + 1])
def test_out_of_range_wait_is_a_422_validation_error(wait_sec: int) -> None:
    with pytest.raises(InvalidWaitBlockTime) as excinfo:
        convert_workflow_definition(_definition(wait_sec), workflow_id="wf_test")

    exc = excinfo.value
    # The class is the whole defect: the create route keys its 422 branch off this base, and the
    # copilot route keys its 400 branch off BaseWorkflowHTTPException, which this now inherits.
    assert isinstance(exc, WorkflowDefinitionValidationException)
    assert exc.status_code == 422

    message = str(exc.message)
    assert BLOCK_LABEL in message
    # Anchored to the preceding word: a bare `str(wait_sec) in message` passes vacuously for 0,
    # which also appears inside "1800".
    assert f"wait time {wait_sec}" in message
    assert f"between 1 and {settings.WORKFLOW_WAIT_BLOCK_MAX_SEC}" in message


@pytest.mark.parametrize("wait_sec", [1, settings.WORKFLOW_WAIT_BLOCK_MAX_SEC])
def test_in_range_wait_still_converts(wait_sec: int) -> None:
    converted = convert_workflow_definition(_definition(wait_sec), workflow_id="wf_test")

    block = converted.blocks[0]
    assert isinstance(block, WaitBlock)
    assert block.wait_sec == wait_sec


class _ConvertOnlyWorkflowService:
    """Stands in for the DB round trips the create route makes before conversion, so the real
    converter still raises and the assertion stays on the route's own dispatch."""

    async def create_workflow_from_request(self, *, organization: Any, request: Any, **kwargs: Any) -> Any:
        convert_workflow_definition(request.workflow_definition, workflow_id="wf_test")
        raise AssertionError("conversion was expected to reject the wait block")


@pytest.fixture(scope="module")
def client() -> TestClient:
    importlib.import_module("skyvern.forge.sdk.routes.agent_protocol")
    importlib.import_module("skyvern.forge.sdk.routes.workflow_copilot")

    app = FastAPI()
    app.include_router(base_router, prefix="/v1")

    # Mirror api_app.py, so a SkyvernHTTPException renders as its own status code. Without this
    # the pre-fix FailedToCreateWorkflow would surface as an unhandled error rather than its 500.
    @app.exception_handler(SkyvernHTTPException)
    async def _handle_skyvern_http_exception(request: Request, exc: SkyvernHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    app.dependency_overrides[org_auth_service.get_current_org] = _make_org
    app.dependency_overrides[org_auth_service.get_current_user_id_or_none] = lambda: None

    return TestClient(app)


def test_create_workflow_route_returns_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forge_app, "WORKFLOW_SERVICE", _ConvertOnlyWorkflowService())

    resp = client.post(
        "/v1/workflows",
        json={
            "json_definition": {
                "title": "Wait block regression",
                "workflow_definition": {
                    "parameters": [],
                    "blocks": [{"block_type": "wait", "label": BLOCK_LABEL, "wait_sec": 0}],
                },
            }
        },
    )

    assert resp.status_code == 422, resp.text
    assert BLOCK_LABEL in resp.json()["detail"]


def test_copilot_convert_yaml_route_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/workflow/copilot/convert-yaml-to-blocks",
        json={"workflow_definition_yaml": WAIT_BLOCK_YAML, "workflow_id": "wf_test"},
    )

    assert resp.status_code == 400, resp.text
    assert BLOCK_LABEL in resp.json()["detail"]
