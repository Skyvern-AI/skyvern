import asyncio
import base64
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import WorkflowRunStatus
from skyvern.forge.sdk.db.models import TaskModel
from skyvern.forge.sdk.db.repositories import tasks as tasks_repository
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunResponseBase
from tests.unit.shared_route_app import shared_route_app

ORG_ID = "o_task_listing"


class _LoopProbe:
    """Holds a phase's first sync call until the event loop runs another coroutine.

    A call running on the loop thread blocks the loop, so the tick never comes and the wait times out.
    """

    def __init__(self) -> None:
        self._ticked = threading.Event()
        self.loop_ran: dict[str, bool] = {}

    async def tick_forever(self) -> None:
        while True:
            self._ticked.set()
            await asyncio.sleep(0.01)

    def gate(self, phase: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def gated(*args: Any, **kwargs: Any) -> Any:
            if phase not in self.loop_ran:
                self._ticked.clear()
                self.loop_ran[phase] = self._ticked.wait(timeout=5)
            return fn(*args, **kwargs)

        return gated


@pytest.mark.asyncio
async def test_task_listing_builds_and_serializes_the_page_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    db = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    for index in range(3):
        await db.tasks.create_task(
            url="https://example.com",
            title=f"Task {index}",
            navigation_goal="Find the pricing page",
            data_extraction_goal=None,
            navigation_payload={"index": index},
            organization_id=ORG_ID,
        )
    agent = ForgeAgent()
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(app_instance, "DATABASE", db)
    monkeypatch.setattr(app_instance, "agent", agent)
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    tasks = await db.tasks.get_tasks(page=1, page_size=1000, organization_id=ORG_ID)
    expected_body = ORJSONResponse([(await agent.build_task_response(task=task)).model_dump() for task in tasks]).body

    probe = _LoopProbe()
    monkeypatch.setattr(tasks_repository, "convert_to_task", probe.gate("convert", tasks_repository.convert_to_task))
    monkeypatch.setattr(Task, "to_task_response", probe.gate("serialize", Task.to_task_response))

    ticker = asyncio.create_task(probe.tick_forever())
    try:
        async with _client(shared_route_app(monkeypatch)) as client:
            response = await client.get("/api/v1/tasks", params={"page_size": 1000, "only_standalone_tasks": False})
    finally:
        ticker.cancel()

    assert response.status_code == 200, response.text
    assert probe.loop_ran == {"convert": True, "serialize": True}
    assert response.json() == json.loads(expected_body)
    assert len(response.json()) == 3
    assert agent_protocol.TASK_NEXT_CURSOR_HEADER not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_authorized", [False, True])
async def test_workflow_run_listing_respects_connect_authorization(
    monkeypatch: pytest.MonkeyPatch, connect_authorized: bool
) -> None:
    session_id = "pbs_test_workflow_run_listing"
    address = f"wss://router.test/{session_id}?token=secret"
    run = WorkflowRun.model_construct(
        workflow_run_id="wr_test_workflow_run_listing",
        workflow_id="wpid_test_workflow_run_listing",
        workflow_permanent_id="wpid_test_workflow_run_listing",
        organization_id=ORG_ID,
        status=WorkflowRunStatus.running,
        browser_session_id=session_id,
        browser_address=address,
    )
    authorization = AsyncMock(return_value={session_id} if connect_authorized else set())
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(
        app_instance,
        "WORKFLOW_SERVICE",
        SimpleNamespace(get_workflow_runs=AsyncMock(return_value=[run])),
    )
    monkeypatch.setattr(
        app_instance,
        "AGENT_FUNCTION",
        SimpleNamespace(
            browser_session_ids_authorized_for_connect=authorization,
            should_check_browser_session_artifacts=AsyncMock(return_value=False),
            role_allows=AsyncMock(return_value=True),
        ),
    )
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)
    principal = SimpleNamespace(organization_id=ORG_ID, api_key_access_mode="member")
    monkeypatch.setattr(agent_protocol, "get_request_principal", lambda: principal)

    async with _client(shared_route_app(monkeypatch)) as client:
        response = await client.get("/api/v1/workflows/runs")

    assert response.status_code == 200, response.text
    assert response.json()[0]["browser_session_id"] == session_id
    assert response.json()[0]["browser_address"] == (address if connect_authorized else None)
    authorization.assert_awaited_once_with(principal, ORG_ID, {session_id})


