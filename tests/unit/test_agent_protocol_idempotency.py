import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from skyvern.forge import app
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base, WorkflowModel
from skyvern.forge.sdk.db.repositories import workflows as workflows_repository
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.exceptions import FailedToCreateWorkflow
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import WorkflowDefinitionYAML, WorkflowRequest
from tests.unit.force_stub_app import start_forge_stub_app

start_forge_stub_app()

WORKFLOW_CREATE_PAYLOAD = {
    "json_definition": {
        "title": "Replay test agent",
        "workflow_definition": {
            "parameters": [],
            "blocks": [{"label": "visit_page", "block_type": "task", "url": "https://example.com"}],
        },
    }
}


@pytest.mark.asyncio
async def test_create_workflow_honors_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    workflow_service = WorkflowService()
    creation_started = asyncio.Event()
    allow_creation_to_finish = asyncio.Event()
    replay_reached_creation = asyncio.Event()
    make_workflow_definition = workflow_service.make_workflow_definition
    workflow_creation_lock = getattr(database.workflows, "acquire_workflow_creation_lock", None)
    definition_calls = 0
    lock_calls = 0

    if workflow_creation_lock is not None:

        @asynccontextmanager
        async def tracked_workflow_creation_lock(lock_key: str) -> AsyncIterator[None]:
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                replay_reached_creation.set()
            async with workflow_creation_lock(lock_key):
                yield

        monkeypatch.setattr(database.workflows, "acquire_workflow_creation_lock", tracked_workflow_creation_lock)

    async def delayed_make_workflow_definition(
        workflow_id: str,
        workflow_definition_yaml: WorkflowDefinitionYAML,
    ) -> WorkflowDefinition:
        nonlocal definition_calls
        definition_calls += 1
        if definition_calls == 1:
            creation_started.set()
        else:
            replay_reached_creation.set()
        await allow_creation_to_finish.wait()
        return await make_workflow_definition(workflow_id, workflow_definition_yaml)

    monkeypatch.setattr(workflow_service, "make_workflow_definition", delayed_make_workflow_definition)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = lambda: organization
    fastapi_app.dependency_overrides[org_auth_service.get_current_user_id_or_none] = lambda: "u_test"
    fastapi_app.include_router(agent_protocol.base_router, prefix="/v1")
    try:
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            original_request = asyncio.create_task(
                client.post(
                    "/v1/agents",
                    headers={"Idempotency-Key": "replay-key"},
                    json=WORKFLOW_CREATE_PAYLOAD,
                )
            )
            await asyncio.wait_for(creation_started.wait(), timeout=5)
            replay_request = asyncio.create_task(
                client.post(
                    "/v1/agents",
                    headers={"Idempotency-Key": "replay-key"},
                    json=WORKFLOW_CREATE_PAYLOAD,
                )
            )
            await asyncio.wait_for(replay_reached_creation.wait(), timeout=5)
            allow_creation_to_finish.set()
            original, replay = await asyncio.gather(original_request, replay_request)

            assert original.status_code == 200
            assert replay.status_code == 200
            assert replay.json() == original.json()
            assert len(original.json()["workflow_definition"]["blocks"]) == 1
            assert replay.json()["workflow_id"] == original.json()["workflow_id"]
            assert replay.json()["workflow_permanent_id"] == original.json()["workflow_permanent_id"]
            async with database.Session() as session:
                active_row_count = await session.scalar(
                    select(func.count())
                    .select_from(WorkflowModel)
                    .where(WorkflowModel.organization_id == organization.organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                )
            assert active_row_count == 1

            distinct = await client.post(
                "/v1/agents",
                headers={"Idempotency-Key": "distinct-key"},
                json=WORKFLOW_CREATE_PAYLOAD,
            )

            assert distinct.status_code == 200
            assert distinct.json()["workflow_id"] != original.json()["workflow_id"]
            assert distinct.json()["workflow_permanent_id"] != original.json()["workflow_permanent_id"]
            async with database.Session() as session:
                active_row_count = await session.scalar(
                    select(func.count())
                    .select_from(WorkflowModel)
                    .where(WorkflowModel.organization_id == organization.organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                )
            assert active_row_count == 2

            await workflow_service.delete_workflow_by_permanent_id(
                original.json()["workflow_permanent_id"],
                organization.organization_id,
            )
            deleted_replay = await client.post(
                "/v1/agents",
                headers={"Idempotency-Key": "replay-key"},
                json=WORKFLOW_CREATE_PAYLOAD,
            )
            blank_one = await client.post(
                "/v1/agents",
                headers={"Idempotency-Key": "   "},
                json=WORKFLOW_CREATE_PAYLOAD,
            )
            blank_two = await client.post(
                "/v1/agents",
                headers={"Idempotency-Key": "   "},
                json=WORKFLOW_CREATE_PAYLOAD,
            )

        assert deleted_replay.status_code == 200
        assert deleted_replay.json()["workflow_id"] == original.json()["workflow_id"]
        assert blank_one.status_code == 200
        assert blank_two.status_code == 200
        assert blank_one.json()["workflow_id"] != blank_two.json()["workflow_id"]
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_workflow_idempotency_wait_deadline_returns_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-timeout.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = lambda: organization
    fastapi_app.dependency_overrides[org_auth_service.get_current_user_id_or_none] = lambda: "u_test"
    fastapi_app.include_router(agent_protocol.base_router, prefix="/v1")

    try:
        transport = httpx.ASGITransport(app=fastapi_app)
        async with database.workflows.acquire_workflow_creation_lock("held"):
            monkeypatch.setattr(
                workflows_repository,
                "WORKFLOW_CREATION_LOCK_TIMEOUT_SECONDS",
                0,
                raising=False,
            )
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await asyncio.wait_for(
                    client.post(
                        "/v1/agents",
                        headers={"Idempotency-Key": "blocked-key"},
                        json=WORKFLOW_CREATE_PAYLOAD,
                    ),
                    timeout=1,
                )

        assert response.status_code == 409
        assert response.json() == {"detail": "Workflow creation with this idempotency key is still in progress."}
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_workflow_idempotency_database_timeout_is_not_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-database-timeout.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )

    @asynccontextmanager
    async def database_timeout(_lock_key: str | None = None) -> AsyncIterator[None]:
        raise TimeoutError("database timed out")
        yield

    monkeypatch.setattr(database.workflows, "_workflow_creation_transaction", database_timeout)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())

    try:
        with pytest.raises(FailedToCreateWorkflow, match="database timed out"):
            await agent_protocol.create_workflow(
                data=WorkflowRequest.model_validate(WORKFLOW_CREATE_PAYLOAD),
                folder_id=None,
                current_org=organization,
                user_id="u_test",
                idempotency_key="database-timeout-key",
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_workflow_idempotency_failure_does_not_record_first_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-rollback.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    workflow_service = WorkflowService()
    hook_started = asyncio.Event()
    first_save_at: object | None = None

    async def record_first_save_at(*, organization_id: str, edited_by: str | None) -> None:
        nonlocal first_save_at
        first_save_at = object()
        hook_started.set()

    async def fail_cache_invalidation(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.wait_for(hook_started.wait(), timeout=0.1)
        except TimeoutError:
            pass
        raise RuntimeError("cache invalidation failed")

    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_workflow_saved", record_first_save_at)
    monkeypatch.setattr(workflow_service, "maybe_delete_cached_code", fail_cache_invalidation)

    try:
        with pytest.raises(FailedToCreateWorkflow, match="cache invalidation failed"):
            await agent_protocol.create_workflow(
                data=WorkflowRequest.model_validate(WORKFLOW_CREATE_PAYLOAD),
                folder_id=None,
                current_org=organization,
                user_id="u_test",
                idempotency_key="rollback-key",
            )

        assert first_save_at is None
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_workflow_idempotency_generates_title_before_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-title.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    workflow_service = WorkflowService()
    workflow_creation_lock = database.workflows.acquire_workflow_creation_lock
    events: list[str] = []

    async def generate_title(*_args: object, **_kwargs: object) -> str:
        events.append("title")
        return "Generated title"

    @asynccontextmanager
    async def tracked_workflow_creation_lock(lock_key: str) -> AsyncIterator[None]:
        events.append("lock")
        async with workflow_creation_lock(lock_key):
            yield

    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(database.workflows, "acquire_workflow_creation_lock", tracked_workflow_creation_lock)
    monkeypatch.setattr("skyvern.forge.sdk.workflow.service.generate_workflow_title", generate_title)
    request = WorkflowRequest.model_validate(WORKFLOW_CREATE_PAYLOAD)
    assert request.json_definition is not None
    request.json_definition.title = "New Agent"

    try:
        workflow = await agent_protocol.create_workflow(
            data=request,
            folder_id=None,
            current_org=organization,
            user_id="u_test",
            idempotency_key="title-key",
        )

        assert workflow.title == "Generated title"
        assert events == ["title", "lock"]
    finally:
        await database.engine.dispose()
