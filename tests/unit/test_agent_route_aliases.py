"""The public /workflows API surface is mirrored to /agents and hidden from the OpenAPI schema.

`register_agent_route_aliases` adds an /agents twin for every in-schema /workflows route (preserving
the handler, params, response models and fern SDK metadata) and drops the /workflows form from the
schema while keeping it callable for backwards compatibility.
"""

import importlib

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from skyvern.forge.api_app import register_agent_route_aliases
from skyvern.forge.sdk.routes.routers import base_router


@pytest.fixture(scope="module")
def app() -> FastAPI:
    # Import the route modules so base_router is fully populated before we mirror it.
    for module in ("agent_protocol", "run_blocks", "workflow_schedules"):
        importlib.import_module(f"skyvern.forge.sdk.routes.{module}")
    fastapi_app = FastAPI()
    fastapi_app.include_router(base_router, prefix="/v1")
    register_agent_route_aliases(fastapi_app)
    return fastapi_app


def test_no_workflow_paths_remain_in_schema(app: FastAPI) -> None:
    visible_workflow_paths = [path for path in app.openapi()["paths"] if "workflows" in path]
    assert visible_workflow_paths == []


@pytest.mark.parametrize(
    "expected_path",
    [
        "/v1/run/agents",
        "/v1/agents",
        "/v1/agents/{workflow_id}",
        "/v1/agents/{workflow_id}/delete",
        "/v1/agents/{workflow_permanent_id}",
        "/v1/agents/{workflow_permanent_id}/versions",
        "/v1/agents/{workflow_permanent_id}/schedules",
        "/v1/agents/{workflow_permanent_id}/tags",
    ],
)
def test_agents_paths_present_in_schema(app: FastAPI, expected_path: str) -> None:
    assert expected_path in app.openapi()["paths"]


def test_fern_metadata_preserved_on_twins(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    # SDK method name carried over verbatim, so generated SDK methods are unchanged
    assert paths["/v1/run/agents"]["post"]["x-fern-sdk-method-name"] == "run_workflow"
    # operation_id-based method names are preserved too
    assert paths["/v1/agents/{workflow_permanent_id}/schedules"]["post"]["operationId"] == "schedules_create"


def test_twins_preserve_original_operation_id(app: FastAPI) -> None:
    # A mirrored route with no explicit operation_id must keep the original /workflows-derived
    # operationId (not get a fresh /agents one), so OpenAPI-generated clients don't see renamed operations.
    operation_id = app.openapi()["paths"]["/v1/agents"]["get"]["operationId"]
    assert "workflows" in operation_id and "agents" not in operation_id


def test_workflow_routes_remain_callable_but_hidden(app: FastAPI) -> None:
    workflow_routes = [r for r in app.router.routes if isinstance(r, APIRoute) and "/workflows" in r.path]
    # the /workflows routes still exist so deployed SDKs / integrations keep working ...
    assert workflow_routes
    # ... but none of them are advertised in the public schema
    assert all(not route.include_in_schema for route in workflow_routes)


def test_task_endpoints_hidden_from_docs_but_kept_in_sdk(app: FastAPI) -> None:
    # `x-hidden` hides these from the Mintlify docs reference, but the operations stay in the
    # OpenAPI schema (include_in_schema=True), so the Fern SDK still generates the methods
    # (e.g. client.run_task()) — docs-only hide, backwards-compatible for SDK users.
    run_task = app.openapi()["paths"]["/v1/run/tasks"]["post"]
    assert run_task["x-hidden"] is True
    assert run_task["x-fern-sdk-method-name"] == "run_task"