@pytest.mark.asyncio
async def test_workflow_run_detail_respects_connect_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = "pbs_test_workflow_run_detail"
    address = f"wss://router.test/{session_id}?token=secret"
    response = WorkflowRunResponseBase.model_construct(
        workflow_run_id="wr_test_workflow_run_detail",
        workflow_id="wpid_test_workflow_run_detail",
        status=WorkflowRunStatus.running,
        browser_session_id=session_id,
        browser_address=address,
    )
    authorization = AsyncMock(return_value=set())
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(
        agent_protocol.run_service,
        "coalesce_in_flight",
        AsyncMock(return_value=response),
    )
    monkeypatch.setattr(
        app_instance,
        "AGENT_FUNCTION",
        SimpleNamespace(browser_session_ids_authorized_for_connect=authorization),
    )
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)
    principal = SimpleNamespace(organization_id=ORG_ID, api_key_access_mode="member")
    monkeypatch.setattr(agent_protocol, "get_request_principal", lambda: principal)

    result = await agent_protocol.get_workflow_run(
        response.workflow_run_id, current_org=SimpleNamespace(organization_id=ORG_ID)
    )

    assert result.browser_address is None
    authorization.assert_awaited_once_with(principal, ORG_ID, {session_id})


def _client(fastapi_app: FastAPI, organization_id: str = ORG_ID) -> httpx.AsyncClient:
    now = datetime.now(UTC)
    organization = Organization(
        organization_id=organization_id, organization_name="Listing", created_at=now, modified_at=now
    )
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = lambda: organization
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test")


async def _seed_tasks_with_timestamp_ties(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, count: int
) -> list[str]:
    db = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    task_ids = []
    for index in range(count):
        task = await db.tasks.create_task(
            url="https://example.com",
            # Characters str.splitlines() treats as line breaks; the export must keep each record on one line.
            title=f"Task {index} \u2028\u2029\x85",
            navigation_goal="Find the pricing page",
            data_extraction_goal=None,
            navigation_payload=None,
            organization_id=ORG_ID,
        )
        task_ids.append(task.task_id)
    # Each sort column gets a few shared values, in an order unlike creation, so page boundaries fall inside a tie.
    async with db.Session() as session:
        for index, task_id in enumerate(task_ids):
            await session.execute(
                update(TaskModel)
                .where(TaskModel.task_id == task_id)
                .values(
                    created_at=datetime(2026, 10, 1 + index % 2, 12, 0, 0),
                    modified_at=datetime(2026, 10, 5 + index % 3, 12, 0, 0),
                )
            )
        await session.commit()
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(app_instance, "DATABASE", db)
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)
    return task_ids


async def _walk_tasks(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]
) -> tuple[list[int], list[dict[str, Any]]]:
    page_lengths: list[int] = []
    walked: list[dict[str, Any]] = []
    cursor = ""
    async with _client(shared_route_app(monkeypatch)) as client:
        for _ in range(10):
            response = await client.get("/api/v1/tasks", params={**params, "cursor": cursor})
            assert response.status_code == 200, response.text
            page_lengths.append(len(response.json()))
            walked.extend(response.json())
            if agent_protocol.TASK_NEXT_CURSOR_HEADER not in response.headers:
                break
            cursor = response.headers[agent_protocol.TASK_NEXT_CURSOR_HEADER]
    return page_lengths, walked


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["desc", "asc"])
async def test_task_cursor_walk_returns_every_task_once_across_timestamp_ties(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, order: str
) -> None:
    task_ids = await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=7)

    page_lengths, walked = await _walk_tasks(monkeypatch, {"page_size": 2, "order": order})

    assert page_lengths == [2, 2, 2, 1]
    keys = [(task["created_at"], task["task_id"]) for task in walked]
    assert keys == sorted(keys, reverse=order == "desc")
    assert len(keys) == len(set(keys)) == len(task_ids)


@pytest.mark.asyncio
async def test_task_cursor_walk_by_modified_at_ends_on_an_exactly_full_page(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    task_ids = await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=6)

    page_lengths, walked = await _walk_tasks(monkeypatch, {"page_size": 2, "sort": "modified_at"})

    assert page_lengths == [2, 2, 2]
    keys = [(task["modified_at"], task["task_id"]) for task in walked]
    assert keys == sorted(keys, reverse=True)
    assert len(set(keys)) == len(task_ids)


