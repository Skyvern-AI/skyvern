from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.workflows import BlockStatus, BlockType

ORG_ID = "o_timeline"
OTHER_ORG_ID = "o_timeline_other"


@dataclass
class _Harness:
    client: httpx.AsyncClient
    workflow_permanent_id: str
    run_ids: dict[str, str]
    statements: list[str]

    async def get(self, path: str) -> tuple[httpx.Response, int]:
        self.statements.clear()
        response = await self.client.get(path)
        return response, len(self.statements)


async def _seed_workflow_run(db: AgentDB, organization_id: str, block_label: str) -> tuple[str, str]:
    workflow = await db.workflows.create_workflow(
        title="Timeline", workflow_definition={"parameters": [], "blocks": []}, organization_id=organization_id
    )
    workflow_run = await db.workflow_runs.create_workflow_run(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id=organization_id,
    )
    await db.observer.create_workflow_run_block(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=organization_id,
        label=block_label,
        block_type=BlockType.TEXT_PROMPT,
        status=BlockStatus.completed,
    )
    return workflow.workflow_permanent_id, workflow_run.workflow_run_id


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine) -> AsyncIterator[_Harness]:
    db = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)

    workflow_permanent_id, workflow_run_id = await _seed_workflow_run(db, ORG_ID, "navigate")
    await db.tasks.create_task_run(task_run_type=RunType.workflow_run, organization_id=ORG_ID, run_id=workflow_run_id)

    _, task_v2_workflow_run_id = await _seed_workflow_run(db, ORG_ID, "plan")
    task_v2 = await db.observer.create_task_v2(
        workflow_run_id=task_v2_workflow_run_id, organization_id=ORG_ID, prompt="Find the pricing page"
    )
    await db.observer.create_thought(
        task_v2_id=task_v2.observer_cruise_id,
        workflow_run_id=task_v2_workflow_run_id,
        organization_id=ORG_ID,
        thought="Open the pricing link",
    )
    await db.tasks.create_task_run(
        task_run_type=RunType.task_v2, organization_id=ORG_ID, run_id=task_v2.observer_cruise_id
    )

    task = await db.tasks.create_task(
        url="https://example.com",
        title=None,
        navigation_goal="Find the pricing page",
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=ORG_ID,
    )
    await db.tasks.create_task_run(task_run_type=RunType.task_v1, organization_id=ORG_ID, run_id=task.task_id)

    _, foreign_workflow_run_id = await _seed_workflow_run(db, OTHER_ORG_ID, "secret")
    await db.tasks.create_task_run(
        task_run_type=RunType.workflow_run, organization_id=OTHER_ORG_ID, run_id=foreign_workflow_run_id
    )

    app_instance = object.__getattribute__(agent_protocol.app, "_inst")
    monkeypatch.setattr(app_instance, "DATABASE", db)
    monkeypatch.setattr(app_instance, "WORKFLOW_SERVICE", WorkflowService())
    # A real agent builds a valid response for the stepless task, so a handler that builds it still answers 400.
    monkeypatch.setattr(app_instance, "agent", ForgeAgent())
    monkeypatch.setattr(agent_protocol.analytics, "capture", lambda *args, **kwargs: None)

    now = datetime.now(UTC)
    organization = Organization(organization_id=ORG_ID, organization_name="Timeline", created_at=now, modified_at=now)
    fastapi_app = FastAPI()
    fastapi_app.include_router(base_router, prefix="/v1")
    fastapi_app.include_router(legacy_base_router, prefix="/api/v1")
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = lambda: organization

    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(sqlite_engine.sync_engine, "before_cursor_execute", record)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://test") as client:
            yield _Harness(
                client=client,
                workflow_permanent_id=workflow_permanent_id,
                run_ids={
                    "workflow_run": workflow_run_id,
                    "task_v2": task_v2.observer_cruise_id,
                    "task_v2_workflow_run": task_v2_workflow_run_id,
                    "task_v1": task.task_id,
                    "foreign_workflow_run": foreign_workflow_run_id,
                    "unknown": "wr_does_not_exist",
                },
                statements=statements,
            )
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "timeline_of", "item_types"),
    [
        ("workflow_run", "workflow_run", {"block"}),
        ("task_v2", "task_v2_workflow_run", {"block", "thought"}),
        # A task v2's own workflow run id has no task_runs row; it resolves through the task v2.
        ("task_v2_workflow_run", "task_v2_workflow_run", {"block", "thought"}),
    ],
)
async def test_run_timeline_matches_the_workflow_run_timeline_plus_one_run_lookup(
    harness: _Harness, kind: str, timeline_of: str, item_types: set[str]
) -> None:
    expected, timeline_statements = await harness.get(
        f"/api/v1/workflows/{harness.workflow_permanent_id}/runs/{harness.run_ids[timeline_of]}/timeline"
    )
    assert expected.status_code == 200, expected.text
    # The thought comes from the task v2 the handler hands to the builder, so the body check covers it.
    assert {item["type"] for item in expected.json()} == item_types

    response, statements = await harness.get(f"/v1/runs/{harness.run_ids[kind]}/timeline")

    assert response.status_code == 200, response.text
    assert response.json() == expected.json()
    # The run type comes from task_runs, and the task v2 found while resolving it is not read again.
    # Building the run's full status response would add ~20 reads and S3 calls.
    assert statements <= timeline_statements + 1, harness.statements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status_code", "max_statements"),
    [
        ("task_v1", 400, 1),
        ("foreign_workflow_run", 404, 2),
        ("unknown", 404, 2),
    ],
)
async def test_run_timeline_rejects_without_reading_the_run(
    harness: _Harness, kind: str, status_code: int, max_statements: int
) -> None:
    response, statements = await harness.get(f"/v1/runs/{harness.run_ids[kind]}/timeline")

    assert response.status_code == status_code, response.text
    assert statements <= max_statements, harness.statements
