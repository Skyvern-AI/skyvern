from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.exceptions import OTPRequestAlreadyBoundError
from skyvern.forge.sdk.db.models import TaskGenerationModel
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.task_generations import TaskGeneration
from skyvern.forge.sdk.schemas.tasks import TaskRequest, TaskStatus
from skyvern.forge.sdk.services import request_principal
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
from skyvern.schemas.runs import RunEngine
from skyvern.services import task_v1_service
from skyvern.services.task_v1_service import InvalidTaskV1ModelError, generate_task, run_task


@pytest.mark.asyncio
async def test_session_credential_denial_precedes_task_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    principal = RequestPrincipal("org_test", AuthKind.bearer, "member", "org:member")
    monkeypatch.setattr(request_principal, "get_request_principal", lambda: principal)
    enforce = AsyncMock(side_effect=PermissionError("credential not accessible"))
    monkeypatch.setattr(app.AGENT_FUNCTION, "enforce_browser_session_credential_use", enforce)
    create_task = AsyncMock(side_effect=AssertionError("task created before credential check"))
    monkeypatch.setattr(app.agent, "create_task", create_task)
    now = datetime.now(timezone.utc)
    organization = Organization(organization_id="org_test", organization_name="Test", created_at=now, modified_at=now)
    with pytest.raises(HTTPException) as exc:
        await run_task(
            TaskRequest(url="https://example.com", browser_session_id="pbs_test"),
            organization,
        )
    assert exc.value.status_code == 403
    enforce.assert_awaited_once_with(principal, "pbs_test")
    create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_submission_rejects_a_principal_from_another_organization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        request_principal,
        "get_request_principal",
        lambda: RequestPrincipal("other_org", AuthKind.api_key),
    )
    enforce = AsyncMock()
    create_task = AsyncMock()
    monkeypatch.setattr(app.AGENT_FUNCTION, "enforce_browser_session_credential_use", enforce)
    monkeypatch.setattr(app.agent, "create_task", create_task)
    now = datetime.now(timezone.utc)
    organization = Organization(organization_id="org_test", organization_name="Test", created_at=now, modified_at=now)
    with pytest.raises(HTTPException) as exc:
        await run_task(TaskRequest(url="https://example.com", browser_session_id="pbs_test"), organization)
    assert exc.value.status_code == 403
    enforce.assert_not_awaited()
    create_task.assert_not_awaited()


@pytest_asyncio.fixture
async def repository(sqlite_engine: AsyncEngine) -> WorkflowParametersRepository:
    db = BaseAlchemyDB(sqlite_engine)
    return WorkflowParametersRepository(db.Session, debug_enabled=False)


