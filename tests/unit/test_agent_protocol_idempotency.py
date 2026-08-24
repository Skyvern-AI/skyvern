import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select

from skyvern.forge import app
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base, OutputParameterModel, WorkflowModel
from skyvern.forge.sdk.db.repositories import workflows as workflows_repository
from skyvern.forge.sdk.routes import agent_protocol
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.exceptions import FailedToCreateWorkflow
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import WorkflowRequest
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

IDEMPOTENCY_KEY_ERROR_DETAIL = "Idempotency-Key must contain 1 to 255 visible ASCII bytes."

REJECTED_IDEMPOTENCY_KEYS = {
    "empty": "",
    "single_space": " ",
    "whitespace_only": "   ",
    "tab": "\t",
    "trailing_newline": "replay-key\n",
    "embedded_newline": "replay\nkey",
    "control_byte": "replay\x07key",
    "delete_byte": "replay\x7fkey",
    "non_ascii": "replay-kéy",
    "256_bytes": "a" * 256,
    "1024_bytes": "a" * 1024,
    "20000_bytes": "a" * 20_000,
}


@dataclass
class IdempotencyLab:
    """Loopback create-workflow router over synthetic SQLite with every post-validation seam counted."""

    client: httpx.AsyncClient
    database: AgentDB
    organization: Organization
    calls: dict[str, int] = field(default_factory=dict)

    async def create(self, key: str | None = None) -> httpx.Response:
        # Raw latin-1 bytes: httpx refuses non-ASCII str header values, but the ASGI layer decodes
        # header bytes as latin-1, so this reproduces exactly what reaches the route on the wire.
        headers = [] if key is None else [(b"Idempotency-Key", key.encode("latin-1"))]
        return await self.client.post("/v1/agents", headers=headers, json=WORKFLOW_CREATE_PAYLOAD)

    async def count_workflows(self) -> int | None:
        async with self.database.Session() as session:
            return await session.scalar(
                select(func.count())
                .select_from(WorkflowModel)
                .where(WorkflowModel.organization_id == self.organization.organization_id)
            )


