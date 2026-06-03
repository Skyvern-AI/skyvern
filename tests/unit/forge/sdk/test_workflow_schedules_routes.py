"""OSS-side tests for the workflow schedules route module."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import skyvern.forge.sdk.routes.workflow_schedules as workflow_schedule_routes
from skyvern.forge.agent_functions import AgentFunction


@pytest.fixture(autouse=True)
def _install_oss_agent_function(monkeypatch: pytest.MonkeyPatch) -> AgentFunction:
    agent = AgentFunction()
    fake_app = SimpleNamespace(
        AGENT_FUNCTION=agent,
        DATABASE=SimpleNamespace(
            schedules=SimpleNamespace(
                list_organization_schedules=AsyncMock(return_value=([], 0)),
            )
        ),
    )
    monkeypatch.setattr(workflow_schedule_routes, "app", fake_app)
    return agent


def _organization() -> SimpleNamespace:
    return SimpleNamespace(organization_id="org_oss")


def test_oss_agent_function_enables_workflow_schedules() -> None:
    agent = AgentFunction()
    assert agent.workflow_schedules_enabled is True
    assert agent.workflow_schedules_use_local_scheduler is True


def test_require_schedules_enabled_allows_oss() -> None:
    assert workflow_schedule_routes._require_schedules_enabled() is None


def test_oss_build_workflow_schedule_id_returns_local_backend_id() -> None:
    assert AgentFunction().build_workflow_schedule_id("wfs_123") == "local-wf-sched-wfs_123"


@pytest.mark.asyncio
async def test_oss_upsert_workflow_schedule_is_noop() -> None:
    result = await AgentFunction().upsert_workflow_schedule(
        backend_schedule_id="ws_123",
        organization_id="org_oss",
        workflow_permanent_id="wpid_123",
        workflow_schedule_id="ws_123",
        cron_expression="0 */6 * * *",
        timezone="UTC",
        enabled=True,
        parameters=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_oss_set_workflow_schedule_enabled_is_noop() -> None:
    assert await AgentFunction().set_workflow_schedule_enabled("ws_123", enabled=False) is None


@pytest.mark.asyncio
async def test_oss_delete_workflow_schedule_is_noop() -> None:
    assert await AgentFunction().delete_workflow_schedule("ws_123") is None


@pytest.mark.parametrize(
    "prefix,router_name",
    [("/v1", "base_router"), ("/api/v1", "legacy_base_router")],
)
def test_oss_route_returns_schedule_list_via_testclient(prefix: str, router_name: str) -> None:
    """End-to-end: route resolves through the FastAPI dependency chain on both
    the public `/v1` mount and the legacy `/api/v1` alias."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from skyvern.forge.sdk.routes import routers as routers_module
    from skyvern.forge.sdk.services import org_auth_service

    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(getattr(routers_module, router_name), prefix=prefix)

    client = TestClient(fastapi_app)
    response = client.get(f"{prefix}/schedules")
    assert response.status_code == 200
    assert response.json() == {"schedules": [], "total_count": 0, "page": 1, "page_size": 10}