@pytest.mark.asyncio
async def test_task_cursor_reused_with_different_filters_is_rejected(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=3)
    fastapi_app = shared_route_app(monkeypatch)

    async with _client(fastapi_app) as client:
        first = await client.get("/api/v1/tasks", params={"page_size": 1, "cursor": ""})
        cursor = first.headers[agent_protocol.TASK_NEXT_CURSOR_HEADER]
        reordered = await client.get("/api/v1/tasks", params={"page_size": 1, "cursor": cursor, "order": "asc"})
        refiltered = await client.get(
            "/api/v1/tasks", params={"page_size": 1, "cursor": cursor, "only_standalone_tasks": True}
        )
        resorted = await client.get("/api/v1/tasks", params={"page_size": 1, "cursor": cursor, "sort": "modified_at"})
        status_filtered = await client.get(
            "/api/v1/tasks", params={"page_size": 1, "cursor": cursor, "task_status": "created"}
        )
        paged = await client.get("/api/v1/tasks", params={"page_size": 1, "cursor": cursor, "page": 2})
        garbage = await client.get("/api/v1/tasks", params={"page_size": 1, "cursor": "not-a-cursor"})
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        payload["after"][1] = "tsk_1\u0000"
        forged_cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        forged = await client.get("/api/v1/tasks", params={"page_size": 1, "cursor": forged_cursor})
    async with _client(fastapi_app, "o_other_org") as other_org_client:
        other_org = await other_org_client.get("/api/v1/tasks", params={"page_size": 1, "cursor": cursor})

    responses = [reordered, refiltered, resorted, status_filtered, paged, garbage, forged, other_org]
    assert [response.status_code for response in responses] == [400] * len(responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [6, 7])
async def test_task_export_streams_every_task_once_across_batch_boundaries(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, count: int
) -> None:
    task_ids = await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=count)
    monkeypatch.setattr(agent_protocol, "TASK_EXPORT_BATCH_SIZE", 2)

    async with _client(shared_route_app(monkeypatch)) as client:
        response = await client.get("/api/v1/tasks/export")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/x-ndjson"
    *lines, trailer = (json.loads(line) for line in response.text.splitlines())
    assert trailer == {"export_complete": True, "task_count": count}
    exported = [line for line in lines if "task_id" in line]
    assert len(lines) - len(exported) == count // 2
    keys = [(task["created_at"], task["task_id"]) for task in exported]
    assert keys == sorted(keys, reverse=True)
    assert sorted(task["task_id"] for task in exported) == sorted(task_ids)


@pytest.mark.asyncio
async def test_task_export_resumes_from_its_last_checkpoint(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    task_ids = await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=7)
    monkeypatch.setattr(agent_protocol, "TASK_EXPORT_BATCH_SIZE", 2)

    async with _client(shared_route_app(monkeypatch)) as client:
        full = [json.loads(line) for line in (await client.get("/api/v1/tasks/export")).text.splitlines()]
        # Simulate a drop after the second checkpoint: keep tasks up to it, resume from its cursor.
        checkpoints = [index for index, line in enumerate(full) if "cursor" in line]
        kept = full[: checkpoints[1]]
        resumed_response = await client.get("/api/v1/tasks/export", params={"cursor": full[checkpoints[1]]["cursor"]})
        resumed = [json.loads(line) for line in resumed_response.text.splitlines()]

    exported_ids = [line["task_id"] for line in kept + resumed if "task_id" in line]
    assert sorted(exported_ids) == sorted(task_ids)
    assert len(exported_ids) == len(set(exported_ids))
    assert resumed[-1] == {"export_complete": True, "task_count": 3}


@pytest.mark.asyncio
async def test_task_export_applies_filters_and_builds_batches_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine
) -> None:
    task_ids = await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=3)
    db = object.__getattribute__(agent_protocol.app, "_inst").DATABASE
    async with db.Session() as session:
        await session.execute(update(TaskModel).where(TaskModel.task_id == task_ids[0]).values(status="completed"))
        await session.commit()
    probe = _LoopProbe()
    monkeypatch.setattr(tasks_repository, "convert_to_task", probe.gate("convert", tasks_repository.convert_to_task))
    monkeypatch.setattr(Task, "to_task_response", probe.gate("serialize", Task.to_task_response))

    ticker = asyncio.create_task(probe.tick_forever())
    try:
        async with _client(shared_route_app(monkeypatch)) as client:
            response = await client.get("/api/v1/tasks/export", params={"task_status": "created"})
    finally:
        ticker.cancel()

    assert response.status_code == 200, response.text
    assert probe.loop_ran == {"convert": True, "serialize": True}
    *exported, trailer = (json.loads(line) for line in response.text.splitlines())
    assert sorted(task["task_id"] for task in exported) == sorted(task_ids[1:])
    assert trailer == {"export_complete": True, "task_count": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["list", "export"])
