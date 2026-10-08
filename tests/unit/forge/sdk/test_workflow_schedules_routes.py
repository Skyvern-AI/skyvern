"""OSS-side tests for the workflow schedules route module."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from freezegun import freeze_time

import skyvern.forge.sdk.routes.workflow_schedules as workflow_schedule_routes
from skyvern.cli.mcp_tools.schedule import (
    skyvern_schedule_cancel,
    skyvern_schedule_create,
    skyvern_schedule_get,
    skyvern_schedule_list,
    skyvern_schedule_update,
)
from skyvern.client import AsyncSkyvern
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.api_app import register_agent_route_aliases
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.routes import routers as routers_module
from skyvern.forge.sdk.schemas.workflow_schedules import OneTimeDispatchStatus
from skyvern.forge.sdk.services import org_auth_service
from tests.unit.forge.sdk.db import conftest as db_fixtures

ORG_ID = "org_oss"
WPID = "wpid_interval"
NOW = datetime(2026, 10, 30, 12, 0, 30, tzinfo=UTC)
HOUR = 60 * 60

agent_db = db_fixtures.agent_db
db_engine = db_fixtures.db_engine


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

    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id="org_oss")

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(getattr(routers_module, router_name), prefix=prefix)

    client = TestClient(fastapi_app)
    response = client.get(f"{prefix}/schedules")
    assert response.status_code == 200
    assert response.json() == {"schedules": [], "total_count": 0, "page": 1, "page_size": 10}


@pytest_asyncio.fixture
async def schedules_client(agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(
        workflow_schedule_routes,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=AgentFunction(),
            DATABASE=agent_db,
            WORKFLOW_SERVICE=SimpleNamespace(validate_schedule_parameters=AsyncMock()),
        ),
    )
    monkeypatch.setattr(
        workflow_schedule_routes,
        "_ensure_workflow_exists",
        AsyncMock(return_value=SimpleNamespace(workflow_permanent_id=WPID, max_elapsed_time_minutes=None)),
    )

    async def _fake_org() -> SimpleNamespace:
        return SimpleNamespace(organization_id=ORG_ID)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = _fake_org
    fastapi_app.include_router(routers_module.base_router, prefix="/v1")
    register_agent_route_aliases(fastapi_app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as client:
        yield client


async def _request(client: httpx.AsyncClient, when: datetime, method: str, path: str, **kwargs: Any) -> httpx.Response:
    with freeze_time(when, real_asyncio=True):
        return await client.request(method, f"/v1/workflows/{WPID}/schedules{path}", **kwargs)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_seconds", [72 * HOUR, 5 * HOUR], ids=["72h", "5h"])
@pytest.mark.parametrize("explicit_first_fire_at", [None, datetime(2026, 10, 31, 9, 17, tzinfo=UTC)])
async def test_create_interval_schedule_anchors_one_interval_out_unless_a_future_first_fire_is_given(
    schedules_client: httpx.AsyncClient,
    agent_db: AgentDB,
    interval_seconds: int,
    explicit_first_fire_at: datetime | None,
) -> None:
    body: dict[str, Any] = {"interval_seconds": interval_seconds, "timezone": "America/New_York"}
    if explicit_first_fire_at is not None:
        body["first_fire_at"] = explicit_first_fire_at.isoformat()
    anchor = explicit_first_fire_at or NOW + timedelta(seconds=interval_seconds)

    response = await _request(schedules_client, NOW, "POST", "", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schedule"]["cron_expression"] is None
    assert payload["schedule"]["interval_seconds"] == interval_seconds
    assert payload["schedule"]["first_fire_at"] == _iso(anchor)
    assert payload["next_runs"][:2] == [_iso(anchor), _iso(anchor + timedelta(seconds=interval_seconds))]
    [stored] = await agent_db.schedules.get_workflow_schedules(workflow_permanent_id=WPID, organization_id=ORG_ID)
    assert stored.first_fire_at == anchor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,field",
    [
        ({"interval_seconds": 299, "timezone": "UTC"}, "interval_seconds"),
        ({"cron_expression": "0 * * * *", "interval_seconds": 3600, "timezone": "UTC"}, "cron_expression"),
        ({"timezone": "UTC"}, "interval_seconds"),
        ({"interval_seconds": 3600, "first_fire_at": "2026-10-30T12:00:00Z", "timezone": "UTC"}, "first_fire_at"),
        ({"interval_seconds": 3600, "first_fire_at": "2026-10-30T12:01:00Z", "timezone": "UTC"}, "first_fire_at"),
    ],
    ids=["under-five-minutes", "cron-and-interval", "no-cadence", "past-first-fire", "first-fire-under-a-minute-out"],
)
async def test_create_rejects_invalid_cadence_with_a_field_named_422_and_writes_nothing(
    schedules_client: httpx.AsyncClient,
    agent_db: AgentDB,
    body: dict[str, Any],
    field: str,
) -> None:
    response = await _request(schedules_client, NOW, "POST", "", json=body)

    assert response.status_code == 422
    assert field in response.text
    assert await agent_db.schedules.get_workflow_schedules(workflow_permanent_id=WPID, organization_id=ORG_ID) == []


@pytest.mark.asyncio
async def test_put_echoing_a_fetched_past_anchor_keeps_it_and_a_changed_past_anchor_is_rejected(
    schedules_client: httpx.AsyncClient,
) -> None:
    created = await _request(schedules_client, NOW, "POST", "", json={"interval_seconds": 5 * HOUR, "timezone": "UTC"})
    schedule_id = created.json()["schedule"]["workflow_schedule_id"]
    later = NOW + timedelta(days=2)

    fetched = (await _request(schedules_client, later, "GET", f"/{schedule_id}")).json()
    echoed = await _request(
        schedules_client, later, "PUT", f"/{schedule_id}", json={**fetched["schedule"], "name": "renamed"}
    )

    assert echoed.status_code == 200, echoed.text
    assert echoed.json()["schedule"]["name"] == "renamed"
    assert echoed.json()["schedule"]["first_fire_at"] == fetched["schedule"]["first_fire_at"]
    assert echoed.json()["next_runs"] == fetched["next_runs"]

    moved = await _request(
        schedules_client,
        later,
        "PUT",
        f"/{schedule_id}",
        json={**fetched["schedule"], "first_fire_at": _iso(NOW + timedelta(hours=1))},
    )
    assert moved.status_code == 422
    assert "first_fire_at" in moved.text


@pytest.mark.asyncio
async def test_update_keeps_the_anchor_across_interval_edits_and_reanchors_when_switching_kinds(
    schedules_client: httpx.AsyncClient,
) -> None:
    created = await _request(schedules_client, NOW, "POST", "", json={"interval_seconds": 5 * HOUR, "timezone": "UTC"})
    schedule_id = created.json()["schedule"]["workflow_schedule_id"]
    anchor = created.json()["schedule"]["first_fire_at"]
    later = NOW + timedelta(days=1)

    lengthened = await _request(
        schedules_client, later, "PUT", f"/{schedule_id}", json={"interval_seconds": 7 * HOUR, "timezone": "UTC"}
    )
    assert lengthened.json()["schedule"]["first_fire_at"] == anchor

    to_cron = await _request(
        schedules_client, later, "PUT", f"/{schedule_id}", json={"cron_expression": "0 9 * * *", "timezone": "UTC"}
    )
    assert to_cron.status_code == 200, to_cron.text
    assert to_cron.json()["schedule"]["interval_seconds"] is None
    assert to_cron.json()["schedule"]["first_fire_at"] is None

    to_interval = await _request(
        schedules_client, later, "PUT", f"/{schedule_id}", json={"interval_seconds": 5 * HOUR, "timezone": "UTC"}
    )
    assert to_interval.json()["schedule"]["cron_expression"] is None
    assert to_interval.json()["schedule"]["first_fire_at"] == _iso(later + timedelta(hours=5))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rest", "mcp"])
@pytest.mark.parametrize(
    "first_fire_at",
    ["9800-01-01T00:00:00+00:00", "9999-12-31T23:00:00-05:00"],
    ids=["ticks-overflow", "anchor-overflows-in-utc"],
)
async def test_first_fire_at_too_far_out_to_compute_ticks_is_a_field_named_422_that_writes_nothing(
    schedules_client: httpx.AsyncClient,
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    first_fire_at: str,
) -> None:
    created = await _request(schedules_client, NOW, "POST", "", json={"interval_seconds": 5 * HOUR, "timezone": "UTC"})
    schedule_id = created.json()["schedule"]["workflow_schedule_id"]
    sdk = AsyncSkyvern(base_url="http://test", api_key="test", httpx_client=schedules_client)
    monkeypatch.setattr("skyvern.cli.mcp_tools.schedule.get_skyvern", lambda: sdk)
    body = {"interval_seconds": 2**31 - 1, "first_fire_at": first_fire_at, "timezone": "UTC"}

    with freeze_time(NOW, real_asyncio=True):
        if entry == "rest":
            prefix = f"/v1/workflows/{WPID}/schedules"
            created_via_rest = await schedules_client.post(prefix, json=body)
            updated_via_rest = await schedules_client.put(f"{prefix}/{schedule_id}", json=body)
            create_error = f"{created_via_rest.status_code} {created_via_rest.text}"
            update_error = f"{updated_via_rest.status_code} {updated_via_rest.text}"
        else:
            anchor = datetime.fromisoformat(first_fire_at)
            created_via_mcp = await skyvern_schedule_create(
                workflow_permanent_id=WPID, timezone="UTC", interval_seconds=2**31 - 1, first_fire_at=anchor
            )
            updated_via_mcp = await skyvern_schedule_update(
                workflow_permanent_id=WPID, workflow_schedule_id=schedule_id, first_fire_at=anchor
            )
            assert not created_via_mcp["ok"] and not updated_via_mcp["ok"]
            create_error = created_via_mcp["error"]["message"]
            update_error = updated_via_mcp["error"]["message"]

    for error in (create_error, update_error):
        assert "422" in error
        assert "first_fire_at" in error and "must be no later than" in error
    [stored] = await agent_db.schedules.get_workflow_schedules(workflow_permanent_id=WPID, organization_id=ORG_ID)
    assert stored.interval_seconds == 5 * HOUR
    assert stored.first_fire_at == NOW + timedelta(hours=5)


RUN_AT = NOW + timedelta(minutes=10)
REAL_ENSURE_WORKFLOW_EXISTS = workflow_schedule_routes._ensure_workflow_exists


async def _create_one_time_and_assert_nothing_written(
    schedules_client: httpx.AsyncClient, agent_db: AgentDB, body: dict[str, Any], status: int
) -> None:
    response = await _request(schedules_client, NOW, "POST", "", json={**body, "timezone": "America/Los_Angeles"})

    assert response.status_code == status, response.text
    assert await agent_db.schedules.get_workflow_schedules(workflow_permanent_id=WPID, organization_id=ORG_ID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"run_at": _iso(NOW - timedelta(minutes=1))}, id="past"),
        pytest.param({"run_at": _iso(NOW + timedelta(seconds=30))}, id="under-a-minute"),
        pytest.param({"run_at": "2026-10-30T12:10:00"}, id="naive"),
        pytest.param({"run_at": _iso(RUN_AT), "enabled": False}, id="disabled"),
        pytest.param({"run_at": _iso(RUN_AT), "cron_expression": "0 9 * * *"}, id="two-cadences"),
    ],
)
async def test_create_one_time_rejects_each_invalid_input_with_a_422_and_writes_nothing(
    schedules_client: httpx.AsyncClient, agent_db: AgentDB, body: dict[str, Any]
) -> None:
    await _create_one_time_and_assert_nothing_written(schedules_client, agent_db, body, 422)


@pytest.mark.asyncio
async def test_create_one_time_rejects_invalid_parameters_with_a_400_and_writes_nothing(
    schedules_client: httpx.AsyncClient, agent_db: AgentDB
) -> None:
    workflow_schedule_routes.app.WORKFLOW_SERVICE.validate_schedule_parameters.side_effect = HTTPException(
        status_code=400, detail="Missing required parameter: city"
    )

    await _create_one_time_and_assert_nothing_written(schedules_client, agent_db, {"run_at": _iso(RUN_AT)}, 400)


@pytest.mark.asyncio
async def test_create_one_time_on_a_foreign_workflow_returns_404_and_writes_nothing(
    schedules_client: httpx.AsyncClient, agent_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workflow_schedule_routes, "_ensure_workflow_exists", REAL_ENSURE_WORKFLOW_EXISTS)

    await _create_one_time_and_assert_nothing_written(schedules_client, agent_db, {"run_at": _iso(RUN_AT)}, 404)


@pytest.mark.asyncio
async def test_one_time_run_at_after_2100_is_rejected_before_the_backend_sync(
    schedules_client: httpx.AsyncClient,
) -> None:
    too_late = await _request(
        schedules_client, NOW, "POST", "", json={"run_at": "2101-01-01T00:00:00Z", "timezone": "UTC"}
    )
    last_allowed = await _request(
        schedules_client, NOW, "POST", "", json={"run_at": "2100-12-31T23:59:59Z", "timezone": "UTC"}
    )

    assert too_late.status_code == 422 and "run_at must be no later than 2100-12-31T23:59:59" in too_late.text
    assert last_allowed.status_code == 200


@pytest.mark.asyncio
async def test_one_time_schedule_edits_while_pending_and_conflicts_unchanged_once_fired(
    schedules_client: httpx.AsyncClient,
    agent_db: AgentDB,
) -> None:
    created = await _request(
        schedules_client,
        NOW,
        "POST",
        "",
        json={"run_at": "2026-10-30T05:10:30.750-07:00", "timezone": "America/Los_Angeles", "parameters": {"a": 1}},
    )
    assert created.status_code == 200, created.text
    schedule = created.json()["schedule"]
    schedule_id = schedule["workflow_schedule_id"]
    assert (schedule["run_at"], schedule["dispatch_status"], schedule["workflow_run_id"]) == (
        _iso(RUN_AT),
        "pending",
        None,
    )
    assert created.json()["next_runs"] == [_iso(RUN_AT)]

    for path, method, body in (
        (f"/{schedule_id}", "PUT", {"cron_expression": "0 9 * * *", "timezone": "UTC"}),
        (f"/{schedule_id}/disable", "POST", None),
        (f"/{schedule_id}/enable", "POST", None),
    ):
        refused = await _request(schedules_client, NOW, method, path, json=body)
        assert refused.status_code == 422, (path, refused.text)

    moved_to = RUN_AT + timedelta(minutes=5)
    edited = await _request(
        schedules_client,
        NOW,
        "PUT",
        f"/{schedule_id}",
        json={"run_at": _iso(moved_to), "timezone": "America/Los_Angeles", "parameters": {"a": 2}},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["schedule"]["run_at"] == _iso(moved_to)
    assert edited.json()["schedule"]["parameters"] == {"a": 2}
    pending = (await schedules_client.get("/v1/schedules")).json()["schedules"]
    assert [(item["dispatch_status"], item["next_run"]) for item in pending] == [("pending", _iso(moved_to))]

    with freeze_time(RUN_AT, real_asyncio=True):
        assert await agent_db.schedules.claim_one_time_dispatch(schedule_id, ORG_ID, "wr_sched_early", RUN_AT) is None
    with freeze_time(moved_to, real_asyncio=True):
        claimed = await agent_db.schedules.claim_one_time_dispatch(schedule_id, ORG_ID, "wr_sched_once", moved_to)
        assert claimed is not None and claimed.parameters == {"a": 2}
        assert (
            await agent_db.schedules.claim_one_time_dispatch(schedule_id, ORG_ID, "wr_sched_once", moved_to) == claimed
        )
        assert await agent_db.schedules.claim_one_time_dispatch(schedule_id, ORG_ID, "wr_sched_other", moved_to) is None

    before = (await _request(schedules_client, moved_to, "GET", f"/{schedule_id}")).json()
    assert before["schedule"]["dispatch_status"] == "fired"
    assert before["schedule"]["workflow_run_id"] == "wr_sched_once"
    assert before["next_runs"] == []
    for path, method, body in (
        (f"/{schedule_id}", "PUT", {"run_at": _iso(moved_to + timedelta(hours=1)), "timezone": "UTC"}),
        (f"/{schedule_id}/cancel", "POST", None),
    ):
        conflict = await _request(schedules_client, moved_to, method, path, json=body)
        assert conflict.status_code == 409, (path, conflict.text)
    raced_edit = await agent_db.schedules.update_workflow_schedule(
        workflow_schedule_id=schedule_id,
        organization_id=ORG_ID,
        cron_expression=None,
        timezone="UTC",
        parameters={"a": 3},
        run_at=moved_to + timedelta(hours=1),
    )
    assert raced_edit is None
    after = (await _request(schedules_client, moved_to, "GET", f"/{schedule_id}")).json()
    assert after == before

    listed = (await schedules_client.get("/v1/schedules")).json()["schedules"]
    assert [
        (item["run_at"], item["dispatch_status"], item["workflow_run_id"], item["next_run"]) for item in listed
    ] == [(_iso(moved_to), "fired", "wr_sched_once", None)]


@pytest.mark.asyncio
async def test_cancel_keeps_a_pending_one_time_schedule_readable_and_it_never_fires(
    schedules_client: httpx.AsyncClient,
    agent_db: AgentDB,
) -> None:
    created = await _request(schedules_client, NOW, "POST", "", json={"run_at": _iso(RUN_AT), "timezone": "UTC"})
    schedule_id = created.json()["schedule"]["workflow_schedule_id"]
    recurring = await _request(
        schedules_client, NOW, "POST", "", json={"cron_expression": "0 9 * * *", "timezone": "UTC"}
    )

    canceled = await _request(schedules_client, NOW, "POST", f"/{schedule_id}/cancel")
    not_one_time = await _request(
        schedules_client, NOW, "POST", f"/{recurring.json()['schedule']['workflow_schedule_id']}/cancel"
    )

    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["schedule"]["dispatch_status"] == OneTimeDispatchStatus.canceled
    assert canceled.json()["next_runs"] == []
    assert not_one_time.status_code == 422
    with freeze_time(RUN_AT + timedelta(days=1), real_asyncio=True):
        assert await agent_db.schedules.claim_one_time_dispatch(schedule_id, ORG_ID, "wr_sched_late", RUN_AT) is None
        due_ids = [s.workflow_schedule_id for s in await agent_db.schedules.get_all_enabled_schedules()]
    assert schedule_id not in due_ids
    assert (await _request(schedules_client, NOW, "GET", f"/{schedule_id}")).json()["schedule"][
        "dispatch_status"
    ] == "canceled"


@pytest.mark.asyncio
async def test_mcp_tools_create_edit_cancel_and_read_back_every_one_time_state(
    schedules_client: httpx.AsyncClient,
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = AsyncSkyvern(base_url="http://test", api_key="test", httpx_client=schedules_client)
    monkeypatch.setattr("skyvern.cli.mcp_tools.schedule.get_skyvern", lambda: sdk)

    async def state(schedule_id: str) -> tuple[str, str | None, list[str]]:
        got = await skyvern_schedule_get(workflow_permanent_id=WPID, workflow_schedule_id=schedule_id)
        assert got["ok"], got
        schedule = got["data"]["schedule"]
        return schedule["dispatch_status"], schedule["workflow_run_id"], got["data"]["next_runs"]

    with freeze_time(NOW, real_asyncio=True):
        created = [
            await skyvern_schedule_create(
                workflow_permanent_id=WPID, timezone="America/Los_Angeles", run_at=RUN_AT, parameters={"n": n}
            )
            for n in range(3)
        ]
        assert all(result["ok"] for result in created), created
        canceled_id, fired_id, failed_id = (result["data"]["schedule"]["workflow_schedule_id"] for result in created)
        edited = await skyvern_schedule_update(
            workflow_permanent_id=WPID, workflow_schedule_id=fired_id, parameters={"n": 10}
        )
        assert edited["ok"], edited
        assert (edited["data"]["schedule"]["run_at"], edited["data"]["schedule"]["parameters"]) == (
            RUN_AT.isoformat(),
            {"n": 10},
        )
        assert (await skyvern_schedule_cancel(workflow_permanent_id=WPID, workflow_schedule_id=canceled_id))["ok"]

    with freeze_time(RUN_AT, real_asyncio=True):
        for schedule_id, run_id in ((fired_id, "wr_sched_fired"), (failed_id, "wr_sched_failed")):
            assert await agent_db.schedules.claim_one_time_dispatch(schedule_id, ORG_ID, run_id, RUN_AT) is not None
        await agent_db.schedules.finish_one_time_dispatch(
            failed_id, ORG_ID, "wr_sched_failed", OneTimeDispatchStatus.failed
        )
        refused = await skyvern_schedule_cancel(workflow_permanent_id=WPID, workflow_schedule_id=fired_id)
        listed = await skyvern_schedule_list()

        assert await state(canceled_id) == ("canceled", None, [])
        assert await state(fired_id) == ("fired", "wr_sched_fired", [])
        assert await state(failed_id) == ("failed", "wr_sched_failed", [])
    assert refused["ok"] is False and "409" in refused["error"]["message"]
    assert sorted((item["dispatch_status"], item["workflow_run_id"]) for item in listed["data"]["schedules"]) == [
        ("canceled", None),
        ("failed", "wr_sched_failed"),
        ("fired", "wr_sched_fired"),
    ]