@asynccontextmanager
async def idempotency_lab(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
) -> AsyncIterator[IdempotencyLab]:
    database = AgentDB(f"sqlite+aiosqlite:///{database_path}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    workflow_service = WorkflowService()
    calls: dict[str, int] = {}

    def track(seam: str, original: object) -> object:
        async def tracked(*args: object, **kwargs: object) -> object:
            calls[seam] = calls.get(seam, 0) + 1
            return await original(*args, **kwargs)  # type: ignore[operator]

        return tracked

    calculate_sha256 = agent_protocol.calculate_sha256
    acquire_lock = database.workflows.acquire_workflow_creation_lock

    def tracked_capture(*args: object, **kwargs: object) -> None:
        calls["analytics"] = calls.get("analytics", 0) + 1

    def tracked_hash(value: str) -> str:
        calls["hash"] = calls.get("hash", 0) + 1
        return calculate_sha256(value)

    @asynccontextmanager
    async def counted_lock(lock_key: str) -> AsyncIterator[None]:
        calls["lock"] = calls.get("lock", 0) + 1
        async with acquire_lock(lock_key):
            yield

    monkeypatch.setattr(agent_protocol.analytics, "capture", tracked_capture)
    monkeypatch.setattr(agent_protocol, "calculate_sha256", tracked_hash)
    monkeypatch.setattr(database.workflows, "acquire_workflow_creation_lock", counted_lock)
    for seam, name in (
        ("lookup", "get_workflow_by_permanent_id"),
        ("title", "resolve_workflow_creation_title"),
        ("create", "create_workflow_from_request"),
    ):
        monkeypatch.setattr(workflow_service, name, track(seam, getattr(workflow_service, name)))
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)

    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[org_auth_service.get_current_org] = lambda: organization
    fastapi_app.dependency_overrides[org_auth_service.get_current_user_id_or_none] = lambda: "u_test"
    fastapi_app.include_router(agent_protocol.base_router, prefix="/v1")

    transport = httpx.ASGITransport(app=fastapi_app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield IdempotencyLab(
                client=client,
                database=database,
                organization=organization,
                calls=calls,
            )
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(REJECTED_IDEMPOTENCY_KEYS))
async def test_create_workflow_rejects_malformed_idempotency_key_before_any_work(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    key = REJECTED_IDEMPOTENCY_KEYS[case]
    async with idempotency_lab(monkeypatch, tmp_path / f"reject-{case}.db") as lab:
        response = await lab.create(key)

        assert response.status_code == 422
        assert response.json() == {"detail": IDEMPOTENCY_KEY_ERROR_DETAIL}
        if len(key) > 16:
            assert key not in response.text
        assert await lab.count_workflows() == 0
        assert lab.calls == {}


def test_validate_idempotency_key_rejects_multibyte_by_encoded_length() -> None:
    # 128 characters, 256 UTF-8 bytes: a character-count bound would accept this. ASGI decodes
    # header bytes as latin-1, so only a non-HTTP caller can hand the validator such a string.
    # The Depends() wiring itself (alias, 422, detail body) stays covered end-to-end by
    # test_create_workflow_rejects_malformed_idempotency_key_before_any_work below.
    key = "é" * 128
    assert len(key) < 255 < len(key.encode("utf-8"))

    with pytest.raises(HTTPException) as rejection:
        agent_protocol.validate_idempotency_key(key)

    assert rejection.value.status_code == 422
    assert rejection.value.detail == IDEMPOTENCY_KEY_ERROR_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [None, "a", "a" * 255],
    ids=["missing_header", "1_byte", "255_bytes"],
)
async def test_create_workflow_accepts_idempotency_key_within_bounds(
    key: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async with idempotency_lab(monkeypatch, tmp_path / f"accept-{len(key or '')}.db") as lab:
        created = await lab.create(key)
        replayed = await lab.create(key)

        assert created.status_code == 200
        assert replayed.status_code == 200
        if key is None:
            assert replayed.json()["workflow_permanent_id"] != created.json()["workflow_permanent_id"]
            assert await lab.count_workflows() == 2
            assert set(lab.calls) == {"analytics", "title", "create"}
        else:
            assert replayed.json()["workflow_permanent_id"] == created.json()["workflow_permanent_id"]
            assert await lab.count_workflows() == 1
            # Counters the rejection tests assert are empty; an accepted key must trip every one.
            assert set(lab.calls) == {"analytics", "hash", "lookup", "title", "lock", "create"}


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
    create_workflow = database.workflows.create_workflow
    workflow_creation_lock = getattr(database.workflows, "acquire_workflow_creation_lock", None)
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

    async def delayed_create_workflow(*args: Any, **kwargs: Any) -> Any:
        creation_started.set()
        await allow_creation_to_finish.wait()
        return await create_workflow(*args, **kwargs)

    monkeypatch.setattr(database.workflows, "create_workflow", delayed_create_workflow)
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

        assert deleted_replay.status_code == 200
        assert deleted_replay.json()["workflow_id"] == original.json()["workflow_id"]
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
                # The lock deadline under test is WORKFLOW_CREATION_LOCK_TIMEOUT_SECONDS=0
                # (set above) — the endpoint answers 409 immediately. This outer wait_for is
                # only a hang guard for a broken deadline path; 1s of wall clock proved too
                # tight for a loaded CI shard (full-app ASGI + sqlite setup), so keep the
                # guard generous rather than timing-sensitive.
                response = await asyncio.wait_for(
                    client.post(
                        "/v1/agents",
                        headers={"Idempotency-Key": "blocked-key"},
                        json=WORKFLOW_CREATE_PAYLOAD,
                    ),
                    timeout=10,
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
async def test_create_workflow_idempotency_post_commit_failure_preserves_workflow(
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

    async def record_first_save_at(
        *,
        organization_id: str,
        edited_by: str | None,
        workflow_permanent_id: str,
    ) -> None:
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

        assert first_save_at is not None
        async with database.Session() as session:
            persisted_workflow = (
                await session.scalars(
                    select(WorkflowModel)
                    .where(WorkflowModel.organization_id == organization.organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                )
            ).one()
        assert len(persisted_workflow.workflow_definition["blocks"]) == 1

        replay = await agent_protocol.create_workflow(
            data=WorkflowRequest.model_validate(WORKFLOW_CREATE_PAYLOAD),
            folder_id=None,
            current_org=organization,
            user_id="u_test",
            idempotency_key="rollback-key",
        )
        assert replay.workflow_id == persisted_workflow.workflow_id
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


@pytest.mark.asyncio
async def test_create_workflow_idempotency_releases_lock_before_post_commit_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-lock-order.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    workflow_service = WorkflowService()
    workflow_creation_lock = database.workflows.acquire_workflow_creation_lock
    validate_parameters = workflow_service._validate_and_normalize_credential_rotation_parameters
    events: list[str] = []

    async def tracked_validation(parameters: list[object], tracked_organization: Organization) -> None:
        events.append("pre_write")
        await validate_parameters(parameters, tracked_organization)

    @asynccontextmanager
    async def tracked_workflow_creation_lock(lock_key: str) -> AsyncIterator[None]:
        events.append("lock_enter")
        async with workflow_creation_lock(lock_key):
            yield
        events.append("lock_exit")

    async def tracked_post_commit(*_args: object, **_kwargs: object) -> None:
        events.append("post_commit")
        async with database.Session() as session:
            persisted_workflow = (
                await session.scalars(
                    select(WorkflowModel)
                    .where(WorkflowModel.organization_id == organization.organization_id)
                    .where(WorkflowModel.deleted_at.is_(None))
                )
            ).one()
        assert len(persisted_workflow.workflow_definition["blocks"]) == 1

    def tracked_saved_hook(
        *,
        organization_id: str,
        edited_by: str | None,
        workflow_permanent_id: str,
    ) -> None:
        events.append("saved_hook")

    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)
    monkeypatch.setattr(workflow_service, "_validate_and_normalize_credential_rotation_parameters", tracked_validation)
    monkeypatch.setattr(database.workflows, "acquire_workflow_creation_lock", tracked_workflow_creation_lock)
    monkeypatch.setattr(workflow_service, "maybe_delete_cached_code", tracked_post_commit)
    monkeypatch.setattr(workflow_service, "schedule_workflow_saved_hook", tracked_saved_hook)

    try:
        await agent_protocol.create_workflow(
            data=WorkflowRequest.model_validate(WORKFLOW_CREATE_PAYLOAD),
            folder_id=None,
            current_org=organization,
            user_id="u_test",
            idempotency_key="lock-order-key",
        )

        assert events == ["pre_write", "lock_enter", "lock_exit", "saved_hook", "post_commit"]
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_create_workflow_idempotency_definition_failure_rolls_back_atomic_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = AgentDB(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-write-rollback.db'}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    organization = await database.organizations.create_organization(
        organization_name="Test",
        organization_id="o_test",
    )
    save_definition_parameters = database.workflow_params.save_workflow_definition_parameters
    hook_scheduled = False

    async def fail_definition_write(parameters: list[Any]) -> None:
        await save_definition_parameters(parameters)
        raise RuntimeError("definition write failed")

    def record_saved_hook(
        *,
        organization_id: str,
        edited_by: str | None,
        workflow_permanent_id: str,
    ) -> None:
        nonlocal hook_scheduled
        hook_scheduled = True

    monkeypatch.setattr(database.workflow_params, "save_workflow_definition_parameters", fail_definition_write)
    monkeypatch.setattr(app, "DATABASE", database)
    workflow_service = WorkflowService()
    monkeypatch.setattr(workflow_service, "schedule_workflow_saved_hook", record_saved_hook)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", workflow_service)

    try:
        with pytest.raises(FailedToCreateWorkflow, match="definition write failed"):
            await agent_protocol.create_workflow(
                data=WorkflowRequest.model_validate(WORKFLOW_CREATE_PAYLOAD),
                folder_id=None,
                current_org=organization,
                user_id="u_test",
                idempotency_key="write-rollback-key",
            )

        async with database.Session() as session:
            workflow_count = await session.scalar(select(func.count()).select_from(WorkflowModel))
            output_parameter_count = await session.scalar(select(func.count()).select_from(OutputParameterModel))
        assert workflow_count == 0
        assert output_parameter_count == 0
        assert hook_scheduled is False
    finally:
        await database.engine.dispose()