async def test_task_listing_checks_descendants_once_per_run_per_request(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, endpoint: str
) -> None:
    from fastapi import HTTPException

    import skyvern.forge.agent as agent_module
    from skyvern.forge.sdk.services import browser_session_artifacts as artifacts
    from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal

    task_ids = await _seed_tasks_with_timestamp_ties(monkeypatch, sqlite_engine, count=12)
    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    db = app_instance.DATABASE
    async with db.Session() as session:
        for index, task_id in enumerate(task_ids):
            await session.execute(
                update(TaskModel)
                .where(TaskModel.task_id == task_id)
                .values(
                    workflow_run_id="wr_a" if index < 6 else "wr_b",
                    browser_session_id="pbs_denied" if index == 0 else "pbs_both_denied" if index == 6 else None,
                    extracted_information={"downloaded_file_urls": ["file:///tmp/private.pdf"]},
                )
            )
        await session.commit()

    async def children(parent_id: str, *, organization_id: str) -> list[SimpleNamespace]:
        await asyncio.sleep(0.01)
        return [SimpleNamespace(workflow_run_id="wr_hidden")] if parent_id == "wr_b" else []

    async def folder_access(principal: RequestPrincipal, action: str, resource: dict[str, object]) -> None:
        if principal.user_id == "viewer":
            raise HTTPException(status_code=403)

    async def task_access(
        _org: str, *, session_ids: list[str | None], **_kwargs: object
    ) -> artifacts.BrowserSessionArtifactAccess:
        return artifacts.BrowserSessionArtifactAccess(
            "pbs_denied" not in session_ids
            and not ("pbs_both_denied" in session_ids and principal["value"].user_id == "viewer")
        )

    lookup = AsyncMock(side_effect=children)
    root_access = AsyncMock(side_effect=task_access)
    monkeypatch.setattr(db.workflow_runs, "get_workflow_runs_by_parent_workflow_run_id", lookup)
    monkeypatch.setattr(db.observer, "get_child_workflow_run_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        app_instance.AGENT_FUNCTION, "should_check_browser_session_artifacts", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(app_instance.AGENT_FUNCTION, "enforce_folder_access", folder_access)
    monkeypatch.setattr(agent_module, "browser_session_artifact_access", root_access)
    monkeypatch.setattr(
        artifacts,
        "browser_session_artifact_access",
        AsyncMock(return_value=artifacts.BrowserSessionArtifactAccess(True)),
    )
    monkeypatch.setattr(app_instance, "agent", ForgeAgent())
    monkeypatch.setattr(agent_protocol, "TASK_EXPORT_BATCH_SIZE", 3)
    principal = {"value": RequestPrincipal(ORG_ID, AuthKind.bearer, user_id="viewer")}
    monkeypatch.setattr(agent_protocol, "get_request_principal", lambda: principal["value"])

    def rows(response: httpx.Response) -> list[dict[str, Any]]:
        if endpoint == "list":
            return response.json()
        return [row for line in response.text.splitlines() if "task_id" in (row := json.loads(line))]

    path = "/api/v1/tasks" if endpoint == "list" else "/api/v1/tasks/export"
    async with _client(shared_route_app(monkeypatch)) as client:
        denied = await client.get(path, params={"page_size": 100} if endpoint == "list" else None)
        assert denied.status_code == 200, denied.text
        assert [call.args[0] for call in lookup.await_args_list].count("wr_a") == 1
        assert [call.args[0] for call in lookup.await_args_list].count("wr_b") == 1
        assert root_access.await_count == len(task_ids)
        denied_by_id = {row["task_id"]: row for row in rows(denied)}
        assert denied_by_id[task_ids[0]]["extracted_information"]["downloaded_file_urls"] is None
        assert denied_by_id[task_ids[1]]["extracted_information"]["downloaded_file_urls"]
        assert denied_by_id[task_ids[6]]["extracted_information"] == "[redacted]"

        lookup.reset_mock()
        root_access.reset_mock()
        principal["value"] = RequestPrincipal(ORG_ID, AuthKind.bearer, user_id="editor")
        allowed = await client.get(path, params={"page_size": 100} if endpoint == "list" else None)
        assert allowed.status_code == 200, allowed.text
        assert [call.args[0] for call in lookup.await_args_list].count("wr_a") == 1
        assert [call.args[0] for call in lookup.await_args_list].count("wr_b") == 1
        assert root_access.await_count == len(task_ids)
        allowed_by_id = {row["task_id"]: row for row in rows(allowed)}
        assert allowed_by_id[task_ids[0]]["extracted_information"]["downloaded_file_urls"] is None
        assert allowed_by_id[task_ids[6]]["extracted_information"]["downloaded_file_urls"]