async def _insert_task_generation(
    repository: WorkflowParametersRepository,
    *,
    task_generation_id: str,
    organization_id: str,
    user_prompt_hash: str,
    created_at: datetime,
    llm: str | None = "test-llm",
) -> None:
    async with repository.Session() as session:
        session.add(
            TaskGenerationModel(
                task_generation_id=task_generation_id,
                organization_id=organization_id,
                user_prompt="test prompt",
                user_prompt_hash=user_prompt_hash,
                llm=llm,
                created_at=created_at,
                modified_at=created_at,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_is_scoped_to_organization(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prompt_hash = "shared-prompt-hash"
    await _insert_task_generation(
        repository,
        task_generation_id="tg_organization_a",
        organization_id="org_a",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=2),
    )
    await _insert_task_generation(
        repository,
        task_generation_id="tg_organization_b",
        organization_id="org_b",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=1),
    )

    organization_a_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_a", user_prompt_hash=prompt_hash
    )
    organization_b_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_b", user_prompt_hash=prompt_hash
    )
    organization_c_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_c", user_prompt_hash=prompt_hash
    )

    assert organization_a_result is not None
    assert organization_a_result.task_generation_id == "tg_organization_a"
    assert organization_a_result.organization_id == "org_a"
    assert organization_b_result is not None
    assert organization_b_result.task_generation_id == "tg_organization_b"
    assert organization_b_result.organization_id == "org_b"
    assert organization_c_result is None


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_does_not_return_cross_tenant_match(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await _insert_task_generation(
        repository,
        task_generation_id="tg_other_organization",
        organization_id="org_other",
        user_prompt_hash="cross-organization-hash",
        created_at=now,
    )

    result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_requesting", user_prompt_hash="cross-organization-hash"
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_returns_newest_matching_row(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prompt_hash = "repeated-prompt-hash"
    await _insert_task_generation(
        repository,
        task_generation_id="tg_older",
        organization_id="org_a",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=2),
    )
    await _insert_task_generation(
        repository,
        task_generation_id="tg_newer",
        organization_id="org_a",
        user_prompt_hash=prompt_hash,
        created_at=now - timedelta(minutes=1),
    )

    result = await repository.get_task_generation_by_prompt_hash(organization_id="org_a", user_prompt_hash=prompt_hash)

    assert result is not None
    assert result.task_generation_id == "tg_newer"
    assert result.organization_id == "org_a"


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_preserves_llm_and_window_filters(
    repository: WorkflowParametersRepository,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await _insert_task_generation(
        repository,
        task_generation_id="tg_without_llm",
        organization_id="org_a",
        user_prompt_hash="without-llm-hash",
        created_at=now,
        llm=None,
    )
    await _insert_task_generation(
        repository,
        task_generation_id="tg_outside_window",
        organization_id="org_a",
        user_prompt_hash="outside-window-hash",
        created_at=now - timedelta(hours=2),
    )

    without_llm_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_a", user_prompt_hash="without-llm-hash"
    )
    outside_window_result = await repository.get_task_generation_by_prompt_hash(
        organization_id="org_a", user_prompt_hash="outside-window-hash", query_window_hours=1
    )

    assert without_llm_result is None
    assert outside_window_result is None


@pytest.mark.asyncio
async def test_get_task_generation_by_prompt_hash_requires_organization_id(
    repository: WorkflowParametersRepository,
) -> None:
    with pytest.raises(TypeError):
        await repository.get_task_generation_by_prompt_hash(user_prompt_hash="prompt-hash")


@pytest.mark.asyncio
async def test_generate_task_forwards_organization_id_to_cache_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    organization = Organization(
        organization_id="org_requesting",
        organization_name="test",
        created_at=now,
        modified_at=now,
    )
    cached_task_generation = TaskGeneration(
        task_generation_id="tg_cached",
        organization_id=organization.organization_id,
        user_prompt="test prompt",
        user_prompt_hash="cached-prompt-hash",
        llm="test-llm",
        created_at=now,
        modified_at=now,
    )
    lookup_kwargs: dict[str, object] = {}

    async def get_task_generation_by_prompt_hash(**kwargs: object) -> TaskGeneration:
        lookup_kwargs.update(kwargs)
        return cached_task_generation

    async def create_task_generation(**kwargs: object) -> TaskGeneration:
        return cached_task_generation

    database = SimpleNamespace(
        workflow_params=SimpleNamespace(
            get_task_generation_by_prompt_hash=get_task_generation_by_prompt_hash,
            create_task_generation=create_task_generation,
        )
    )
    monkeypatch.setattr(app, "DATABASE", database)

    await generate_task("test prompt", organization)

    assert lookup_kwargs["organization_id"] == organization.organization_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("public_dns")
async def test_run_task_rejects_or_terminalizes_admission_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_VOLCENGINE", False)
    create_task_calls: list[object] = []

    async def fake_create_task(*args: object, **kwargs: object) -> None:
        create_task_calls.append(args)

    monkeypatch.setattr(app, "agent", SimpleNamespace(create_task=fake_create_task))
    now = datetime.now(timezone.utc)
    organization = Organization(organization_id="o_1", organization_name="org", created_at=now, modified_at=now)

    with pytest.raises(InvalidTaskV1ModelError):
        await run_task(
            TaskRequest(url="https://example.com", navigation_goal="go"), organization, engine=RunEngine.ui_tars
        )
    assert create_task_calls == []
    with pytest.raises(HTTPException) as masked:
        await run_task(TaskRequest(url="https://example.com", extra_http_headers={"X-Key": "***"}), organization)
    assert masked.value.status_code == 422
    assert masked.value.detail == "extra_http_headers value for X-Key is masked (***); send the real value"
    assert create_task_calls == []

    monkeypatch.setattr(
        app,
        "agent",
        SimpleNamespace(
            create_task=AsyncMock(side_effect=OTPRequestAlreadyBoundError(owner_type="task", owner_id="tsk_owner"))
        ),
    )
    with pytest.raises(HTTPException) as conflict:
        await run_task(TaskRequest(url="https://example.com", otp_request_id="otpr_retry"), organization)
    assert conflict.value.status_code == 409
    assert conflict.value.detail == "otp_request_id is already bound to another run"

    created_task = SimpleNamespace(
        task_id="tsk_strict_setup",
        organization_id=organization.organization_id,
        otp_request_id="otpr_strict_setup",
    )
    failed_task = SimpleNamespace(
        task_id=created_task.task_id,
        organization_id=organization.organization_id,
        otp_request_id=created_task.otp_request_id,
        status=TaskStatus.failed,
        started_at=None,
        finished_at=now,
    )
    finish_calls: list[dict[str, object]] = []
    task_run_syncs: list[dict[str, object]] = []
    task_webhooks: list[dict[str, object]] = []

    async def create_strict_task(*_args: object, **_kwargs: object) -> object:
        return created_task

    async def fail_engine_resolution(**_kwargs: object) -> RunEngine:
        raise RuntimeError("engine setup failed")

    async def finish_task(*_args: object, **kwargs: object) -> tuple[object, bool]:
        finish_calls.append(kwargs)
        return failed_task, True

    async def sync_task_run(**kwargs: object) -> None:
        task_run_syncs.append(kwargs)

    async def send_task_webhook(**kwargs: object) -> None:
        task_webhooks.append(kwargs)

    monkeypatch.setattr(
        app,
        "agent",
        SimpleNamespace(create_task=create_strict_task, execute_task_webhook=send_task_webhook),
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "resolve_run_engine", fail_engine_resolution)
    monkeypatch.setattr(app.DATABASE.tasks, "update_task_and_claim_finish", finish_task)
    monkeypatch.setattr(app.DATABASE.tasks, "sync_task_run_status", sync_task_run)

    with pytest.raises(RuntimeError, match="engine setup failed"):
        await run_task(
            TaskRequest(
                url="https://example.com",
                navigation_goal="go",
                otp_request_id=created_task.otp_request_id,
            ),
            organization,
            x_api_key="api-key",
        )

    assert finish_calls == [
        {
            "organization_id": organization.organization_id,
            "status": TaskStatus.failed,
            "failure_reason": "Strict OTP task setup failed after admission.",
            "keep_final_status": True,
        }
    ]
    assert len(task_run_syncs) == 1
    assert task_webhooks == [{"task": failed_task, "api_key": "api-key"}]

    finish_calls.clear()
    task_run_syncs.clear()
    task_webhooks.clear()

    cancellation = asyncio.CancelledError()

    async def cancel_engine_resolution(**_kwargs: object) -> RunEngine:
        raise cancellation

    monkeypatch.setattr(app.AGENT_FUNCTION, "resolve_run_engine", cancel_engine_resolution)

    with pytest.raises(asyncio.CancelledError) as raised:
        await run_task(
            TaskRequest(
                url="https://example.com",
                navigation_goal="go",
                otp_request_id=created_task.otp_request_id,
            ),
            organization,
            x_api_key="api-key",
        )

    assert raised.value is cancellation
    assert len(finish_calls) == 1
    assert len(task_run_syncs) == 1
    assert task_webhooks == [{"task": failed_task, "api_key": "api-key"}]

    finish_calls.clear()

    async def resolve_engine(**_kwargs: object) -> RunEngine:
        return RunEngine.skyvern_v1

    async def noop(**_kwargs: object) -> None:
        return None

    async def fail_dispatch(**_kwargs: object) -> None:
        raise RuntimeError("ambiguous Temporal submission")

    monkeypatch.setattr(app.AGENT_FUNCTION, "resolve_run_engine", resolve_engine)
    monkeypatch.setattr(app.DATABASE.tasks, "create_task_run", noop)
    monkeypatch.setattr(task_v1_service.uploaded_file_service, "attach_files_to_run", noop)
    monkeypatch.setattr(
        task_v1_service.AsyncExecutorFactory,
        "get_executor",
        lambda: SimpleNamespace(execute_task=fail_dispatch),
    )

    with pytest.raises(RuntimeError, match="ambiguous Temporal submission"):
        await run_task(
            TaskRequest(
                url="https://example.com",
                navigation_goal="go",
                otp_request_id=created_task.otp_request_id,
            ),
            organization,
        )

    assert finish_calls == []
