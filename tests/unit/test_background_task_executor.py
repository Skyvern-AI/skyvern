import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy import insert, literal, select, update

from skyvern.config import settings
from skyvern.exceptions import (
    BackgroundSequentialCredentialUnsupported,
    SkyvernException,
    WorkflowRetryAttemptLookupError,
)
from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.models import (
    ArtifactModel,
    Base,
    OrganizationModel,
    OutputParameterModel,
    StepModel,
    TaskModel,
    WorkflowModel,
    WorkflowRunAttemptModel,
    WorkflowRunBlockModel,
    WorkflowRunModel,
    WorkflowRunOutputParameterModel,
)
from skyvern.forge.sdk.db.repositories import workflow_run_attempts as attempts_repository_module
from skyvern.forge.sdk.db.repositories import workflow_runs as workflow_runs_repository_module
from skyvern.forge.sdk.db.repositories.workflow_runs import PrepareNextAttemptResult
from skyvern.forge.sdk.executor import background_task_executor as background_task_executor_module
from skyvern.forge.sdk.executor.background_task_executor import BackgroundTaskExecutor
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.persistent_browser_sessions import FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
from skyvern.forge.sdk.workflow import retry_policy as retry_policy_module
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.constants import INTERIM_OUTPUT_SNAPSHOT_MAX_BYTES
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import RETRY_DECISION_GRACE_SECONDS, RetryDecision, mark_attempt_started
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.services import workflow_schedule_service as schedule_service_module
from tests.unit.scoped_asyncio import ScopedAsyncio


@pytest.fixture(autouse=True)
def workflow_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", WorkflowService())
    monkeypatch.setattr(
        app.DATABASE.workflow_run_attempts, "list_stale_dispatch_claims", AsyncMock(return_value=[]), raising=False
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", AsyncMock(return_value="absent"))


@pytest_asyncio.fixture
async def sqlite_db(tmp_path: Path) -> AsyncIterator[AgentDB]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}"
    engine = _build_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield AgentDB(url, db_engine=engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def interim_retry_db(sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> AgentDB:
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sqlite_db.Session() as session:
        session.add(OrganizationModel(organization_id="org_test", organization_name="Test Organization"))
        await session.commit()
        session.add_all(
            [
                WorkflowModel(
                    workflow_id="wf_retry",
                    workflow_permanent_id="wpid_retry",
                    organization_id="org_test",
                    title="Retry delivery",
                    version=1,
                    workflow_definition={
                        "blocks": [],
                        "parameters": [],
                        "retry_policy": {
                            "max_retries": 1,
                            "delay_seconds": 5,
                            "retry_on": [{"status": "failed"}],
                            "webhook_on_retry": "every_attempt",
                        },
                    },
                ),
                WorkflowRunModel(
                    workflow_run_id="wr_retry",
                    workflow_id="wf_retry",
                    workflow_permanent_id="wpid_retry",
                    organization_id="org_test",
                    status="failed",
                    finished_at=now,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_retry",
                    organization_id="org_test",
                    attempt_number=1,
                    status="failed",
                    retry_decision="retry",
                    finished_at=now,
                    next_attempt_at=now + timedelta(seconds=5),
                ),
            ]
        )
        await session.commit()
    svc = app.WORKFLOW_SERVICE
    monkeypatch.setattr(svc, "prepare_workflow_webhook", AsyncMock(return_value=object()))
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "remove_workflow_run_context", MagicMock())
    monkeypatch.setattr(sqlite_db.tasks, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(sqlite_db.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))
    return sqlite_db


@pytest_asyncio.fixture
async def prepared_dispatch_db(interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch) -> AgentDB:
    database = interim_retry_db
    monkeypatch.setattr(settings, "RETRY_DISPATCH_GRACE_SECONDS", 600)
    async with database.Session() as session:
        previous = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        assert previous is not None
        previous.interim_webhook_sent_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    preparation = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert preparation.status == "inserted"
    monkeypatch.setattr(
        database.organizations, "get_valid_org_auth_token", AsyncMock(return_value=SimpleNamespace(token="api-token"))
    )
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    return database


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_fresh", [True, False], ids=["startup", "periodic"])
@pytest.mark.parametrize("configured_grace", [0, 600, 2 * service_module.LEASE_TAKEOVER_SECONDS])
@pytest.mark.parametrize("scenario", ["stale", "young", "boundary", "running"])
async def test_claimed_undispatched_retry_recovers_after_process_loss(
    prepared_dispatch_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    resume_fresh: bool,
    configured_grace: int,
) -> None:
    database = prepared_dispatch_db
    assert await database.workflow_run_attempts.claim_prepared_attempt_execution("wr_retry", "org_test", 2)
    attempt = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    original_stamp = attempt.started_at
    assert original_stamp is not None
    monkeypatch.setattr(settings, "RETRY_DISPATCH_GRACE_SECONDS", configured_grace)
    grace = max(600, service_module.LEASE_TAKEOVER_SECONDS, configured_grace)
    age = grace + {"stale": 1, "young": -1, "boundary": 0, "running": 1}[scenario]
    recovery_time = attempt.modified_at + timedelta(seconds=age)
    for module in (service_module, background_task_executor_module, attempts_repository_module):
        monkeypatch.setattr(module, "naive_utc_now", lambda: recovery_time)
    if scenario == "running":
        async with database.Session() as session:
            run_model = await session.get(WorkflowRunModel, "wr_retry")
            assert run_model is not None
            run_model.status = "running"
            await session.commit()
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    run = await database.workflow_runs.get_workflow_run("wr_retry", "org_test")
    assert run is not None and run.started_at is not None
    assert attempt.status == "running" and attempt.retry_decision is None
    assert await AgentFunction.get_workflow_run_execution_status(app.AGENT_FUNCTION, run) == "absent"
    # Cloud/Temporal's existing recovery query must continue to exclude dispatch claims.
    assert (
        await database.workflow_run_attempts.list_attempts_needing_recovery(
            recovery_time - timedelta(seconds=service_module.LEASE_TAKEOVER_SECONDS)
        )
        == []
    )
    executor = BackgroundTaskExecutor()
    assert not executor._retry_dispatches_needing_recovery
    if resume_fresh:
        monkeypatch.setattr(executor, "_start_retry_recovery_sweep", MagicMock())
        await executor.recover_pending_retries()
    else:
        await executor._recover_pending_retries_once(resume_fresh=False)
    if executor._background_tasks:
        await asyncio.gather(*executor._background_tasks)
    current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    if scenario == "stale":
        execute.assert_awaited_once()
        assert execute.await_args is not None
        assert execute.await_args.kwargs["attempt_number"] == 2
        assert execute.await_args.kwargs["prepared_attempt_claimed"] is True
        assert current.started_at == recovery_time != original_stamp
        assert current.status == "running" and current.retry_decision is None
    else:
        execute.assert_not_awaited()
        assert current.started_at == original_stamp
        assert current.modified_at == attempt.modified_at


def _hold_after_dispatch_claim(
    database: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, asyncio.Event]:
    """Pause a prepared resume right after it claims the dispatch, before execution starts."""
    claimed = asyncio.Event()
    resume = asyncio.Event()
    claim = database.workflow_run_attempts.claim_prepared_attempt_execution

    async def claim_and_wait(*args: object, **kwargs: object) -> datetime | None:
        stamp = await claim(*args, **kwargs)
        claimed.set()
        await resume.wait()
        return stamp

    monkeypatch.setattr(database.workflow_run_attempts, "claim_prepared_attempt_execution", claim_and_wait)
    return claimed, resume


@pytest.mark.asyncio
async def test_retry_dispatch_fences_claim_replaced_before_dispatch(
    prepared_dispatch_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = prepared_dispatch_db
    entered, resume = _hold_after_dispatch_claim(database, monkeypatch)
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    pending = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    executor = BackgroundTaskExecutor()
    executor._schedule_retry_resume(pending)
    tasks = list(executor._background_tasks)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        claimed = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
        assert claimed.started_at is not None
        # Emulate the durable result of a competing recovery release/reclaim before this dispatch runs.
        replacement_stamp = claimed.started_at + timedelta(seconds=2 * service_module.LEASE_TAKEOVER_SECONDS)
        async with database.Session() as session:
            await session.execute(
                update(WorkflowRunAttemptModel)
                .where(
                    WorkflowRunAttemptModel.workflow_run_id == "wr_retry", WorkflowRunAttemptModel.attempt_number == 2
                )
                .values(started_at=replacement_stamp, modified_at=replacement_stamp)
            )
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.workflow_run_id == "wr_retry")
                .values(started_at=replacement_stamp, modified_at=replacement_stamp)
            )
            await session.commit()
    finally:
        resume.set()
        await asyncio.gather(*tasks)
    execute.assert_not_awaited()
    current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    assert current.started_at == replacement_stamp
    assert current.retry_decision is None
    assert not executor._retry_dispatches_needing_recovery
    assert not executor._scheduled_retry_resumes


@pytest.mark.asyncio
@pytest.mark.parametrize("initializer", ["state_file", "llm_runtime"])
@pytest.mark.parametrize("recovers", [True, False], ids=["transient", "persistent"])
async def test_prepared_retry_initializer_failure_keeps_the_attempt_recoverable(
    prepared_dispatch_db: AgentDB, monkeypatch: pytest.MonkeyPatch, initializer: str, recovers: bool
) -> None:
    database = prepared_dispatch_db
    error = RuntimeError("initializer unavailable")
    failing = AsyncMock(side_effect=[error, None, None] if recovers else error)
    monkeypatch.setattr(
        background_task_executor_module,
        "initialize_skyvern_state_file" if initializer == "state_file" else "prepare_org_llm_runtime",
        failing,
    )
    real_now = background_task_executor_module.naive_utc_now
    clock_offset = timedelta()

    async def sleep(seconds: float) -> None:
        nonlocal clock_offset
        assert 0 < seconds <= background_task_executor_module.LEASE_TAKEOVER_SECONDS
        if not recovers:
            clock_offset += timedelta(seconds=background_task_executor_module.LEASE_TAKEOVER_SECONDS + 1)

    monkeypatch.setattr(background_task_executor_module, "naive_utc_now", lambda: real_now() + clock_offset)
    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=sleep))
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    pending = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    executor = BackgroundTaskExecutor()
    executor._schedule_retry_resume(pending)
    await asyncio.gather(*executor._background_tasks)

    current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    workflow_run = await database.workflow_runs.get_workflow_run(workflow_run_id="wr_retry", organization_id="org_test")
    assert workflow_run is not None
    assert current.retry_decision is None
    assert not executor._scheduled_retry_resumes
    if recovers:
        execute.assert_awaited_once()
        assert execute.await_args is not None
        assert execute.await_args.kwargs["attempt_number"] == 2
        assert execute.await_args.kwargs["prepared_attempt_claimed"] is True
        assert current.started_at is not None
    else:
        execute.assert_not_awaited()
        assert current.status == "queued" and current.started_at is None
        assert workflow_run.status == WorkflowRunStatus.queued
        assert ("wr_retry", 2) in executor._retry_resumes_needing_recovery


@pytest.mark.asyncio
async def test_periodic_recovery_skips_its_own_stale_dispatch(
    prepared_dispatch_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = prepared_dispatch_db
    entered, resume = _hold_after_dispatch_claim(database, monkeypatch)
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    pending = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    executor = BackgroundTaskExecutor()
    executor._schedule_retry_resume(pending)
    tasks = list(executor._background_tasks)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        claimed = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
        assert claimed.started_at is not None
        recovery_time = claimed.modified_at + timedelta(seconds=2 * service_module.LEASE_TAKEOVER_SECONDS)
        for module in (service_module, background_task_executor_module, attempts_repository_module):
            monkeypatch.setattr(module, "naive_utc_now", lambda: recovery_time)
        await executor._recover_pending_retries_once(resume_fresh=False)
        current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
        assert current.started_at == claimed.started_at
        assert current.modified_at == claimed.modified_at
        execute.assert_not_awaited()
    finally:
        resume.set()
        await asyncio.gather(*tasks)
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_skips_dispatch_if_release_cas_loses(
    prepared_dispatch_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = prepared_dispatch_db
    assert await database.workflow_run_attempts.claim_prepared_attempt_execution("wr_retry", "org_test", 2)
    claimed = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    recovery_time = claimed.modified_at + timedelta(seconds=2 * service_module.LEASE_TAKEOVER_SECONDS)
    for module in (service_module, background_task_executor_module, attempts_repository_module):
        monkeypatch.setattr(module, "naive_utc_now", lambda: recovery_time)
    release = database.workflow_run_attempts.release_stale_dispatch_claim

    async def lose_release(*args: object, **kwargs: object) -> bool:
        async with database.Session() as session:
            await session.execute(
                update(WorkflowRunModel).where(WorkflowRunModel.workflow_run_id == "wr_retry").values(status="running")
            )
            await session.commit()
        return await release(*args, **kwargs)

    monkeypatch.setattr(database.workflow_run_attempts, "release_stale_dispatch_claim", lose_release)
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=False)
    if executor._background_tasks:
        await asyncio.gather(*executor._background_tasks)
    execute.assert_not_awaited()
    current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    assert current.started_at == claimed.started_at
    assert not executor._scheduled_retry_resumes


@pytest_asyncio.fixture
async def undecided_terminal_db(interim_retry_db: AgentDB) -> AgentDB:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=service_module.LEASE_TAKEOVER_SECONDS + 1)
    async with interim_retry_db.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.started_at = old - timedelta(seconds=10)
        run.finished_at = old
        run.failure_reason = "persisted terminal failure"
        run.failure_category = [{"category": "navigation"}]
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.status = "running"
        attempt.retry_decision = None
        attempt.started_at = run.started_at
        attempt.finished_at = None
        attempt.next_attempt_at = None
        attempt.modified_at = old
        await session.commit()
    return interim_retry_db


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", ["startup", "periodic", "release_only"])
@pytest.mark.parametrize("status", ["failed", "terminated"])
async def test_recovery_reconstructs_undecided_terminal_attempt(
    undecided_terminal_db: AgentDB, monkeypatch: pytest.MonkeyPatch, sweep: str, status: str
) -> None:
    database = undecided_terminal_db
    svc = app.WORKFLOW_SERVICE
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.status = status
        await session.commit()

    deliver = AsyncMock(return_value=True)
    execute = AsyncMock()
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)
    monkeypatch.setattr(svc, "execute_workflow_with_retries", execute)
    monkeypatch.setattr(service_module, "_get_recovery_api_key", AsyncMock(return_value="test-key"))
    monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", AsyncMock(return_value="test-key"))
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    executor = BackgroundTaskExecutor()
    if sweep == "release_only":
        await svc.recover_pending_workflow_attempts(release_only=True)
    else:
        await executor._recover_pending_retries_once(resume_fresh=sweep == "startup")
        await asyncio.gather(*executor._background_tasks)

    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    first = attempts[0]
    assert first.status == status
    assert first.retry_decision == ("retry" if status == "failed" else "final")
    assert first.failure_reason == "persisted terminal failure"
    assert first.failure_category == [{"category": "navigation"}]
    assert first.finished_at is not None
    deliver.assert_awaited_once()
    if status == "failed":
        assert first.next_attempt_at is not None
        assert first.interim_webhook_sent_at is not None
        assert first.webhook_sent_at is None
        if sweep == "release_only":
            assert len(attempts) == 1
            execute.assert_not_awaited()
        else:
            assert first.next_attempt_prepared_at is not None
            assert len(attempts) == 2
            assert attempts[1].status == "queued"
            assert execute.await_args.kwargs["attempt_number"] == 2
    else:
        assert len(attempts) == 1
        assert first.webhook_sent_at is not None
        assert first.next_attempt_at is None
        execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", ["background", "release_only"])
@pytest.mark.parametrize("ownership", ["absent", "running", "unknown"])
async def test_recovery_leaves_concurrent_terminal_decision_and_live_owner_alone(
    undecided_terminal_db: AgentDB, monkeypatch: pytest.MonkeyPatch, sweep: str, ownership: str
) -> None:
    database = undecided_terminal_db
    svc = app.WORKFLOW_SERVICE

    async def execution_status(run: WorkflowRun) -> str:
        if ownership == "absent":
            await service_module.on_terminal_transition(
                run, run.status, run.failure_reason, run.failure_category, attempt_number=1
            )
        return ownership

    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", execution_status)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)
    executor = BackgroundTaskExecutor()
    if sweep == "release_only":
        await svc.recover_pending_workflow_attempts(release_only=True)
    else:
        await executor._recover_pending_retries_once(resume_fresh=False)
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert len(attempts) == 1
    assert attempts[0].retry_decision == ("retry" if ownership == "absent" else None)
    assert attempts[0].side_effects_released_at is None
    assert attempts[0].interim_webhook_sent_at is None
    assert attempts[0].webhook_sent_at is None
    assert not executor._scheduled_retry_resumes
    deliver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("race_stage", ["read", "write"])
async def test_undecided_recovery_is_noop_when_live_finalizer_wins(
    undecided_terminal_db: AgentDB, monkeypatch: pytest.MonkeyPatch, race_stage: str
) -> None:
    database = undecided_terminal_db
    svc = app.WORKFLOW_SERVICE
    read_attempts = database.workflow_run_attempts.get_attempts
    transition = service_module.on_terminal_transition
    winning_attempt: WorkflowRunAttemptModel | None = None
    finalize_ready = asyncio.Event()

    async def finalize_live() -> None:
        nonlocal winning_attempt
        await finalize_ready.wait()
        winning_attempt = await database.workflow_run_attempts.finalize_attempt(
            "wr_retry",
            1,
            status="failed",
            failure_reason="live finalizer failure",
            failure_category=None,
            error_codes=[],
            finished_at=datetime.now(UTC),
            retry_decision="final",
            decision_reason="live finalizer decision",
            next_attempt_at=None,
        )

    live_finalizer = asyncio.create_task(finalize_live())

    async def transition_after_live(*args: object, **kwargs: object) -> object:
        finalize_ready.set()
        await live_finalizer
        return await transition(*args, **kwargs)

    async def tasks_after_live(*args: object, **kwargs: object) -> list:
        finalize_ready.set()
        await live_finalizer
        return []

    if race_stage == "read":
        monkeypatch.setattr(service_module, "on_terminal_transition", transition_after_live)
    else:
        monkeypatch.setattr(database.tasks, "get_tasks_by_workflow_run_id", tasks_after_live)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)
    executor = BackgroundTaskExecutor()
    try:
        await executor._recover_pending_retries_once(resume_fresh=False)
    finally:
        live_finalizer.cancel()
        await asyncio.gather(live_finalizer, return_exceptions=True)
    assert winning_attempt is not None
    attempts = await read_attempts("wr_retry")
    assert len(attempts) == 1
    current = attempts[0]
    assert current.retry_decision == "final"
    assert current.failure_reason == "live finalizer failure"
    assert current.decision_reason == "live finalizer decision"
    assert current.modified_at == winning_attempt.modified_at
    assert current.side_effects_released_at is None
    assert current.webhook_sent_at is None
    assert not executor._scheduled_retry_resumes
    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_sweep_paginates_undecided_attempts_after_finalizing_each_page(
    undecided_terminal_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = undecided_terminal_db
    svc = app.WORKFLOW_SERVICE
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=service_module.LEASE_TAKEOVER_SECONDS + 1)
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.status = "terminated"
        for index in range(4):
            run_id = f"wr_page_{index}"
            session.add(
                WorkflowRunModel(
                    workflow_run_id=run_id,
                    organization_id="org_test",
                    workflow_id="wf_retry",
                    workflow_permanent_id="wpid_retry",
                    status="terminated",
                    started_at=old,
                    finished_at=old,
                )
            )
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id=run_id,
                    organization_id="org_test",
                    attempt_number=1,
                    status="running",
                    started_at=old,
                    modified_at=old,
                )
            )
        await session.commit()

    read_page = database.workflow_run_attempts.list_attempts_needing_recovery
    cursors: list[tuple[datetime, str, int] | None] = []

    async def small_page(
        cutoff: datetime, *, cursor: tuple[datetime, str, int] | None = None
    ) -> list[WorkflowRunAttemptModel]:
        cursors.append(cursor)
        return await read_page(cutoff, cursor=cursor, limit=2)

    monkeypatch.setattr(database.workflow_run_attempts, "list_attempts_needing_recovery", small_page)
    monkeypatch.setattr(service_module, "ATTEMPT_RECOVERY_BATCH_SIZE", 2)
    monkeypatch.setattr(service_module, "_get_recovery_api_key", AsyncMock(return_value="test-key"))
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)
    await svc.recover_pending_workflow_attempts(release_only=True)
    assert len(cursors) == 3
    for run_id in ["wr_retry", *(f"wr_page_{index}" for index in range(4))]:
        attempt = (await database.workflow_run_attempts.get_attempts(run_id))[0]
        assert attempt.retry_decision == "final"
        assert attempt.webhook_sent_at is not None
    assert await read_page(datetime.now(UTC)) == []
    assert deliver.await_count == 5


@pytest.mark.asyncio
async def test_policy_terminal_winner_survives_late_failed_writer(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.status = "running"
        run.finished_at = None
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.status = "running"
        attempt.retry_decision = None
        attempt.finished_at = None
        attempt.next_attempt_at = None
        await session.commit()

    finalize_attempt = AsyncMock(wraps=database.workflow_run_attempts.finalize_attempt)
    monkeypatch.setattr(database.workflow_run_attempts, "finalize_attempt", finalize_attempt)
    sync_status = AsyncMock()
    monkeypatch.setattr(svc, "_sync_task_run_from_workflow_run", sync_status)

    before = set(svc._background_tasks)
    completed = await svc.mark_workflow_run_as_completed("wr_retry")
    late_failure = await svc.mark_workflow_run_as_failed("wr_retry", failure_reason="late failure")
    stored = await svc.get_workflow_run("wr_retry", "org_test")
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    await asyncio.gather(*(svc._background_tasks - before))

    assert completed.status == late_failure.status == stored.status == WorkflowRunStatus.completed
    assert late_failure.failure_reason == stored.failure_reason is None
    assert late_failure.failure_category == stored.failure_category is None
    assert late_failure.finished_at == stored.finished_at == completed.finished_at
    assert len(attempts) == 1
    assert attempts[0].status == "completed"
    assert attempts[0].retry_decision == "final"
    assert attempts[0].finished_at is not None
    assert attempts[0].failure_reason is None
    assert attempts[0].next_attempt_at is None
    assert attempts[0].next_attempt_prepared_at is None
    finalize_attempt.assert_awaited_once()
    sync_status.assert_awaited_once()
    assert sync_status.await_args.args[2] == WorkflowRunStatus.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped_exception", [False, True])
@pytest.mark.parametrize("final_delivery_succeeds", [False, True])
async def test_in_process_retry_continues_after_interim_delivery_exhaustion(
    interim_retry_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    escaped_exception: bool,
    final_delivery_succeeds: bool,
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    events: list[str] = []
    deliveries: list[int] = []

    async def deliver(_webhook: object) -> bool:
        attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
        number = attempts[-1].attempt_number
        deliveries.append(number)
        return number == 2 and final_delivery_succeeds

    async def execute(**kwargs: object) -> WorkflowRun:
        number = kwargs["attempt_number"]
        events.append(f"execute:{number}")
        if number == 1:
            if escaped_exception:
                raise RuntimeError("attempt failed")
            return await svc.get_workflow_run("wr_retry", "org_test")
        assert number == 2
        attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
        assert attempts[0].interim_webhook_sent_at is not None
        assert attempts[0].next_attempt_prepared_at is not None
        completed = await database.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id="wr_retry", status=WorkflowRunStatus.completed
        )
        assert completed is not None
        await service_module.on_terminal_transition(completed, completed.status, None, None, attempt_number=2)
        return completed

    async def sleep(seconds: float) -> None:
        # The owner sleeps only what is left of the 5 s delay after the interim effects ran.
        assert 0 <= seconds <= 5
        assert deliveries == [1] * service_module.TERMINAL_RELEASE_RETRY_MAX_ATTEMPTS
        events.append("sleep")

    monkeypatch.setattr(svc, "execute_workflow", execute)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))
    result = await svc.execute_workflow_with_retries(
        "wr_retry", api_key="test-key", organization=SimpleNamespace(organization_id="org_test")
    )

    assert result.status == WorkflowRunStatus.completed
    assert events == ["execute:1", "sleep", "execute:2"]
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert len(attempts) == 2
    assert attempts[0].interim_webhook_sent_at is not None
    assert attempts[0].side_effects_released_at is not None
    assert attempts[0].interim_side_effects_progress["webhook_delivery_exhausted_at"]
    assert not attempts[0].interim_side_effects_progress.get("webhook_delivery_attempted")
    assert attempts[0].retry_decision == "retry"
    assert attempts[1].retry_decision == "final"
    assert attempts[1].webhook_sent_at is not None
    assert bool(attempts[1].final_side_effects_progress.get("webhook_delivery_attempted")) == final_delivery_succeeds
    assert bool(attempts[1].final_side_effects_progress.get("webhook_delivery_exhausted_at")) != final_delivery_succeeds
    assert deliveries.count(2) == (1 if final_delivery_succeeds else service_module.TERMINAL_RELEASE_RETRY_MAX_ATTEMPTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped_exception", [False, True])
async def test_in_process_retry_continues_after_interim_payload_failure(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, escaped_exception: bool
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    events: list[str] = []
    release_outcomes: list[str] = []
    release = svc._run_terminal_side_effects_with_retries

    async def record_release(*args: object, **kwargs: object) -> str:
        outcome = await release(*args, **kwargs)
        release_outcomes.append(outcome)
        return outcome

    async def prepare(run: WorkflowRun, *args: object, **kwargs: object) -> object:
        if run.status == WorkflowRunStatus.failed:
            raise RuntimeError("payload unavailable")
        return object()

    async def execute(**kwargs: object) -> WorkflowRun:
        number = kwargs["attempt_number"]
        events.append(f"execute:{number}")
        if number == 1:
            if escaped_exception:
                raise RuntimeError("attempt failed")
            return await svc.get_workflow_run("wr_retry", "org_test")
        assert number == 2
        attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
        assert attempts[0].next_attempt_prepared_at is not None
        completed = await database.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id="wr_retry", status=WorkflowRunStatus.completed
        )
        assert completed is not None
        await service_module.on_terminal_transition(completed, completed.status, None, None, attempt_number=2)
        return completed

    async def sleep(seconds: float) -> None:
        # The owner sleeps only what is left of the 5 s delay after the interim effects ran.
        assert 0 <= seconds <= 5
        assert release_outcomes == ["effect_failed"]
        attempt = (await database.workflow_run_attempts.get_attempts("wr_retry"))[0]
        assert attempt.interim_webhook_sent_at is None
        assert attempt.side_effects_released_at is None
        assert (attempt.interim_side_effects_progress or {}).get("webhook_delivery_attempts", 0) == 0
        events.append("sleep")

    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", record_release)
    monkeypatch.setattr(svc, "prepare_workflow_webhook", prepare)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "execute_workflow", execute)
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))
    result = await svc.execute_workflow_with_retries(
        "wr_retry", api_key="test-key", organization=SimpleNamespace(organization_id="org_test")
    )
    assert result.status == WorkflowRunStatus.completed
    assert events == ["execute:1", "sleep", "execute:2"]
    assert release_outcomes == ["effect_failed", "released"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", ["background", "cloud"])
@pytest.mark.parametrize("current_status", ["running", "completed"])
@pytest.mark.parametrize("artifact_link", ["task", "step", "block", "time"])
@pytest.mark.parametrize("latest_has_screenshot", [False, True])
@pytest.mark.parametrize("latest_has_recording", [False, True])
async def test_recovered_interim_payload_describes_target_attempt(
    interim_retry_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    sweep: str,
    current_status: str,
    artifact_link: str,
    latest_has_screenshot: bool,
    latest_has_recording: bool,
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    monkeypatch.setattr(svc, "prepare_workflow_webhook", WorkflowService.prepare_workflow_webhook.__get__(svc))
    monkeypatch.setattr(
        database.tasks,
        "get_tasks_by_workflow_run_id",
        type(database.tasks).get_tasks_by_workflow_run_id.__get__(database.tasks),
    )

    async def links(artifacts: list) -> list[str]:
        return [artifact.uri for artifact in artifacts]

    monkeypatch.setattr(app.ARTIFACT_MANAGER, "get_share_links_with_bundle_support", links)
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=False))

    def attribution(number: str) -> dict[str, str]:
        return {
            "task": {"task_id": f"task_{number}"},
            "step": {"step_id": f"step_{number}"},
            "block": {"workflow_run_block_id": f"block_{number}"},
            "time": {},
        }[artifact_link]

    downloads: list[FileInfo] = []
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(side_effect=lambda **kwargs: list(downloads)))
    monkeypatch.setattr(service_module, "_get_recovery_api_key", AsyncMock(return_value="test-key"))
    deliver = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(app.AGENT_FUNCTION, "deliver_webhook", deliver)
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.webhook_callback_url = "https://example.com/hook"
        run.failure_reason = "first attempt failed"
        run.started_at = run.finished_at - timedelta(seconds=10)
        run.queued_at = run.started_at - timedelta(seconds=1)
        run.failure_category = [{"category": "navigation"}]
        run.credits_used = 10
        run.cached_credits_used = 2
        first = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        first.failure_category = run.failure_category
        first.failure_reason = run.failure_reason
        first.error_codes = ["FIRST_ERROR"]
        first.started_at = run.started_at
        session.add(OutputParameterModel(output_parameter_id="op_retry", workflow_id="wf_retry", key="result"))
        session.add(
            WorkflowRunOutputParameterModel(
                workflow_run_id="wr_retry",
                output_parameter_id="op_retry",
                value={
                    "value": "first",
                    "legacy": {
                        "task_id": "task_first",
                        "workflow_screenshots": ["old-url"],
                    },
                },
            )
        )
        session.add(
            TaskModel(
                task_id="task_first",
                organization_id="org_test",
                workflow_run_id="wr_retry",
                status="failed",
                url="https://example.com",
                attempt_number=None,
            )
        )
        await session.flush()
        session.add(
            StepModel(
                step_id="step_first", task_id="task_first", organization_id="org_test", status="completed", order=0
            )
        )
        session.add(
            WorkflowRunBlockModel(
                workflow_run_block_id="block_first",
                workflow_run_id="wr_retry",
                organization_id="org_test",
                status="failed",
                block_type="task",
                attempt_number=None,
            )
        )
        for kind, suffix in [
            (ArtifactType.SCREENSHOT_FINAL, "png"),
            (ArtifactType.DOWNLOAD, "pdf"),
            (ArtifactType.RECORDING, "webm"),
        ]:
            session.add(
                ArtifactModel(
                    artifact_id=f"first_{suffix}",
                    organization_id="org_test",
                    run_id="wr_retry",
                    workflow_run_id="wr_retry",
                    **attribution("first"),
                    artifact_type=kind,
                    uri=(
                        "https://example.com/run_recordings/attempt1.webm"
                        if kind == ArtifactType.RECORDING
                        else f"https://example.com/attempt1.{suffix}"
                    ),
                    created_at=run.started_at,
                    modified_at=run.started_at,
                )
            )
        downloads.append(
            FileInfo(
                url="https://example.com/attempt1.pdf",
                filename="attempt1.pdf",
                artifact_id="first_pdf",
                modified_at=run.started_at,
            )
        )
        await session.commit()
    run = await svc.get_workflow_run("wr_retry", "org_test")
    original = await svc.prepare_workflow_webhook(run, "test-key")
    assert original is not None
    expected = json.loads(original.signed_payload)
    assert expected["retry_pending"] is True
    preparation = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert preparation.status == "inserted"
    assert await database.workflow_runs.get_workflow_run_output_parameters("wr_retry") == []
    await database.workflow_runs.create_or_update_workflow_run_output_parameter(
        "wr_retry", "op_retry", {"value": "second"}
    )
    async with database.Session() as session:
        run_row = await session.get(WorkflowRunModel, "wr_retry")
        run_row.status = current_status
        run_row.credits_used = 30
        run_row.cached_credits_used = 6
        run_row.started_at = datetime.now(UTC).replace(tzinfo=None)
        run_row.finished_at = run_row.started_at if current_status == "completed" else None
        second = await session.get(WorkflowRunAttemptModel, ("wr_retry", 2))
        second.status = current_status
        second.started_at = run_row.started_at
        second.finished_at = run_row.finished_at
        second.retry_decision = "final" if current_status == "completed" else None
        second.side_effects_released_at = run_row.started_at if current_status == "completed" else None
        session.add(
            TaskModel(
                task_id="task_second",
                organization_id="org_test",
                workflow_run_id="wr_retry",
                status=current_status,
                url="https://example.com",
                attempt_number=2,
            )
        )
        await session.flush()
        session.add(
            StepModel(
                step_id="step_second", task_id="task_second", organization_id="org_test", status="completed", order=0
            )
        )
        session.add(
            WorkflowRunBlockModel(
                workflow_run_block_id="block_second",
                workflow_run_id="wr_retry",
                organization_id="org_test",
                status="failed",
                block_type="task",
                attempt_number=2,
            )
        )
        for kind, suffix in [
            (ArtifactType.SCREENSHOT_FINAL, "png"),
            (ArtifactType.DOWNLOAD, "pdf"),
            (ArtifactType.RECORDING, "webm"),
        ]:
            session.add(
                ArtifactModel(
                    artifact_id=f"second_{suffix}",
                    organization_id="org_test",
                    run_id="wr_retry",
                    workflow_run_id="wr_retry",
                    **attribution("second"),
                    artifact_type=kind,
                    uri=(
                        "https://example.com/run_recordings/attempt2.webm"
                        if kind == ArtifactType.RECORDING
                        else f"https://example.com/attempt2.{suffix}"
                    ),
                    created_at=run_row.started_at,
                    modified_at=run_row.started_at,
                )
            )
        if not latest_has_recording:
            await session.flush()
            recording = await session.get(ArtifactModel, "second_webm")
            await session.delete(recording)
        if not latest_has_screenshot:
            await session.flush()
            screenshot = await session.get(ArtifactModel, "second_png")
            await session.delete(screenshot)
        downloads.extend(
            [
                FileInfo(
                    url="https://example.com/attempt2.pdf",
                    filename="attempt2.pdf",
                    artifact_id="second_pdf",
                    modified_at=downloads[0].modified_at,
                ),
                FileInfo(
                    url="https://example.com/unattributed2.pdf",
                    filename="unattributed2.pdf",
                    modified_at=run_row.started_at,
                ),
            ]
        )
        await session.commit()
    if sweep == "background":
        await BackgroundTaskExecutor()._recover_pending_retries_once(resume_fresh=False)
    else:
        await svc.recover_pending_workflow_attempts(release_only=True)
    assert deliver.await_count == 1
    payload = json.loads(deliver.call_args.kwargs["payload"])
    compared_fields = [
        "attempt",
        "status",
        "failure_reason",
        "failure_category",
        "queued_at",
        "credits_used",
        "cached_credits_used",
        "screenshot_urls",
        "recording_url",
        "recording_urls",
        "downloaded_files",
        "downloaded_file_urls",
        "total_steps",
        "step_count",
        "started_at",
        "finished_at",
        "attempts",
        "retry_pending",
        "next_attempt_at",
        "outputs",
        "output",
    ]
    mismatched_fields = [field for field in compared_fields if payload[field] != expected[field]]
    assert not mismatched_fields, mismatched_fields
    assert payload["attempts"][0]["error_codes"] == ["FIRST_ERROR"]
    assert payload["screenshot_urls"] == ["https://example.com/attempt1.png"]
    assert payload["recording_url"] == "https://example.com/run_recordings/attempt1.webm"
    assert payload["step_count"] == 1
    assert payload["credits_used"] == 10
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "calculate_workflow_run_total_cost",
        AsyncMock(side_effect=lambda **kwargs: kwargs["credits_used"] + kwargs["cached_credits_used"]),
    )
    response = await svc.build_workflow_run_status_response(
        "wpid_retry",
        "wr_retry",
        organization_id="org_test",
        target_attempt_number=1,
        include_cost=True,
    )
    assert response.total_cost == 12
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert attempts[0].interim_webhook_sent_at is not None
    assert attempts[0].webhook_sent_at is None
    assert attempts[1].interim_webhook_sent_at is None
    assert attempts[1].webhook_sent_at is None
    assert "output_parameters" not in attempts[0].interim_side_effects_progress
    historical = (await database.workflow_run_attempts.get_interim_payload_snapshot("wr_retry", 1))["output_parameters"]
    assert historical[0]["value"]["value"] == "first"
    assert payload["output"]["result"]["legacy"]["workflow_screenshots"] == ["https://example.com/attempt1.png"]
    current = await database.workflow_runs.get_workflow_run_output_parameters("wr_retry")
    assert [output.value for output in current] == [{"value": "second"}]
    async with database.Session() as session:
        run_row = await session.get(WorkflowRunModel, "wr_retry")
        run_row.status = "completed"
        run_row.finished_at = datetime.now(UTC).replace(tzinfo=None)
        second = await session.get(WorkflowRunAttemptModel, ("wr_retry", 2))
        second.status = "completed"
        second.finished_at = run_row.finished_at
        second.retry_decision = "final"
        second.side_effects_released_at = None
        await session.commit()
    run = await svc.get_workflow_run("wr_retry", "org_test")
    ordinary = await svc.prepare_workflow_webhook(run, "test-key")
    targeted = await svc.prepare_workflow_webhook(run, "test-key", target_attempt_number=2)
    assert json.loads(ordinary.signed_payload) == json.loads(targeted.signed_payload)
    latest = json.loads(ordinary.signed_payload)
    expected_screenshots = ["https://example.com/attempt2.png"] if latest_has_screenshot else None
    assert latest["screenshot_urls"] == expected_screenshots
    response = await svc.build_workflow_run_status_response("wpid_retry", "wr_retry", organization_id="org_test")
    assert response.screenshot_urls == expected_screenshots
    expected_recordings = ["https://example.com/run_recordings/attempt2.webm"] if latest_has_recording else None
    assert response.recording_urls == expected_recordings
    assert latest["recording_urls"] == expected_recordings
    assert response.recording_url == (expected_recordings[-1] if expected_recordings else None)
    assert latest["recording_url"] == response.recording_url
    # Steps are attempt-scoped like errors and screenshots; credits accumulate across attempts.
    assert latest["step_count"] == 1
    assert latest["credits_used"] == 30
    decision = RetryDecision(False, 2, 0, True, "no_match")
    for expected_outcome in ["released", "already_released"]:
        assert await svc._run_terminal_side_effects_with_retries(run, decision, api_key="test-key") == expected_outcome
    payloads = [json.loads(call.kwargs["payload"]) for call in deliver.call_args_list]
    assert [(item["attempt"], item["status"]) for item in payloads] == [(1, "failed"), (2, "completed")]
    assert payloads[1]["output"]["result"] == {"value": "second"}
    assert payloads[1]["screenshot_urls"] == expected_screenshots
    assert payloads[1]["recording_urls"] == expected_recordings
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert attempts[1].webhook_sent_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("has_attempt_row", [False, True])
async def test_single_attempt_preserves_screenshots_and_recordings(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, has_attempt_row: bool
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    start = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)
    async with database.Session() as session:
        workflow = await session.get(WorkflowModel, "wf_retry")
        workflow.workflow_definition = {"blocks": [], "parameters": []}
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.webhook_callback_url = "https://example.com/hook"
        if not has_attempt_row:
            attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
            await session.delete(attempt)
        for index in range(2):
            session.add(
                TaskModel(
                    task_id=f"legacy_task_{index}",
                    organization_id="org_test",
                    workflow_run_id="wr_retry",
                    status="failed",
                    url="https://example.com",
                    attempt_number=None,
                    created_at=start + timedelta(minutes=index),
                )
            )
        await session.flush()
        for index in range(3):
            session.add(
                ArtifactModel(
                    artifact_id=f"legacy_png_{index}",
                    organization_id="org_test",
                    run_id="wr_retry",
                    workflow_run_id="wr_retry",
                    task_id=f"legacy_task_{index}" if index < 2 else None,
                    artifact_type=ArtifactType.SCREENSHOT_FINAL,
                    uri=f"https://example.com/legacy{index}.png",
                    created_at=start + timedelta(minutes=index),
                )
            )
        session.add(
            ArtifactModel(
                artifact_id="legacy_webm",
                organization_id="org_test",
                run_id="wr_retry",
                workflow_run_id="wr_retry",
                artifact_type=ArtifactType.RECORDING,
                uri="https://example.com/run_recordings/legacy.webm",
                created_at=start,
            )
        )
        await session.commit()
    monkeypatch.setattr(
        database.tasks,
        "get_tasks_by_workflow_run_id",
        type(database.tasks).get_tasks_by_workflow_run_id.__get__(database.tasks),
    )
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER,
        "get_share_links_with_bundle_support",
        AsyncMock(side_effect=lambda artifacts: [artifact.uri for artifact in artifacts]),
    )
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=False))
    expected = [f"https://example.com/legacy{index}.png" for index in (1, 0, 2)]
    response = await svc.build_workflow_run_status_response("wpid_retry", "wr_retry", organization_id="org_test")
    assert response.screenshot_urls == expected
    assert response.recording_urls == ["https://example.com/run_recordings/legacy.webm"]
    payload = await WorkflowService.prepare_workflow_webhook(
        svc, await svc.get_workflow_run("wr_retry", "org_test"), "test-key"
    )
    assert json.loads(payload.signed_payload)["screenshot_urls"] == expected
    assert json.loads(payload.signed_payload)["recording_urls"] == response.recording_urls


@pytest.mark.asyncio
@pytest.mark.parametrize("interim_complete", [False, True])
@pytest.mark.parametrize("null_progress", [False, True])
async def test_interim_snapshot_is_bounded_and_survives_completed_delivery(
    interim_retry_db: AgentDB,
    interim_complete: bool,
    null_progress: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = interim_retry_db
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.interim_webhook_sent_at = datetime.now(UTC).replace(tzinfo=None) if interim_complete else None
        attempt.interim_side_effects_progress = (
            None
            if null_progress
            else {
                "completed_effects": ["cleanup"],
                "webhook_delivery_attempts": 1,
            }
        )
        for index in range(3):
            session.add(
                OutputParameterModel(output_parameter_id=f"large_{index}", workflow_id="wf_retry", key=f"large_{index}")
            )
            session.add(
                WorkflowRunOutputParameterModel(
                    workflow_run_id="wr_retry",
                    output_parameter_id=f"large_{index}",
                    value="x" * (INTERIM_OUTPUT_SNAPSHOT_MAX_BYTES // 2 + index),
                )
            )
        await session.flush()
        if null_progress:
            await session.execute(
                update(WorkflowRunAttemptModel)
                .where(
                    WorkflowRunAttemptModel.workflow_run_id == "wr_retry",
                )
                .values(interim_side_effects_progress=literal("null"))
            )
        await session.commit()
    result = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert result.status == "inserted"
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        progress = attempt.interim_side_effects_progress or {}
    if not null_progress:
        assert progress["completed_effects"] == ["cleanup"]
        assert progress["webhook_delivery_attempts"] == 1
    # A completed interim delivery no longer skips the snapshot: the historical response needs it.
    entries = progress["output_parameters"]
    assert len(json.dumps(entries).encode("utf-8")) <= INTERIM_OUTPUT_SNAPSHOT_MAX_BYTES
    assert [entry["output_parameter_id"] for entry in entries] == [f"large_{index}" for index in range(3)]
    assert all(entry["created_at"] for entry in entries)
    assert entries[1]["value"]["truncated"] is True
    assert entries[2]["value"]["truncated"] is True
    metadata = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert "output_parameters" not in metadata[0].interim_side_effects_progress
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))
    response = await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        "wpid_retry",
        "wr_retry",
        organization_id="org_test",
        target_attempt_number=1,
    )
    assert response.outputs["large_2"]["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_link", ["task", "step", "block"])
@pytest.mark.parametrize("ancestry", ["block", "chain", "fallback"])
@pytest.mark.parametrize("child_attempt", [None, 1])
async def test_historical_child_artifacts_use_parent_attempt(
    interim_retry_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    artifact_link: str,
    ancestry: str,
    child_attempt: int | None,
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    start = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    downloads = []
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.webhook_callback_url = "https://example.com/hook"
        for number in (1, 2):
            when = start + timedelta(minutes=10 * (number - 1))
            attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", number))
            if attempt is None:
                attempt = WorkflowRunAttemptModel(
                    workflow_run_id="wr_retry", organization_id="org_test", attempt_number=number
                )
                session.add(attempt)
            attempt.status = "failed"
            attempt.started_at = when
            attempt.finished_at = when + timedelta(minutes=1)
            attempt.retry_decision = "retry"
            attempt.interim_side_effects_progress = {
                "credits_used": 0,
                "cached_credits_used": 0,
                "output_parameters": [
                    {
                        "output_parameter_id": "op_children",
                        "created_at": when.isoformat(),
                        "value": {
                            "workflow_screenshot_artifact_ids": ["child_1_png", "child_2_png"],
                            "downloaded_file_artifact_ids": ["child_1_pdf", "child_2_pdf"],
                        },
                    }
                ],
            }
            enclosing_id = f"wr_enclosing_{number}"
            child_id = f"wr_child_{number}"
            if ancestry == "chain":
                session.add(
                    WorkflowRunModel(
                        workflow_run_id=enclosing_id,
                        workflow_permanent_id="wpid_retry",
                        workflow_id="wf_retry",
                        organization_id="org_test",
                        parent_workflow_run_id="wr_retry",
                        status="completed",
                    )
                )
            session.add(
                WorkflowRunModel(
                    workflow_run_id=child_id,
                    workflow_permanent_id="wpid_retry",
                    workflow_id="wf_retry",
                    organization_id="org_test",
                    parent_workflow_run_id=enclosing_id if ancestry == "chain" else "wr_retry",
                    status="completed",
                )
            )
            if ancestry != "fallback":
                session.add(
                    WorkflowRunBlockModel(
                        workflow_run_block_id=f"spawn_{number}",
                        workflow_run_id="wr_retry",
                        block_workflow_run_id=enclosing_id if ancestry == "chain" else child_id,
                        organization_id="org_test",
                        block_type="task_v2",
                        status="completed",
                        attempt_number=number,
                    )
                )
            await session.flush()
            session.add(
                TaskModel(
                    task_id=f"child_task_{number}",
                    workflow_run_id=child_id,
                    organization_id="org_test",
                    status="completed",
                    url="https://example.com",
                    attempt_number=child_attempt,
                )
            )
            await session.flush()
            session.add(
                StepModel(
                    step_id=f"child_step_{number}",
                    task_id=f"child_task_{number}",
                    organization_id="org_test",
                    status="completed",
                    order=0,
                )
            )
            session.add(
                WorkflowRunBlockModel(
                    workflow_run_block_id=f"child_block_{number}",
                    workflow_run_id=child_id,
                    organization_id="org_test",
                    block_type="task",
                    status="completed",
                    attempt_number=child_attempt,
                )
            )
            link = {
                "task": {"task_id": f"child_task_{number}"},
                "step": {"step_id": f"child_step_{number}"},
                "block": {"workflow_run_block_id": f"child_block_{number}"},
            }[artifact_link]
            # Late uploads retain the spawning attempt when an ancestor block is available.
            uploaded_at = when if ancestry == "fallback" else start + timedelta(minutes=25)
            for kind, suffix in [
                (ArtifactType.DOWNLOAD, "pdf"),
                (ArtifactType.SCREENSHOT_FINAL, "png"),
                (ArtifactType.RECORDING, "webm"),
            ]:
                artifact_id = f"child_{number}_{suffix}"
                url = f"https://example.com/{artifact_id}"
                session.add(
                    ArtifactModel(
                        artifact_id=artifact_id,
                        organization_id="org_test",
                        run_id="wr_retry",
                        workflow_run_id=child_id,
                        artifact_type=kind,
                        uri=url,
                        created_at=uploaded_at,
                        modified_at=uploaded_at,
                        **link,
                    )
                )
                if suffix == "pdf":
                    downloads.append(
                        FileInfo(url=url, filename=artifact_id, artifact_id=artifact_id, modified_at=uploaded_at)
                    )
        session.add(OutputParameterModel(output_parameter_id="op_children", workflow_id="wf_retry", key="children"))
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id="wr_retry",
                organization_id="org_test",
                attempt_number=3,
                status="running",
                started_at=start + timedelta(minutes=20),
            )
        )
        await session.commit()
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=downloads))
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER,
        "get_share_links_with_bundle_support",
        AsyncMock(side_effect=lambda artifacts: [a.uri for a in artifacts]),
    )
    monkeypatch.setattr(
        app.ARTIFACT_MANAGER, "resolve_share_url", AsyncMock(side_effect=lambda artifact, **kwargs: artifact.uri)
    )
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "resolve_artifact_url_expiry_seconds", AsyncMock(return_value=3600))
    monkeypatch.setattr(app.ARTIFACT_MANAGER, "is_recording_archived", AsyncMock(return_value=False))
    lookup = AsyncMock(wraps=database.workflow_runs.get_artifacts_for_attempt)
    monkeypatch.setattr(database.workflow_runs, "get_artifacts_for_attempt", lookup)
    for number in (1, 2):
        payload = await WorkflowService.prepare_workflow_webhook(
            svc, await svc.get_workflow_run("wr_retry", "org_test"), "test-key", target_attempt_number=number
        )
        body = json.loads(payload.signed_payload)
        assert body["screenshot_urls"] == [f"https://example.com/child_{number}_png"]
        assert body["recording_url"] == f"https://example.com/child_{number}_webm"
        assert body["downloaded_file_urls"] == [f"https://example.com/child_{number}_pdf"]
        assert body["outputs"]["children"]["downloaded_file_urls"] == [f"https://example.com/child_{number}_pdf"]
        assert lookup.await_count == number
    artifacts = await database.workflow_runs.get_artifacts_for_attempt(
        "wr_retry", "org_test", 1, start, start + timedelta(minutes=10)
    )
    legacy = await svc._refresh_output_urls(
        {
            "workflow_screenshot_artifact_ids": [],
            "downloaded_files": [
                {"filename": "child_1_pdf"},
                {"filename": "child_2_pdf"},
            ],
        },
        "org_test",
        "wr_retry",
        attempt_rows=await database.workflow_run_attempts.get_attempts("wr_retry"),
        attempt_number=1,
        historical_artifacts=artifacts,
    )
    assert legacy["downloaded_file_urls"] == ["https://example.com/child_1_pdf"]
    assert lookup.await_count == 3
    async with database.Session() as session:
        await session.execute(
            update(ArtifactModel)
            .where(ArtifactModel.artifact_type == ArtifactType.DOWNLOAD)
            .values(run_id="observer_storage")
        )
        await session.commit()
    monkeypatch.setattr(
        app.STORAGE,
        "get_downloaded_files",
        AsyncMock(side_effect=lambda **kwargs: downloads if kwargs["run_id"] == "observer_storage" else []),
    )
    files, urls = await svc._fetch_downloaded_files(
        await svc.get_workflow_run("wr_retry", "org_test"),
        SimpleNamespace(observer_cruise_id="observer_storage"),
        attempt_rows=await database.workflow_run_attempts.get_attempts("wr_retry"),
        attempt_number=1,
        historical_artifacts=[],
    )
    assert [file.artifact_id for file in files] == ["child_1_pdf"]
    assert urls == ["https://example.com/child_1_pdf"]
    assert lookup.await_count == 4
    assert lookup.call_args.args[0] == "wr_retry"
    assert lookup.call_args.kwargs == {"artifact_run_id": "observer_storage"}


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", ["null_columns", "missing_keys", "null_keys"])
async def test_historical_usage_defaults_to_zero(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, usage: str
) -> None:
    database = interim_retry_db
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.credits_used = None
        run.cached_credits_used = None
        run.webhook_callback_url = "https://example.com/hook"
        await session.commit()
    result = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert result.status == "inserted"
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.credits_used = 91
        run.cached_credits_used = 17
        if usage != "null_columns":
            attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
            attempt.interim_side_effects_progress = (
                {"output_parameters": []}
                if usage == "missing_keys"
                else {"output_parameters": [], "credits_used": None, "cached_credits_used": None}
            )
        await session.commit()
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))
    payload = await WorkflowService.prepare_workflow_webhook(
        app.WORKFLOW_SERVICE,
        await app.WORKFLOW_SERVICE.get_workflow_run("wr_retry", "org_test"),
        "test-key",
        target_attempt_number=1,
    )
    body = json.loads(payload.signed_payload)
    assert (body["credits_used"], body["cached_credits_used"]) == (0, 0)
    if usage == "null_columns":
        snapshot = await database.workflow_run_attempts.get_interim_payload_snapshot("wr_retry", 1)
        assert (snapshot["credits_used"], snapshot["cached_credits_used"]) == (0, 0)


@pytest.mark.asyncio
async def test_interim_snapshot_metadata_overflow_keeps_retry_and_reports_omission(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = interim_retry_db
    count = 10000
    created_at = datetime.now(UTC).replace(tzinfo=None)
    ids = [f"op_metadata_overflow_{index:08d}" for index in range(count)]
    async with database.Session() as session:
        await session.execute(
            insert(OutputParameterModel),
            [{"output_parameter_id": output_id, "workflow_id": "wf_retry", "key": output_id} for output_id in ids],
        )
        await session.execute(
            insert(WorkflowRunOutputParameterModel),
            [
                {
                    "workflow_run_id": "wr_retry",
                    "output_parameter_id": output_id,
                    "value": None,
                    "created_at": created_at + timedelta(microseconds=index),
                }
                for index, output_id in enumerate(ids)
            ],
        )
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.webhook_callback_url = "https://example.com/hook"
        await session.commit()
    result = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert result.status == "inserted"
    assert len(await database.workflow_run_attempts.get_attempts("wr_retry")) == 2
    snapshot = await database.workflow_run_attempts.get_interim_payload_snapshot("wr_retry", 1)
    entries = snapshot["output_parameters"]
    assert 0 < len(entries) < count
    assert snapshot["omitted_output_count"] == count - len(entries)
    assert (
        len(json.dumps({key: snapshot[key] for key in ("output_parameters", "omitted_output_count")}).encode())
        <= INTERIM_OUTPUT_SNAPSHOT_MAX_BYTES
    )
    assert [entry["output_parameter_id"] for entry in entries] == ids[: len(entries)]
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))
    payload = await WorkflowService.prepare_workflow_webhook(
        app.WORKFLOW_SERVICE,
        await app.WORKFLOW_SERVICE.get_workflow_run("wr_retry", "org_test"),
        "test-key",
        target_attempt_number=1,
    )
    body = json.loads(payload.signed_payload)
    assert body["outputs"]["_interim_output_snapshot"] == {
        "truncated": True,
        "reason": "interim_output_snapshot_budget_exceeded",
        "omitted_output_count": count - len(entries),
    }
    assert body["output"] == body["outputs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_attempt_number", [1, None])
async def test_attempt_recording_uses_its_shared_session(
    interim_retry_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    target_attempt_number: int | None,
) -> None:
    database = interim_retry_db
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.browser_session_id = "session_first"
        run.started_at = now - timedelta(seconds=30)
        first = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        first.started_at = run.started_at
        await session.commit()
    result = await database.workflow_runs.prepare_next_attempt_atomic(
        "wr_retry",
        "org_test",
        1,
        "failed",
        "session_second",
    )
    assert result.status == "inserted"
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        run.started_at = now
        second = await session.get(WorkflowRunAttemptModel, ("wr_retry", 2))
        second.started_at = now
        await session.commit()
    expected_session = "session_first" if target_attempt_number == 1 else "session_second"

    async def shared_recordings(*, organization_id: str, browser_session_id: str) -> list[FileInfo]:
        assert browser_session_id == expected_session
        return [
            FileInfo(
                url="https://example.com/shared.webm", filename="shared.webm", modified_at=now + timedelta(hours=1)
            )
        ]

    monkeypatch.setattr(app.STORAGE, "get_shared_recordings_in_browser_session", shared_recordings)
    monkeypatch.setattr(app.STORAGE, "get_downloaded_files", AsyncMock(return_value=[]))
    response = await app.WORKFLOW_SERVICE.build_workflow_run_status_response(
        "wpid_retry",
        "wr_retry",
        organization_id="org_test",
        target_attempt_number=target_attempt_number,
    )
    assert response.recording_url == "https://example.com/shared.webm"
    assert response.browser_session_id == expected_session


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", ["background", "cloud"])
@pytest.mark.parametrize("lease", ["none", "stale", "young"])
async def test_recovery_releases_earlier_interim_without_abandoning_retry(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, sweep: str, lease: str
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(seconds=service_module.LEASE_TAKEOVER_SECONDS)
    old = cutoff - timedelta(seconds=1)
    preparation = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert preparation.status == "inserted"
    async with database.Session() as session:
        attempt = await session.scalar(
            select(WorkflowRunAttemptModel).where(WorkflowRunAttemptModel.attempt_number == 1)
        )
        attempt.next_attempt_at = old
        attempt.side_effects_released_at = {"none": None, "stale": old, "young": now}[lease]
        await session.commit()
    assert [a.attempt_number for a in await database.workflow_run_attempts.list_attempts_needing_recovery(cutoff)] == (
        [] if lease == "young" else [1]
    )
    delivery = AsyncMock(return_value=False)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", delivery)
    monkeypatch.setattr(service_module, "_get_recovery_api_key", AsyncMock(return_value="test-key"))
    for succeeds in [False, True]:
        delivery.return_value = succeeds
        if sweep == "background":
            await BackgroundTaskExecutor()._recover_pending_retries_once(resume_fresh=False)
        else:
            await svc.recover_pending_workflow_attempts(release_only=True)
        attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
        assert [a.retry_decision for a in attempts] == ["retry", None]
        assert (attempts[0].interim_webhook_sent_at is not None) == (lease != "young")
        if lease != "young":
            assert attempts[0].interim_side_effects_progress["webhook_delivery_exhausted_at"]
            assert not attempts[0].interim_side_effects_progress.get("webhook_delivery_attempted")
        assert attempts[0].webhook_sent_at is None
        assert attempts[1].started_at is None
    assert delivery.await_count == (0 if lease == "young" else service_module.WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS)
    assert await database.workflow_run_attempts.list_attempts_needing_recovery(cutoff) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", ["background", "cloud"])
@pytest.mark.parametrize("run_terminal", [True, False])
async def test_recovery_finalizes_never_started_attempts_of_terminal_runs(
    monkeypatch: pytest.MonkeyPatch, sweep: str, run_terminal: bool
) -> None:
    # A cancel before mark_attempt_started leaves a created row with no started_at; the terminal run
    # still owes its cleanup and webhook. A queued run with the same row is not terminal and stays put.
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=service_module.LEASE_TAKEOVER_SECONDS + 1)
    attempt = SimpleNamespace(
        workflow_run_id="wr_never_started",
        organization_id="org_test",
        attempt_number=1,
        status="created",
        retry_decision=None,
        started_at=None,
        next_attempt_at=None,
        next_attempt_prepared_at=None,
        modified_at=old,
    )
    run = SimpleNamespace(
        workflow_run_id="wr_never_started",
        organization_id="org_test",
        status=WorkflowRunStatus.canceled if run_terminal else WorkflowRunStatus.queued,
    )
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=AsyncMock(side_effect=[[attempt], []]),
                list_stale_dispatch_claims=AsyncMock(return_value=[]),
            ),
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=run)),
        ),
    )
    recover = AsyncMock(return_value=None)
    monkeypatch.setattr(service_module, "_recover_undecided_terminal_attempt", recover)
    schedule_resume = MagicMock()

    if sweep == "background":
        executor = BackgroundTaskExecutor()
        executor._schedule_retry_resume = schedule_resume  # type: ignore[method-assign]
        await executor._recover_pending_retries_once(resume_fresh=False)
    else:
        await app.WORKFLOW_SERVICE.recover_pending_workflow_attempts(release_only=True)

    if run_terminal:
        recover.assert_awaited_once_with(attempt, workflow_run=run)
    else:
        recover.assert_not_awaited()
    schedule_resume.assert_not_called()


@pytest.mark.asyncio
async def test_undecided_recovery_reuses_the_run_the_sweep_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    # workflow_runs is high-traffic: the sweep's finality read is the only read per candidate.
    attempt = SimpleNamespace(workflow_run_id="wr_loaded", organization_id="org_test", attempt_number=1)
    run = SimpleNamespace(
        workflow_run_id="wr_loaded",
        organization_id="org_test",
        status=WorkflowRunStatus.canceled,
        failure_reason=None,
        failure_category=None,
    )
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(side_effect=AssertionError("re-read"))),
        ),
    )
    monkeypatch.setattr(
        app, "AGENT_FUNCTION", SimpleNamespace(get_workflow_run_execution_status=AsyncMock(return_value="running"))
    )

    assert await service_module._recover_undecided_terminal_attempt(attempt, workflow_run=run) is None


@pytest.mark.asyncio
async def test_schedule_runs_work_without_background_tasks() -> None:
    """Without a FastAPI BackgroundTasks the work used to be dropped silently."""
    ran = asyncio.Event()

    async def work(value: str, *, keyword: str) -> None:
        assert value == "positional"
        assert keyword == "keyword"
        ran.set()

    BackgroundTaskExecutor()._schedule(None, work, "positional", keyword="keyword")

    await asyncio.wait_for(ran.wait(), timeout=1)


@pytest.mark.asyncio
async def test_schedule_defers_to_background_tasks_when_present() -> None:
    calls: list[tuple[str, str]] = []

    async def work(value: str, *, keyword: str) -> None:
        calls.append((value, keyword))

    background_tasks = BackgroundTasks()
    BackgroundTaskExecutor()._schedule(background_tasks, work, "positional", keyword="keyword")

    # Queued on the request's BackgroundTasks rather than started eagerly.
    assert calls == []
    await background_tasks()
    assert calls == [("positional", "keyword")]


@pytest.mark.asyncio
async def test_recover_pending_retries_abandons_only_stale_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    stale_attempt = SimpleNamespace(
        workflow_run_id="wr_stale",
        organization_id="org_test",
        attempt_number=1,
        retry_decision="retry",
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=RETRY_DECISION_GRACE_SECONDS + 1),
        next_attempt_prepared_at=None,
    )
    fresh_attempt = SimpleNamespace(workflow_run_id="wr_fresh", organization_id="org_test", attempt_number=1)
    stale_run = SimpleNamespace(
        workflow_run_id="wr_stale",
        organization_id="org_test",
        status=WorkflowRunStatus.failed,
        failure_reason="failed",
        finished_at=SimpleNamespace(),
    )
    list_recovery = AsyncMock(return_value=[stale_attempt])
    get_workflow_run = AsyncMock(return_value=stale_run)
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=list_recovery, list_stale_dispatch_claims=AsyncMock(return_value=[])
            ),
            workflow_runs=SimpleNamespace(get_workflow_run=get_workflow_run),
        ),
    )
    abandoned_decision = RetryDecision(False, 1, 0, True, "process_restart")
    finalize_abandoned = AsyncMock(return_value=abandoned_decision)
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.finalize_abandoned_attempt",
        finalize_abandoned,
    )
    side_effects = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", side_effects)
    monkeypatch.setattr(BackgroundTaskExecutor, "_start_retry_recovery_sweep", MagicMock())

    await BackgroundTaskExecutor().recover_pending_retries()

    list_recovery.assert_awaited_once()
    assert get_workflow_run.await_args.kwargs["workflow_run_id"] == stale_attempt.workflow_run_id
    assert fresh_attempt.workflow_run_id not in {
        call.kwargs["workflow_run_id"] for call in get_workflow_run.await_args_list
    }
    finalize_abandoned.assert_awaited_once_with(
        workflow_run_id="wr_stale",
        organization_id="org_test",
        reason="process_restart",
        status=WorkflowRunStatus.failed,
        failure_reason="failed",
        finished_at=stale_run.finished_at,
        attempt_number=1,
    )
    side_effects.assert_awaited_once_with(stale_run, abandoned_decision)


@pytest.mark.asyncio
async def test_recovery_sweep_does_not_start_final_effects_for_a_young_retry_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_attempt = SimpleNamespace(
        workflow_run_id="wr_young_lease",
        organization_id="org_test",
        attempt_number=1,
        retry_decision="retry",
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=RETRY_DECISION_GRACE_SECONDS + 1),
        next_attempt_prepared_at=None,
        side_effects_released_at=datetime.now(UTC),
    )
    stale_run = SimpleNamespace(
        workflow_run_id="wr_young_lease",
        organization_id="org_test",
        status=WorkflowRunStatus.failed,
        failure_reason="failed",
        finished_at=SimpleNamespace(),
    )
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=AsyncMock(return_value=[stale_attempt]),
                list_stale_dispatch_claims=AsyncMock(return_value=[]),
            ),
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=stale_run)),
        ),
    )
    skipped_decision = RetryDecision(True, 1, 0, True, "stale_retry_lease_active")
    finalize_abandoned = AsyncMock(return_value=skipped_decision)
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.finalize_abandoned_attempt",
        finalize_abandoned,
    )
    side_effects = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", side_effects)

    await BackgroundTaskExecutor()._recover_pending_retries_once(resume_fresh=False)

    finalize_abandoned.assert_awaited_once()
    side_effects.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_pending_retries_starts_the_sweep_when_the_initial_pass_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = BackgroundTaskExecutor()
    passes: list[bool] = []
    outcomes = iter([RuntimeError("database unavailable"), RuntimeError("database unavailable"), None, None])

    async def recover_once(*, resume_fresh: bool) -> None:
        passes.append(resume_fresh)
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    ticks = asyncio.Semaphore(0)

    async def controlled_sleep(_seconds: float) -> None:
        await ticks.acquire()

    async def wait_for_pass(count: int) -> None:
        while len(passes) < count:
            await asyncio.sleep(0)

    monkeypatch.setattr(executor, "_recover_pending_retries_once", recover_once)
    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=controlled_sleep))

    with pytest.raises(RuntimeError, match="database unavailable"):
        await executor.recover_pending_retries()

    sweep_task = executor._retry_recovery_sweep_task
    assert sweep_task is not None and not sweep_task.done()
    try:
        for count in range(2, 5):
            ticks.release()
            await asyncio.wait_for(wait_for_pass(count), timeout=1)
    finally:
        await executor.stop_retry_recovery()

    # The sweep keeps resuming fresh rows until one pass succeeds, then leaves them to in-process owners.
    assert passes == [True, True, True, False]


@pytest.mark.asyncio
async def test_stop_retry_recovery_cancels_the_sweep_and_scheduled_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = BackgroundTaskExecutor()
    monkeypatch.setattr(executor, "_recover_pending_retries_once", AsyncMock())

    async def wait_for_next_attempt(_attempt: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(executor, "_resume_pending_retry", wait_for_next_attempt)

    await executor.recover_pending_retries()
    executor._schedule_retry_resume(SimpleNamespace(workflow_run_id="wr_retry", attempt_number=1))
    sweep_task = executor._retry_recovery_sweep_task
    assert sweep_task is not None and not sweep_task.done()
    (resume_task,) = executor._retry_resume_tasks
    assert not resume_task.done()

    await executor.stop_retry_recovery()

    assert sweep_task.cancelled()
    assert resume_task.cancelled()
    assert executor._retry_recovery_sweep_task is None
    assert not executor._retry_resume_tasks
    await executor.stop_retry_recovery()


@pytest.mark.asyncio
async def test_recover_pending_retries_resumes_fresh_attempt_after_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    pending_attempt = SimpleNamespace(
        workflow_run_id="wr_fresh",
        organization_id="org_test",
        attempt_number=1,
        retry_decision="retry",
        next_attempt_at=now + timedelta(seconds=5),
        next_attempt_prepared_at=None,
    )
    organization = SimpleNamespace(organization_id="org_test")
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    execution_done = asyncio.Event()

    async def controlled_sleep(delay_seconds: float) -> None:
        assert delay_seconds > 0
        sleep_started.set()
        await release_sleep.wait()

    async def execute_workflow_with_retries(**kwargs: object) -> None:
        assert kwargs["attempt_number"] == 2
        assert kwargs["browser_session_id"] == "pinned-session"
        assert kwargs["api_key"] == "api-token"
        execution_done.set()

    prepare = AsyncMock(
        return_value=PrepareNextAttemptResult(
            status="inserted",
            pinned_browser_session_id="pinned-session",
        )
    )
    get_valid_org_auth_token = AsyncMock(return_value=SimpleNamespace(token="api-token"))
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=AsyncMock(return_value=[pending_attempt]),
                list_stale_dispatch_claims=AsyncMock(return_value=[]),
            ),
            organizations=SimpleNamespace(
                get_organization=AsyncMock(return_value=organization),
                get_valid_org_auth_token=get_valid_org_auth_token,
            ),
        ),
    )
    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=controlled_sleep))
    monkeypatch.setattr(background_task_executor_module, "prepare_next_attempt_result", prepare)
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    monkeypatch.setattr(
        app.WORKFLOW_SERVICE,
        "execute_workflow_with_retries",
        execute_workflow_with_retries,
    )
    monkeypatch.setattr(BackgroundTaskExecutor, "_start_retry_recovery_sweep", MagicMock())

    executor = BackgroundTaskExecutor()
    await executor.recover_pending_retries()

    await asyncio.wait_for(sleep_started.wait(), timeout=1)
    prepare.assert_not_awaited()
    release_sleep.set()
    await asyncio.wait_for(execution_done.wait(), timeout=1)
    get_valid_org_auth_token.assert_awaited_once_with(
        organization_id="org_test",
        token_type="api",
    )
    prepare.assert_awaited_once_with(
        workflow_run_id="wr_fresh",
        organization_id="org_test",
        from_attempt=1,
        clear_browser_address=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup, failure",
    [
        ("token", "exception"),
        ("token", "none"),
        ("organization", "exception"),
        ("organization", "none"),
        ("initialization", "exception"),
    ],
)
async def test_startup_retry_recovers_transient_initialization_failure_without_another_sweep(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, lookup: str, failure: str
) -> None:
    database = interim_retry_db
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.next_attempt_at = now
        attempt.interim_webhook_sent_at = now
        await session.commit()
    organization = await database.organizations.get_organization("org_test")
    token = SimpleNamespace(token="api-token")
    error = RuntimeError("temporary lookup failure") if failure == "exception" else None
    monkeypatch.setattr(
        database.organizations,
        "get_organization",
        AsyncMock(side_effect=[error, organization] if lookup == "organization" else None, return_value=organization),
    )
    monkeypatch.setattr(
        database.organizations,
        "get_valid_org_auth_token",
        AsyncMock(side_effect=[error, token] if lookup == "token" else None, return_value=token),
    )
    monkeypatch.setattr(
        background_task_executor_module,
        "initialize_skyvern_state_file",
        AsyncMock(side_effect=[error, None, None] if lookup == "initialization" else None),
    )
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert 0 < seconds <= background_task_executor_module.LEASE_TAKEOVER_SECONDS
        attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
        assert len(attempts) == 1
        assert attempts[0].next_attempt_prepared_at is None

    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=sleep))
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=True)
    await asyncio.gather(*executor._background_tasks)

    assert sleeps
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert [row.attempt_number for row in attempts] == [1, 2]
    assert attempts[0].next_attempt_prepared_at is not None
    execute.assert_awaited_once()
    assert execute.await_args.kwargs["attempt_number"] == 2
    assert execute.await_args.kwargs["api_key"] == "api-token"
    assert not executor._scheduled_retry_resumes


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_startup_retry_backoff_preserves_sweep_recovery_or_cancellation(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    database = interim_retry_db
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.next_attempt_at = now
        attempt.interim_webhook_sent_at = now
        await session.commit()
    monkeypatch.setattr(background_task_executor_module, "naive_utc_now", lambda: now)
    token = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(database.organizations, "get_valid_org_auth_token", token)
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        if cancelled:
            raise asyncio.CancelledError
        now += timedelta(seconds=seconds)
        await executor._recover_pending_retries_once(resume_fresh=False)
        attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
        assert len(attempts) == 1
        assert attempts[0].retry_decision == "retry"

    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=sleep))
    await executor._recover_pending_retries_once(resume_fresh=True)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.gather(*executor._background_tasks)
    else:
        await asyncio.gather(*executor._background_tasks)
    execute.assert_not_awaited()
    assert not executor._scheduled_retry_resumes
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert len(attempts) == 1
    assert attempts[0].retry_decision == "retry"
    if cancelled:
        assert not executor._retry_resumes_needing_recovery
        return

    assert sum(sleeps) == background_task_executor_module.LEASE_TAKEOVER_SECONDS
    assert max(sleeps) <= 30
    assert ("wr_retry", 1) in executor._retry_resumes_needing_recovery
    now += timedelta(seconds=1)
    token.side_effect = None
    token.return_value = SimpleNamespace(token="api-token")
    await executor._recover_pending_retries_once(resume_fresh=False)
    await asyncio.gather(*executor._background_tasks)
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert [row.attempt_number for row in attempts] == [1, 2]
    execute.assert_awaited_once()
    assert execute.await_args.kwargs["attempt_number"] == 2
    assert not executor._retry_resumes_needing_recovery


@pytest.mark.asyncio
async def test_startup_retry_with_definitively_missing_token_remains_abandonable(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = interim_retry_db
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.next_attempt_at = now
        await session.commit()
    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=True)
    await asyncio.gather(*executor._background_tasks)
    assert not executor._scheduled_retry_resumes
    execute.assert_not_awaited()

    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.next_attempt_at = now - timedelta(seconds=background_task_executor_module.LEASE_TAKEOVER_SECONDS + 1)
        await session.commit()
    await executor._recover_pending_retries_once(resume_fresh=False)
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    assert len(attempts) == 1
    assert attempts[0].retry_decision == "abandoned"
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["token", "organization"])
@pytest.mark.parametrize("transient", [False, True])
async def test_prepared_retry_missing_dependency_abandons_or_recovers_on_next_sweep(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, dependency: str, transient: bool
) -> None:
    database = interim_retry_db
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.interim_webhook_sent_at = now
        await session.commit()
    preparation = await database.workflow_runs.prepare_next_attempt_atomic("wr_retry", "org_test", 1, "failed", None)
    assert preparation.status == "inserted"
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 2))
        attempt.modified_at = now - timedelta(seconds=background_task_executor_module.LEASE_TAKEOVER_SECONDS + 1)
        await session.commit()
    monkeypatch.setattr(background_task_executor_module, "LEASE_TAKEOVER_SECONDS", 3)
    monkeypatch.setattr(background_task_executor_module, "naive_utc_now", lambda: now)
    organization = await database.organizations.get_organization("org_test")
    lookup = AsyncMock(side_effect=RuntimeError("database unavailable") if transient else None, return_value=None)
    monkeypatch.setattr(
        database.organizations,
        "get_valid_org_auth_token" if dependency == "token" else "get_organization",
        lookup,
    )
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "prepare_workflow_webhook", AsyncMock(return_value=None))
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()

    async def sleep(seconds: float) -> None:
        nonlocal now
        current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
        assert current.status == "queued"
        assert current.retry_decision is None
        now += timedelta(seconds=seconds)

    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=sleep))
    await executor._recover_pending_retries_once(resume_fresh=True)
    await asyncio.gather(*executor._background_tasks)
    execute.assert_not_awaited()
    assert not executor._scheduled_retry_resumes
    current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
    run = await database.workflow_runs.get_workflow_run("wr_retry", "org_test")
    assert run is not None
    if transient:
        assert current.status == "queued"
        assert current.retry_decision is None
        assert current.started_at is None
        assert run.status == WorkflowRunStatus.queued
        assert ("wr_retry", 2) in executor._retry_resumes_needing_recovery
        lookup.side_effect = None
        lookup.return_value = SimpleNamespace(token="api-token") if dependency == "token" else organization
        if dependency == "organization":
            monkeypatch.setattr(
                database.organizations,
                "get_valid_org_auth_token",
                AsyncMock(return_value=SimpleNamespace(token="api-token")),
            )
        await executor._recover_pending_retries_once(resume_fresh=False)
        await asyncio.gather(*executor._background_tasks)
        current = (await database.workflow_run_attempts.get_attempts("wr_retry"))[-1]
        assert current.started_at is not None
        execute.assert_awaited_once()
        assert execute.await_args.kwargs["attempt_number"] == 2
        assert not executor._retry_resumes_needing_recovery
    else:
        assert current.status == "failed"
        assert current.retry_decision == "abandoned"
        assert current.decision_reason == "prepared_attempt_never_started"
        assert run.status == WorkflowRunStatus.failed
        assert ("API token" if dependency == "token" else "organization") in run.failure_reason
        assert current.webhook_sent_at is not None
        assert current.final_side_effects_progress["completed_effects"]
        assert not executor._retry_resumes_needing_recovery
        schedule = MagicMock()
        monkeypatch.setattr(executor, "_schedule_retry_resume", schedule)
        await executor._recover_pending_retries_once(resume_fresh=False)
        schedule.assert_not_called()
        execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_recovery_sweep_never_resumes_a_retry_while_its_loop_is_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_attempt = SimpleNamespace(
        workflow_run_id="wr_sleeping",
        organization_id="org_test",
        attempt_number=1,
        retry_decision="retry",
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=30),
        next_attempt_prepared_at=None,
    )
    sweep_sleep_started = asyncio.Event()
    release_sweep = asyncio.Event()
    recovery_finished = asyncio.Event()

    async def controlled_sleep(_delay_seconds: float) -> None:
        sweep_sleep_started.set()
        await release_sweep.wait()
        await asyncio.sleep(0)

    async def list_recovery(_cutoff: datetime) -> list[SimpleNamespace]:
        recovery_finished.set()
        return [pending_attempt]

    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=list_recovery, list_stale_dispatch_claims=AsyncMock(return_value=[])
            ),
        ),
    )
    monkeypatch.setattr(background_task_executor_module, "asyncio", ScopedAsyncio(sleep=controlled_sleep))
    schedule_resume = MagicMock()
    executor = BackgroundTaskExecutor()
    executor._fresh_retries_recovered = True
    executor._schedule_retry_resume = schedule_resume  # type: ignore[method-assign]

    sweep_task = asyncio.create_task(executor._retry_recovery_sweep())
    await asyncio.wait_for(sweep_sleep_started.wait(), timeout=1)
    release_sweep.set()
    await asyncio.wait_for(recovery_finished.wait(), timeout=1)
    sweep_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep_task

    schedule_resume.assert_not_called()


@pytest.mark.asyncio
async def test_two_retry_resumers_only_the_inserting_preparer_executes_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_attempt = SimpleNamespace(
        workflow_run_id="wr_two_callers",
        organization_id="org_test",
        attempt_number=1,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    organization = SimpleNamespace(organization_id="org_test")
    prepare = AsyncMock(
        side_effect=[
            PrepareNextAttemptResult(status="inserted", pinned_browser_session_id="pinned-session"),
            PrepareNextAttemptResult(status="already_prepared", pinned_browser_session_id="pinned-session"),
        ]
    )
    execute = AsyncMock()
    monkeypatch.setattr(background_task_executor_module, "prepare_next_attempt_result", prepare)
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(
                get_organization=AsyncMock(return_value=organization),
                get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="api-token")),
            ),
        ),
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)

    executor = BackgroundTaskExecutor()
    await asyncio.gather(
        executor._resume_pending_retry(pending_attempt),
        executor._resume_pending_retry(pending_attempt),
    )

    assert prepare.await_count == 2
    execute.assert_awaited_once()
    assert execute.await_args.kwargs["attempt_number"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_retry_entry_preserves_policy_when_attempt_lookup_fails(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    organization = await database.organizations.get_organization("org_test")
    attempts = await database.workflow_run_attempts.get_attempts("wr_retry")
    lookup = AsyncMock(
        side_effect=RuntimeError("attempt lookup failed")
        if persistent
        else [RuntimeError("temporary failure"), attempts]
    )
    monkeypatch.setattr(database.workflow_run_attempts, "get_attempts", lookup)
    sleep = AsyncMock()
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))
    run = await svc.get_workflow_run("wr_retry", "org_test")
    execute = AsyncMock(return_value=run)
    monkeypatch.setattr(svc, "execute_workflow", execute)
    decision = RetryDecision(False, 1, 0, True, "budget_exhausted")
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(return_value=decision))
    release = AsyncMock(return_value="released")
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", release)

    if persistent:
        with pytest.raises(WorkflowRetryAttemptLookupError) as raised:
            await svc.execute_workflow_with_retries("wr_retry", api_key="api-token", organization=organization)
        assert raised.value.__cause__ is lookup.side_effect
        assert lookup.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [0.1, 0.2]
        execute.assert_not_awaited()
        release.assert_not_awaited()
    else:
        await svc.execute_workflow_with_retries("wr_retry", api_key="api-token", organization=organization)
        assert lookup.await_count == 2
        sleep.assert_awaited_once_with(0.1)
        execute.assert_awaited_once()
        assert execute.await_args.kwargs["need_call_webhook"] is False
        release.assert_awaited_once()
        assert release.await_args.args[1] == decision


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", ["transient", "persistent"])
async def test_retry_owner_retries_next_attempt_preparation_until_the_sweep_cutoff(
    monkeypatch: pytest.MonkeyPatch, outage: str
) -> None:
    svc = app.WORKFLOW_SERVICE
    decision = RetryDecision(True, 1, 5, False, "retry_on_failed")
    error = RuntimeError("database unavailable")
    read = AsyncMock(side_effect=error if outage == "persistent" else [error, decision])
    monkeypatch.setattr(service_module, "get_recorded_decision", read)
    prepared = SimpleNamespace(status="inserted", pinned_browser_session_id=None, serialized_identity=False)
    prepare = AsyncMock(return_value=prepared)
    monkeypatch.setattr(service_module, "prepare_next_attempt_result", prepare)
    sleep = AsyncMock()
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))
    # Each clock read advances two thirds of the takeover window, so the second failure is past the deadline.
    clock = datetime.now(UTC).replace(tzinfo=None)
    step = timedelta(seconds=service_module.LEASE_TAKEOVER_SECONDS * 2 / 3)

    def now() -> datetime:
        nonlocal clock
        clock += step
        return clock - step

    monkeypatch.setattr(service_module, "naive_utc_now", now)

    if outage == "persistent":
        with pytest.raises(RuntimeError, match="database unavailable"):
            await svc._prepare_recorded_retry("wr_retry", "org_test", decision)
        prepare.assert_not_awaited()
    else:
        assert await svc._prepare_recorded_retry("wr_retry", "org_test", decision) is prepared
        prepare.assert_awaited_once()
        assert prepare.await_args.kwargs["from_attempt"] == 1
        assert prepare.await_args.kwargs["clear_browser_address"] is False
    assert read.await_count == 2
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_phase",
    [
        "preparation",
        "entry",
        "prepared_entry",
        "prepared_run_lookup",
        "transient_entry",
        "execution",
        "prepared_execution",
    ],
)
async def test_retry_resumer_recovers_attempt_lookup_failure_on_next_sweep(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    database = interim_retry_db
    svc = app.WORKFLOW_SERVICE
    now = datetime.now(UTC).replace(tzinfo=None)
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        attempt.next_attempt_at = now - timedelta(seconds=1)
        attempt.interim_webhook_sent_at = now
        await session.commit()
    if failure_phase.startswith("prepared_"):
        preparation = await database.workflow_runs.prepare_next_attempt_atomic(
            "wr_retry", "org_test", 1, "failed", None
        )
        assert preparation.status == "inserted"
    read_attempts = database.workflow_run_attempts.get_attempts
    pending = (await read_attempts("wr_retry"))[-1]
    monkeypatch.setattr(
        database.organizations, "get_valid_org_auth_token", AsyncMock(return_value=SimpleNamespace(token="api-token"))
    )
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    lookup = AsyncMock(wraps=read_attempts)
    monkeypatch.setattr(database.workflow_run_attempts, "get_attempts", lookup)
    claim = AsyncMock(wraps=database.workflow_run_attempts.claim_prepared_attempt_execution)
    monkeypatch.setattr(database.workflow_run_attempts, "claim_prepared_attempt_execution", claim)
    abandon = AsyncMock()
    executor = BackgroundTaskExecutor()
    monkeypatch.setattr(executor, "_abandon_retry", abandon)
    release = AsyncMock(return_value="released")
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", release)
    decision = RetryDecision(False, 2, 0, True, "budget_exhausted")
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(return_value=decision))
    execute = AsyncMock(return_value=await svc.get_workflow_run("wr_retry", "org_test"))
    monkeypatch.setattr(svc, "execute_workflow", execute)
    read_run = AsyncMock(wraps=svc.get_workflow_run)
    monkeypatch.setattr(svc, "get_workflow_run", read_run)
    real_entry = svc.execute_workflow_with_retries
    before_failure: list[dict[str, object]] = []

    async def snapshot() -> list[dict[str, object]]:
        return [
            {column.name: getattr(row, column.name) for column in WorkflowRunAttemptModel.__table__.columns}
            for row in await read_attempts("wr_retry")
        ]

    async def entry(**kwargs: object) -> WorkflowRun:
        nonlocal before_failure
        before_failure = await snapshot()
        if failure_phase in {"entry", "prepared_entry"}:
            lookup.side_effect = RuntimeError("attempt lookup failed")
        elif failure_phase == "prepared_run_lookup":
            read_run.side_effect = RuntimeError("workflow run lookup failed before execution")
        elif failure_phase == "transient_entry":
            lookup.side_effect = [RuntimeError("temporary failure"), await read_attempts("wr_retry")]
        return await real_entry(**kwargs)

    if failure_phase == "preparation":
        before_failure = await snapshot()
        lookup.side_effect = RuntimeError("attempt lookup failed")
    if failure_phase in {"execution", "prepared_execution"}:
        release.side_effect = RuntimeError("terminal effects failed after execution")
    if failure_phase == "prepared_execution":
        execute.side_effect = RuntimeError("workflow execution failed")
    dispatch = AsyncMock(side_effect=entry)
    monkeypatch.setattr(svc, "execute_workflow_with_retries", dispatch)
    executor._schedule_retry_resume(pending)
    await asyncio.gather(*executor._background_tasks)
    key = ("wr_retry", pending.attempt_number)
    assert not executor._scheduled_retry_resumes
    abandon.assert_not_awaited()

    if failure_phase.startswith("prepared_"):
        claimed_attempt = (await read_attempts("wr_retry"))[-1]
        assert claimed_attempt.started_at is not None
        assert claim.await_count == 1

    if failure_phase in {"transient_entry", "execution", "prepared_execution"}:
        execute.assert_awaited_once()
        assert execute.await_args.kwargs["need_call_webhook"] is False
        assert key not in executor._retry_resumes_needing_recovery
        assert key not in executor._retry_dispatches_needing_recovery
        lookup.side_effect = None
        await executor._recover_pending_retries_once(resume_fresh=False)
        assert not executor._scheduled_retry_resumes
        return

    execute.assert_not_awaited()
    assert await snapshot() == before_failure
    assert (await read_attempts("wr_retry"))[0].retry_decision == "retry"
    assert key in executor._retry_resumes_needing_recovery
    if failure_phase != "preparation":
        assert key in executor._retry_dispatches_needing_recovery
    if failure_phase in {"entry", "prepared_entry"}:
        assert lookup.await_count >= 3
    lookup.side_effect = None
    read_run.side_effect = None
    dispatch.side_effect = real_entry
    await executor._recover_pending_retries_once(resume_fresh=False)
    assert key in executor._scheduled_retry_resumes
    await asyncio.gather(*executor._background_tasks)
    execute.assert_awaited_once()
    assert execute.await_args.kwargs["attempt_number"] == 2
    assert execute.await_args.kwargs["need_call_webhook"] is False
    assert claim.await_count == 1
    assert key not in executor._retry_resumes_needing_recovery
    assert key not in executor._retry_dispatches_needing_recovery
    assert not executor._scheduled_retry_resumes
    assert (await read_attempts("wr_retry"))[0].retry_decision == "retry"
    abandon.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_recovery_leaves_a_fresh_prepared_queued_attempt_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_attempt = SimpleNamespace(
        workflow_run_id="wr_prepared_queued",
        organization_id="org_test",
        attempt_number=2,
        retry_decision=None,
        status=WorkflowRunStatus.queued.value,
        started_at=None,
        modified_at=datetime.now(UTC),
    )
    failed_run = SimpleNamespace(
        workflow_run_id="wr_prepared_queued",
        organization_id="org_test",
        status=WorkflowRunStatus.failed,
        failure_reason="Workflow retry attempt was prepared but never started before recovery.",
        finished_at=datetime.now(UTC),
    )
    mark_failed = AsyncMock()
    mark_failed.return_value = failed_run
    finalize_abandoned = AsyncMock(return_value=RetryDecision(False, 2, 0, True, "prepared_attempt_never_started"))
    side_effects = AsyncMock()
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=AsyncMock(side_effect=[[prepared_attempt], []]),
                list_stale_dispatch_claims=AsyncMock(return_value=[]),
            ),
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=failed_run)),
        ),
    )
    monkeypatch.setattr(background_task_executor_module, "finalize_abandoned_attempt", finalize_abandoned)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", mark_failed)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", side_effects)

    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=False)
    await executor._recover_pending_retries_once(resume_fresh=False)

    mark_failed.assert_not_awaited()
    finalize_abandoned.assert_not_awaited()
    side_effects.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_pending_retries_releases_unreleased_terminal_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_attempt = SimpleNamespace(
        workflow_run_id="wr_abandoned",
        organization_id="org_test",
        attempt_number=2,
        retry_decision="abandoned",
        decision_reason="process_restart",
        side_effects_released_at=None,
        webhook_sent_at=None,
        modified_at=datetime.now(UTC) - timedelta(seconds=RETRY_DECISION_GRACE_SECONDS + 1),
    )
    terminal_run = SimpleNamespace(
        workflow_run_id="wr_abandoned",
        organization_id="org_test",
        status=WorkflowRunStatus.failed,
    )
    list_recovery = AsyncMock(return_value=[terminal_attempt])
    get_workflow_run = AsyncMock(return_value=terminal_run)
    side_effects = AsyncMock()
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=list_recovery, list_stale_dispatch_claims=AsyncMock(return_value=[])
            ),
            workflow_runs=SimpleNamespace(get_workflow_run=get_workflow_run),
        ),
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", side_effects)
    monkeypatch.setattr(BackgroundTaskExecutor, "_start_retry_recovery_sweep", MagicMock())

    await BackgroundTaskExecutor().recover_pending_retries()

    side_effects.assert_awaited_once()
    decision = side_effects.await_args.args[1]
    assert decision == RetryDecision(False, 2, 0, True, "process_restart")


@pytest.mark.asyncio
async def test_recovery_reacquires_expired_side_effect_lease_after_effect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_claim = datetime.now(UTC) - timedelta(seconds=RETRY_DECISION_GRACE_SECONDS + 1)
    attempts = [
        SimpleNamespace(
            workflow_run_id="wr_lease",
            organization_id="org_test",
            attempt_number=1,
            retry_decision="final",
            decision_reason="budget_exhausted",
            side_effects_released_at=old_claim,
            webhook_sent_at=None,
            modified_at=old_claim,
        ),
        SimpleNamespace(
            workflow_run_id="wr_lease",
            organization_id="org_test",
            attempt_number=1,
            retry_decision="final",
            decision_reason="budget_exhausted",
            side_effects_released_at=old_claim,
            webhook_sent_at=None,
            modified_at=old_claim,
        ),
    ]
    terminal_run = SimpleNamespace(
        workflow_run_id="wr_lease",
        organization_id="org_test",
        status=WorkflowRunStatus.failed,
    )
    list_recovery = AsyncMock(side_effect=[[attempts[0]], [attempts[1]]])
    get_workflow_run = AsyncMock(return_value=terminal_run)
    release_calls: list[dict[str, object]] = []

    async def release_side_effects(*_args: object, **kwargs: object) -> None:
        release_calls.append(kwargs)
        if len(release_calls) == 1:
            raise RuntimeError("logical terminal effect failed")

    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                list_attempts_needing_recovery=list_recovery, list_stale_dispatch_claims=AsyncMock(return_value=[])
            ),
            workflow_runs=SimpleNamespace(get_workflow_run=get_workflow_run),
        ),
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", release_side_effects)

    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=False)
    await executor._recover_pending_retries_once(resume_fresh=False)

    assert [call["side_effects_claim_at"] for call in release_calls] == [old_claim, old_claim]


@pytest.mark.asyncio
async def test_scheduled_run_cannot_clobber_the_callers_context() -> None:
    """The caller keeps running after dispatching; both must not write one context object."""
    ran = asyncio.Event()
    child_context: list[SkyvernContext | None] = []

    async def work() -> None:
        context = skyvern_context.current()
        child_context.append(context)
        assert context is not None
        # execute_workflow assigns this on whatever context it finds.
        context.generate_script = False
        context.task_id = "tsk_child"
        ran.set()

    parent = SkyvernContext(organization_id="org_1", task_id="tsk_parent", generate_script=True)
    with skyvern_context.scoped(parent):
        BackgroundTaskExecutor()._schedule(None, work)
        await asyncio.wait_for(ran.wait(), timeout=1)

        assert child_context[0] is not parent
        # The caller's context survives the child's writes.
        assert parent.task_id == "tsk_parent"
        assert parent.generate_script is True
        # ...while inherited values still reach the child.
        assert child_context[0].organization_id == "org_1"


@pytest.mark.asyncio
async def test_execute_workflow_stamps_org_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        default_llm_key="CUSTOM_LLM_oat_smart",
        default_secondary_llm_key="CUSTOM_LLM_oat_fast",
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(sequential_credential_id=None, status=WorkflowRunStatus.created)),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.initialize_skyvern_state_file",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.custom_llm_registry.load_custom_llm_configs_for_organization",
        AsyncMock(),
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", AsyncMock())
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    await executor.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )
    _background_tasks, dispatch, attempt, execution_kwargs = executor._schedule.call_args.args

    # The dispatch task stamps the defaults on the context the run executes in.
    with skyvern_context.scoped(SkyvernContext(organization_id="org_test")) as context:
        await dispatch(attempt, execution_kwargs)

        assert context.org_default_llm_key == "CUSTOM_LLM_oat_smart"
        assert context.org_default_secondary_llm_key == "CUSTOM_LLM_oat_fast"


@pytest.mark.asyncio
async def test_execute_workflow_dispatches_from_latest_retry_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        default_llm_key=None,
        default_secondary_llm_key=None,
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(sequential_credential_id=None, status=WorkflowRunStatus.created)),
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(
            return_value=[
                SimpleNamespace(attempt_number=1),
                SimpleNamespace(attempt_number=2),
            ]
        ),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.initialize_skyvern_state_file",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.custom_llm_registry.load_custom_llm_configs_for_organization",
        AsyncMock(),
    )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    await executor.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )

    _background_tasks, dispatch, attempt, execution_kwargs = executor._schedule.call_args.args
    assert dispatch == executor._dispatch_retry_resume
    assert attempt.attempt_number == 2
    assert execution_kwargs["attempt_number"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("need_call_webhook", [True, False])
@pytest.mark.parametrize(
    "scenario", ["state_file", "llm_runtime", "lookup_failure", "attempt_row", "webhook_preparation"]
)
async def test_no_policy_initializer_failure_fails_the_run_durably(
    interim_retry_db: AgentDB, monkeypatch: pytest.MonkeyPatch, scenario: str, need_call_webhook: bool
) -> None:
    database = interim_retry_db
    async with database.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_retry")
        assert run is not None
        run.status = "created"
        run.started_at = None
        run.finished_at = None
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_retry", 1))
        assert attempt is not None
        if scenario == "attempt_row":
            attempt.status = "created"
            attempt.retry_decision = None
            attempt.finished_at = None
            attempt.next_attempt_at = None
        else:
            workflow = await session.get(WorkflowModel, "wf_retry")
            assert workflow is not None
            workflow.workflow_definition = {"blocks": [], "parameters": []}
            await session.delete(attempt)
        await session.commit()

    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    initializer = "prepare_org_llm_runtime" if scenario == "llm_runtime" else "initialize_skyvern_state_file"
    error = RuntimeError(f"{initializer} unavailable")
    monkeypatch.setattr(background_task_executor_module, initializer, AsyncMock(side_effect=error))
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    fail_run = AsyncMock(wraps=app.WORKFLOW_SERVICE.mark_workflow_run_as_failed_if_not_final)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", fail_run)
    webhook = AsyncMock(wraps=app.WORKFLOW_SERVICE.execute_workflow_webhook)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_webhook", webhook)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "deliver_prepared_workflow_webhook", deliver)
    if scenario == "webhook_preparation":
        monkeypatch.setattr(
            app.WORKFLOW_SERVICE, "prepare_workflow_webhook", AsyncMock(side_effect=RuntimeError("preparation failed"))
        )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]
    organization = await database.organizations.get_organization("org_test")
    assert organization is not None
    await executor.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_retry",
        workflow_run_id="wr_retry",
        workflow_permanent_id="wpid_retry",
        max_steps_override=None,
        api_key="test-dispatch-key",
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )
    _background_tasks, dispatch, attempt, execution_kwargs = executor._schedule.call_args.args
    execution_kwargs["need_call_webhook"] = need_call_webhook
    get_attempts = database.workflow_run_attempts.get_attempts
    if scenario == "lookup_failure":
        monkeypatch.setattr(
            database.workflow_run_attempts, "get_attempts", AsyncMock(side_effect=RuntimeError("db down"))
        )
    await dispatch(attempt, execution_kwargs)
    execute.assert_not_awaited()
    run = await database.workflow_runs.get_workflow_run("wr_retry")
    assert run is not None
    assert run.started_at is None
    attempts = await get_attempts("wr_retry")
    key = ("wr_retry", 1)
    if scenario in {"lookup_failure", "attempt_row"}:
        assert run.status == WorkflowRunStatus.queued
        assert run.failure_reason is None
        fail_run.assert_not_awaited()
        webhook.assert_not_awaited()
        assert key in executor._retry_dispatches_needing_recovery
        assert key in executor._retry_resumes_needing_recovery
        if scenario == "attempt_row":
            assert len(attempts) == 1
            assert attempts[0].status == "queued"
            assert attempts[0].started_at is None
        else:
            assert not attempts
    else:
        assert run.status == WorkflowRunStatus.failed
        reason = f"Workflow run initialization failed before execution: RuntimeError: {error}"
        assert run.failure_reason == reason
        fail_run.assert_awaited_once_with(workflow_run_id="wr_retry", failure_reason=reason)
        if need_call_webhook:
            webhook.assert_awaited_once_with(run, api_key="test-dispatch-key", claim_kind=None)
        else:
            webhook.assert_not_awaited()
        assert not attempts
        assert not executor._retry_dispatches_needing_recovery
        assert not executor._retry_resumes_needing_recovery
        executor._schedule.reset_mock()
        await executor._recover_pending_retries_once(resume_fresh=True)
        executor._schedule.assert_not_called()
        execute.assert_not_awaited()
    if need_call_webhook and scenario in {"state_file", "llm_runtime"}:
        deliver.assert_awaited_once()
    else:
        deliver.assert_not_awaited()
    if app.WORKFLOW_SERVICE._background_tasks:
        await asyncio.gather(*app.WORKFLOW_SERVICE._background_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "duplicate",
        "terminal_reread",
        "foreign_terminal",
        "nonfinal_reread",
        "reread_failure",
        "missing_reread",
        "delivery_failure",
    ],
)
async def test_fail_run_without_attempt_row_finalization_outcomes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, scenario: str
) -> None:
    run = SimpleNamespace(
        workflow_run_id="wr_test", status=WorkflowRunStatus.failed, failure_reason="initialization failed"
    )
    finalization_error = RuntimeError("finalization failed")
    fail_run = AsyncMock(return_value=run)
    reread = AsyncMock(return_value=run)
    webhook = AsyncMock()
    if scenario == "duplicate":
        fail_run.return_value = None
    elif scenario == "delivery_failure":
        webhook.side_effect = RuntimeError("delivery failed")
    else:
        fail_run.side_effect = finalization_error
        if scenario == "nonfinal_reread":
            run.status = WorkflowRunStatus.queued
        elif scenario == "foreign_terminal":
            run.status = WorkflowRunStatus.canceled
        elif scenario == "reread_failure":
            reread.side_effect = RuntimeError("re-read failed")
        elif scenario == "missing_reread":
            reread.return_value = None
    get_attempts = AsyncMock(return_value=[])
    monkeypatch.setattr(
        retry_policy_module,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(
                workflow_run_attempts=SimpleNamespace(get_attempts=get_attempts),
                workflow_runs=SimpleNamespace(get_workflow_run=reread),
            ),
            WORKFLOW_SERVICE=SimpleNamespace(
                mark_workflow_run_as_failed_if_not_final=fail_run,
                execute_workflow_webhook=webhook,
            ),
        ),
    )
    if scenario in {"nonfinal_reread", "reread_failure"}:
        with pytest.raises(RuntimeError) as exc_info:
            await retry_policy_module.fail_run_without_attempt_row("wr_test", "initialization failed")
        assert exc_info.value is finalization_error
        if scenario == "reread_failure":
            assert exc_info.value.__context__ is reread.side_effect
    else:
        assert await retry_policy_module.fail_run_without_attempt_row("wr_test", "initialization failed") is True
    get_attempts.assert_awaited_once_with("wr_test")
    fail_run.assert_awaited_once_with(workflow_run_id="wr_test", failure_reason="initialization failed")
    if scenario in {"duplicate", "delivery_failure"}:
        reread.assert_not_awaited()
    else:
        reread.assert_awaited_once_with("wr_test")
    if scenario in {"terminal_reread", "delivery_failure"}:
        webhook.assert_awaited_once_with(run, api_key=None, claim_kind=None)
    else:
        webhook.assert_not_awaited()
    if scenario == "reread_failure":
        assert "leaving the run recoverable" in caplog.text
    elif scenario == "delivery_failure":
        assert "Failed to deliver workflow webhook after initialization failure" in caplog.text


@pytest.mark.asyncio
async def test_first_dispatch_initializer_failure_is_retried_by_the_sweep(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_test",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="created",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_test", organization_id="org_test", attempt_number=1, status="created"
                ),
            ]
        )
        await session.commit()
    initializer_calls = 0

    async def flaky_initialize(**kwargs: object) -> None:
        nonlocal initializer_calls
        initializer_calls += 1
        if initializer_calls == 1:
            raise RuntimeError("state file unavailable")

    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", flaky_initialize)
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    await executor.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )
    _background_tasks, dispatch, attempt, execution_kwargs = executor._schedule.call_args.args

    # The scheduled dispatch fails to initialize; the run must stay owned by the sweep, not stuck.
    await dispatch(attempt, execution_kwargs)
    execute.assert_not_awaited()
    assert ("wr_test", 1) in executor._retry_dispatches_needing_recovery

    await executor._resume_pending_retry(attempt)
    execute.assert_awaited_once()
    assert execute.await_args is not None
    assert execute.await_args.kwargs["attempt_number"] == 1
    assert not executor._retry_dispatches_needing_recovery


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_fails", [False, True], ids=["lookup-ok", "lookup-fails"])
async def test_lost_first_dispatch_is_recovered_after_a_restart(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch, lookup_fails: bool
) -> None:
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_first",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="created",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_first", organization_id="org_test", attempt_number=1, status="created"
                ),
            ]
        )
        await session.commit()
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    monkeypatch.setattr(sqlite_db.organizations, "get_organization", AsyncMock(return_value=organization))
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", AsyncMock(return_value="absent"))
    monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", AsyncMock(return_value="token"))
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    if lookup_fails:
        real_get_attempts = sqlite_db.workflow_run_attempts.get_attempts
        lookups = iter([RuntimeError("temporary database failure")])

        async def flaky_get_attempts(workflow_run_id: str) -> list[WorkflowRunAttemptModel]:
            failure = next(lookups, None)
            if failure is not None:
                raise failure
            return await real_get_attempts(workflow_run_id)

        monkeypatch.setattr(sqlite_db.workflow_run_attempts, "get_attempts", flaky_get_attempts)
    dispatcher = BackgroundTaskExecutor()
    dispatcher._schedule = MagicMock()  # type: ignore[method-assign]

    await dispatcher.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_first",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )
    dispatched = (await sqlite_db.workflow_run_attempts.get_attempts("wr_first"))[0]
    assert dispatched.status == "queued" and dispatched.started_at is None
    queued_run = await sqlite_db.workflow_runs.get_workflow_run("wr_first")
    assert queued_run is not None and queued_run.status == WorkflowRunStatus.queued and queued_run.started_at is None

    # The process exits before the scheduled dispatch runs; a fresh executor sweeps after the lease window.
    later = background_task_executor_module.naive_utc_now() + timedelta(
        seconds=2 * service_module.LEASE_TAKEOVER_SECONDS
    )
    for module in (service_module, background_task_executor_module, attempts_repository_module):
        monkeypatch.setattr(module, "naive_utc_now", lambda: later)
    recovering = BackgroundTaskExecutor()
    await recovering._recover_pending_retries_once(resume_fresh=False)
    await asyncio.gather(*recovering._background_tasks)

    execute.assert_awaited_once()
    assert execute.await_args is not None
    assert execute.await_args.kwargs["attempt_number"] == 1
    assert execute.await_args.kwargs["prepared_attempt_claimed"] is True
    recovered = (await sqlite_db.workflow_run_attempts.get_attempts("wr_first"))[0]
    assert recovered.status == "running" and recovered.started_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep_claims_first", [False, True], ids=["dispatch_claims", "sweep_claimed_first"])
async def test_first_dispatch_runs_its_attempt_only_when_it_claims_it(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch, sweep_claims_first: bool
) -> None:
    """The initializers run while the attempt is queued. A sweep that claims the attempt in that window
    owns it, so the original dispatch must not execute the attempt as well."""
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_first",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="created",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_first", organization_id="org_test", attempt_number=1, status="created"
                ),
            ]
        )
        await session.commit()
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    monkeypatch.setattr(sqlite_db.organizations, "get_organization", AsyncMock(return_value=organization))
    monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", AsyncMock(return_value="token"))
    sweep_stamp: datetime | None = None

    async def initialize_state(**_kwargs: Any) -> None:
        nonlocal sweep_stamp
        if sweep_claims_first:
            sweep_stamp = await sqlite_db.workflow_run_attempts.claim_prepared_attempt_execution(
                "wr_first", "org_test", 1
            )
            assert sweep_stamp is not None

    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", initialize_state)
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock(return_value=None)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow", execute)
    monkeypatch.setattr(
        service_module, "get_recorded_decision", AsyncMock(return_value=RetryDecision(False, 1, 0, True, "final"))
    )
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", AsyncMock())
    dispatcher = BackgroundTaskExecutor()

    await dispatcher.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_first",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )
    await asyncio.gather(*dispatcher._background_tasks)

    attempt = (await sqlite_db.workflow_run_attempts.get_attempts("wr_first"))[0]
    assert attempt.status == "running" and attempt.started_at is not None
    if sweep_claims_first:
        execute.assert_not_awaited()
        assert attempt.started_at == sweep_stamp
    else:
        execute.assert_awaited_once()
        assert execute.await_args is not None
        assert execute.await_args.kwargs["attempt_number"] == 1
        assert execute.await_args.kwargs["dispatch_claim_started_at"] == attempt.started_at


@pytest.mark.asyncio
async def test_scheduled_run_whose_initializer_fails_is_recovered_after_a_restart(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    fire_time = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
    schedule = SimpleNamespace(
        workflow_schedule_id="wfs_test", workflow_permanent_id="wpid_test", organization_id="org_test", parameters={}
    )
    workflow_run_id = schedule_service_module.build_scheduled_workflow_run_id(schedule.workflow_schedule_id, fire_time)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id=workflow_run_id,
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="created",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id=workflow_run_id, organization_id="org_test", attempt_number=1, status="created"
                ),
            ]
        )
        await session.commit()
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    monkeypatch.setattr(sqlite_db.organizations, "get_organization", AsyncMock(return_value=organization))
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", AsyncMock(return_value="absent"))
    monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", AsyncMock(return_value="token"))
    monkeypatch.setattr(
        schedule_service_module,
        "prepare_workflow",
        AsyncMock(
            return_value=SimpleNamespace(
                workflow_run_id=workflow_run_id, browser_session_id=None, status=WorkflowRunStatus.created
            )
        ),
    )
    monkeypatch.setattr(schedule_service_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(
        schedule_service_module, "prepare_org_llm_runtime", AsyncMock(side_effect=RuntimeError("llm registry down"))
    )
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    scheduler = schedule_service_module.LocalWorkflowScheduleScheduler(poll_interval_seconds=1, max_concurrent_runs=1)

    with pytest.raises(RuntimeError, match="llm registry down"):
        await scheduler._run_schedule(
            schedule_service_module.DueWorkflowSchedule(schedule=schedule, previous_fire_time=fire_time)  # type: ignore[arg-type]
        )
    execute.assert_not_awaited()
    dispatched = (await sqlite_db.workflow_run_attempts.get_attempts(workflow_run_id))[0]
    assert dispatched.status == "queued" and dispatched.started_at is None
    queued_run = await sqlite_db.workflow_runs.get_workflow_run(workflow_run_id)
    assert queued_run is not None and queued_run.status == WorkflowRunStatus.queued

    # The scheduler now treats the fire as delivered; only the dispatch sweep can run the persisted run.
    later = background_task_executor_module.naive_utc_now() + timedelta(
        seconds=2 * service_module.LEASE_TAKEOVER_SECONDS
    )
    for module in (service_module, background_task_executor_module, attempts_repository_module):
        monkeypatch.setattr(module, "naive_utc_now", lambda: later)
    recovering = BackgroundTaskExecutor()
    await recovering._recover_pending_retries_once(resume_fresh=False)
    await asyncio.gather(*recovering._background_tasks)

    execute.assert_awaited_once()
    assert execute.await_args is not None
    assert execute.await_args.kwargs["workflow_run_id"] == workflow_run_id
    assert execute.await_args.kwargs["attempt_number"] == 1
    assert execute.await_args.kwargs["prepared_attempt_claimed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True], ids=["transient", "persistent"])
async def test_first_dispatch_is_not_scheduled_without_a_durable_queued_attempt(
    monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(sequential_credential_id=None, status=WorkflowRunStatus.created)),
    )
    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
    failure = RuntimeError("temporary database failure")
    queue_write = AsyncMock(side_effect=failure if persistent else [failure, True])
    monkeypatch.setattr(app.DATABASE.workflow_runs, "queue_initial_dispatch", queue_write)
    sleep = AsyncMock()
    monkeypatch.setattr(retry_policy_module, "asyncio", ScopedAsyncio(sleep=sleep))
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]
    dispatch_kwargs: dict[str, Any] = {
        "request": None,
        "background_tasks": None,
        "organization": organization,
        "workflow_id": "wf_test",
        "workflow_run_id": "wr_test",
        "workflow_permanent_id": "wpid_test",
        "max_steps_override": None,
        "api_key": None,
        "browser_session_id": None,
        "block_labels": None,
        "block_outputs": None,
    }

    if persistent:
        with pytest.raises(RuntimeError, match="temporary database failure"):
            await executor.execute_workflow(**dispatch_kwargs)
        assert queue_write.await_count == 3
        executor._schedule.assert_not_called()
        assert [call.args[0] for call in sleep.await_args_list] == [0.1, 0.2]
    else:
        await executor.execute_workflow(**dispatch_kwargs)
        assert queue_write.await_count == 2
        executor._schedule.assert_called_once()
        assert [call.args[0] for call in sleep.await_args_list] == [0.1]


@pytest.mark.asyncio
async def test_run_canceled_before_its_first_dispatch_is_not_scheduled(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_canceled",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="canceled",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_canceled",
                    organization_id="org_test",
                    attempt_number=1,
                    status="canceled",
                    retry_decision="revoked",
                ),
            ]
        )
        await session.commit()
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    # The dispatcher's own read still sees the run as created; the cancel commits before the queued write.
    stale_run = SimpleNamespace(sequential_credential_id=None, status=WorkflowRunStatus.created)
    monkeypatch.setattr(sqlite_db.workflow_runs, "get_workflow_run", AsyncMock(return_value=stale_run))
    dispatcher = BackgroundTaskExecutor()
    dispatcher._schedule = MagicMock()  # type: ignore[method-assign]

    await dispatcher.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_canceled",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )

    dispatcher._schedule.assert_not_called()
    async with sqlite_db.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_canceled")
        assert run is not None and run.status == "canceled" and run.queued_at is None
    assert (await sqlite_db.workflow_run_attempts.get_attempts("wr_canceled"))[0].status == "canceled"


@pytest.mark.asyncio
async def test_queue_initial_dispatch_leaves_the_run_created_when_the_attempt_write_fails(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_partial",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="created",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_partial", organization_id="org_test", attempt_number=1, status="created"
                ),
            ]
        )
        await session.commit()
    real_update = workflow_runs_repository_module.update

    def failing_attempt_update(model: Any) -> Any:
        if model is WorkflowRunAttemptModel:
            raise RuntimeError("database connection lost")
        return real_update(model)

    monkeypatch.setattr(workflow_runs_repository_module, "update", failing_attempt_update)

    with pytest.raises(RuntimeError, match="database connection lost"):
        await sqlite_db.workflow_runs.queue_initial_dispatch("wr_partial", 1)

    # Neither row moved: a queued run whose attempt stayed created is invisible to dispatch recovery.
    async with sqlite_db.Session() as session:
        run = await session.get(WorkflowRunModel, "wr_partial")
        assert run is not None and run.status == "created" and run.queued_at is None
    assert (await sqlite_db.workflow_run_attempts.get_attempts("wr_partial"))[0].status == "created"


@pytest.mark.asyncio
async def test_scoped_inputs_are_marked_unrecoverable_before_the_dispatch_becomes_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = SimpleNamespace(organization_id="org_test", default_llm_key=None, default_secondary_llm_key=None)
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(sequential_credential_id=None, status=WorkflowRunStatus.created)),
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(side_effect=RuntimeError("temporary database failure")),
    )
    writes: list[str] = []

    async def mark_unrecoverable(workflow_run_id: str, attempt_number: int) -> None:
        writes.append(f"unrecoverable:{workflow_run_id}:{attempt_number}")

    async def mark_queued(workflow_run_id: str, attempt_number: int) -> bool:
        writes.append(f"queued:{workflow_run_id}:{attempt_number}")
        return True

    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "mark_attempt_inputs_unrecoverable", mark_unrecoverable)
    monkeypatch.setattr(app.DATABASE.workflow_runs, "queue_initial_dispatch", mark_queued)
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    await executor.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=["extract"],
        block_outputs=None,
    )

    # A restart-recovered dispatch has no scoped inputs, so the marker must land before the queued write.
    assert writes == ["unrecoverable:wr_test:1", "queued:wr_test:1"]
    executor._schedule.assert_called_once()


@pytest.mark.asyncio
async def test_execute_workflow_schedules_plain_attempt_when_attempt_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        default_llm_key=None,
        default_secondary_llm_key=None,
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(sequential_credential_id=None, status=WorkflowRunStatus.created)),
    )
    attempt_lookup = AsyncMock(side_effect=RuntimeError("temporary database failure"))
    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", attempt_lookup)
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    # The owner's own bounded lookup fails too; the dispatch must stay owned by the recovery sweep.
    owner = AsyncMock(side_effect=WorkflowRetryAttemptLookupError("execution was not started"))
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", owner)
    executor = BackgroundTaskExecutor()

    await executor.execute_workflow(
        request=None,
        background_tasks=None,
        organization=organization,
        workflow_id="wf_test",
        workflow_run_id="wr_test",
        workflow_permanent_id="wpid_test",
        max_steps_override=None,
        api_key=None,
        browser_session_id=None,
        block_labels=None,
        block_outputs=None,
    )
    await asyncio.gather(*executor._background_tasks)

    attempt_lookup.assert_awaited_once_with("wr_test")
    assert owner.await_args.kwargs["attempt_number"] == 1
    pending_attempt, pending_kwargs = executor._retry_dispatches_needing_recovery[("wr_test", 1)]
    assert pending_attempt.attempt_number == 1
    assert pending_kwargs["attempt_number"] == 1
    assert ("wr_test", 1) in executor._retry_resumes_needing_recovery


@pytest.mark.asyncio
async def test_execute_task_v2_stamps_org_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        default_llm_key="CUSTOM_LLM_oat_smart",
        default_secondary_llm_key="CUSTOM_LLM_oat_fast",
    )
    monkeypatch.setattr(app.DATABASE.organizations, "get_organization", AsyncMock(return_value=organization))
    monkeypatch.setattr(
        app.DATABASE.observer,
        "get_task_v2",
        AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr_test")),
    )
    monkeypatch.setattr(app.DATABASE.observer, "update_task_v2", AsyncMock())
    monkeypatch.setattr(app.DATABASE.workflow_runs, "update_workflow_run", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.sdk.executor.background_task_executor.initialize_skyvern_state_file",
        AsyncMock(),
    )
    load_custom_llms = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.custom_llm_registry.load_custom_llm_configs_for_organization",
        load_custom_llms,
    )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with skyvern_context.scoped(SkyvernContext(organization_id="org_test")) as context:
        await executor.execute_task_v2(
            request=None,
            background_tasks=None,
            organization_id="org_test",
            task_v2_id="task_v2_test",
            max_steps_override=None,
            browser_session_id=None,
        )

        assert context.org_default_llm_key == "CUSTOM_LLM_oat_smart"
        assert context.org_default_secondary_llm_key == "CUSTOM_LLM_oat_fast"

    load_custom_llms.assert_awaited_once_with(app.DATABASE, "org_test")


@pytest.mark.asyncio
async def test_scheduled_task_is_retained_until_done() -> None:
    """A bare create_task reference can be garbage collected mid-flight."""
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()

    executor = BackgroundTaskExecutor()
    executor._schedule(None, work)

    await asyncio.sleep(0)
    assert len(executor._background_tasks) == 1

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert executor._background_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_case", ["no_attempt_rows", "attempt_rows", "webhook_error"])
async def test_execute_workflow_fails_closed_for_stamped_sequential_credential(
    monkeypatch: pytest.MonkeyPatch, delivery_case: str
) -> None:
    workflow_run = SimpleNamespace(sequential_credential_id="cred_sequential", browser_session_id=None)
    terminal_run = SimpleNamespace(
        status=WorkflowRunStatus.failed,
        failure_reason="Sequential credential execution is unavailable in the background executor",
        finished_at=datetime.now(UTC),
    )
    get_run = AsyncMock(side_effect=[workflow_run, terminal_run])
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        get_run,
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[SimpleNamespace(attempt_number=1)] if delivery_case == "attempt_rows" else []),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", mark_failed)
    decision = RetryDecision(False, 1, 0, True, "sequential_credential_unsupported")
    finalize = AsyncMock(return_value=decision)
    monkeypatch.setattr(background_task_executor_module, "finalize_abandoned_attempt", finalize)
    release = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", release)
    webhook = AsyncMock(side_effect=RuntimeError("delivery failed") if delivery_case == "webhook_error" else None)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_webhook", webhook)
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(BackgroundSequentialCredentialUnsupported, match="background executor"):
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=SimpleNamespace(organization_id="org_test"),
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key="test-api-key",
            browser_session_id=None,
            block_labels=None,
            block_outputs=None,
        )

    executor._schedule.assert_not_called()
    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.kwargs["workflow_run_id"] == "wr_test"
    assert mark_failed.await_args.kwargs["cascade_children"] is True
    get_run.assert_awaited_with(workflow_run_id="wr_test", organization_id="org_test")
    if delivery_case == "attempt_rows":
        finalize.assert_awaited_once_with(
            workflow_run_id="wr_test",
            organization_id="org_test",
            reason="sequential_credential_unsupported",
            status=terminal_run.status,
            failure_reason=terminal_run.failure_reason,
            finished_at=terminal_run.finished_at,
        )
        release.assert_awaited_once_with(terminal_run, decision, api_key="test-api-key")
        webhook.assert_not_awaited()
    else:
        webhook.assert_awaited_once_with(terminal_run, api_key="test-api-key", claim_kind=None)
        release.assert_not_awaited()
        finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_closes_forced_session_before_credential_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run = SimpleNamespace(
        sequential_credential_id="cred_sequential",
        browser_session_id="pbs_forced",
    )
    monkeypatch.setattr(
        app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=workflow_run),
    )
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_persistent_browser_session",
        AsyncMock(return_value=SimpleNamespace(runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE)),
    )
    close_session = AsyncMock()
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "close_session", close_session)
    monkeypatch.setattr(
        app.WORKFLOW_SERVICE,
        "mark_workflow_run_as_failed_if_not_final",
        AsyncMock(),
    )
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(SkyvernException, match="background executor"):
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=SimpleNamespace(organization_id="org_test"),
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key=None,
            browser_session_id="pbs_forced",
            block_labels=None,
            block_outputs=None,
        )

    close_session.assert_awaited_once_with("org_test", "pbs_forced", reason=BrowserSessionCloseReason.aborted)
    executor._schedule.assert_not_called()


@pytest.mark.asyncio
async def test_execute_workflow_terminalizes_when_forced_session_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_run = SimpleNamespace(
        sequential_credential_id="cred_sequential",
        browser_session_id="pbs_forced",
    )
    monkeypatch.setattr(app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_persistent_browser_session",
        AsyncMock(return_value=SimpleNamespace(runnable_type=FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE)),
    )
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER,
        "close_session",
        AsyncMock(side_effect=RuntimeError("cleanup failed")),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "mark_workflow_run_as_failed_if_not_final", mark_failed)
    executor = BackgroundTaskExecutor()
    executor._schedule = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(SkyvernException, match="background executor"):
        await executor.execute_workflow(
            request=None,
            background_tasks=None,
            organization=SimpleNamespace(organization_id="org_test"),
            workflow_id="wf_test",
            workflow_run_id="wr_test",
            workflow_permanent_id="wpid_test",
            max_steps_override=None,
            api_key=None,
            browser_session_id="pbs_forced",
            block_labels=None,
            block_outputs=None,
        )

    mark_failed.assert_awaited_once()
    executor._schedule.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("unrecoverable_inputs", [False, True])
async def test_startup_recovers_prepared_retry_without_changing_its_inputs(
    monkeypatch: pytest.MonkeyPatch, unrecoverable_inputs: bool
) -> None:
    attempt = SimpleNamespace(
        workflow_run_id="wr_prepared_resume",
        organization_id="org_test",
        attempt_number=2,
        status="queued",
        started_at=None,
        retry_decision=None,
        decision_reason="unrecoverable_block_outputs" if unrecoverable_inputs else None,
        pinned_browser_session_id="pinned-session",
        modified_at=datetime.now(UTC) - timedelta(seconds=RETRY_DECISION_GRACE_SECONDS + 1),
    )
    run = SimpleNamespace(
        workflow_run_id=attempt.workflow_run_id,
        organization_id=attempt.organization_id,
        status=WorkflowRunStatus.queued,
        started_at=None,
        failure_reason=None,
        finished_at=None,
    )
    organization = SimpleNamespace(organization_id="org_test")
    fail = AsyncMock(return_value=True)
    claim_stamp = datetime.now(UTC)
    database = SimpleNamespace(
        workflow_run_attempts=SimpleNamespace(
            list_attempts_needing_recovery=AsyncMock(return_value=[attempt]),
            list_stale_dispatch_claims=AsyncMock(return_value=[]),
            fail_prepared_workflow_run=fail,
            claim_prepared_attempt_execution=AsyncMock(return_value=claim_stamp),
            get_attempts=AsyncMock(return_value=[attempt]),
        ),
        workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=run)),
        organizations=SimpleNamespace(
            get_organization=AsyncMock(return_value=organization),
            get_valid_org_auth_token=AsyncMock(return_value=SimpleNamespace(token="api-token")),
        ),
    )
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    prepare = AsyncMock()
    monkeypatch.setattr(background_task_executor_module, "prepare_next_attempt_result", prepare)
    abandoned = AsyncMock(return_value=RetryDecision(False, 2, 0, True, "unrecoverable_block_outputs"))
    monkeypatch.setattr(background_task_executor_module, "finalize_abandoned_attempt", abandoned)
    execute = AsyncMock()
    release = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", release)
    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=True)
    await asyncio.gather(*executor._background_tasks)

    prepare.assert_not_awaited()
    if unrecoverable_inputs:
        execute.assert_not_awaited()
        fail.assert_awaited_once()
        abandoned.assert_awaited_once()
        release.assert_awaited_once()
    else:
        fail.assert_not_awaited()
        abandoned.assert_not_awaited()
        execute.assert_awaited_once_with(
            workflow_run_id=attempt.workflow_run_id,
            api_key="api-token",
            organization=organization,
            browser_session_id="pinned-session",
            block_labels=None,
            block_outputs=None,
            need_call_webhook=True,
            attempt_number=2,
            prepared_attempt_claimed=True,
            dispatch_claim_started_at=claim_stamp,
            on_execution_start=ANY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["absent", "running", "unknown"])
async def test_periodic_recovery_resumes_prepared_attempt_after_it_ages(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch, ownership: str
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_aged",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="queued",
                    queued_at=now,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_aged",
                    organization_id="org_test",
                    attempt_number=2,
                    status="queued",
                    modified_at=now,
                ),
            ]
        )
        await session.commit()
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", AsyncMock(return_value=ownership))
    monkeypatch.setattr(
        sqlite_db.organizations, "get_organization", AsyncMock(return_value=SimpleNamespace(organization_id="org_test"))
    )
    monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", AsyncMock(return_value="token"))
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    executor = BackgroundTaskExecutor()
    await executor._recover_pending_retries_once(resume_fresh=True)
    assert not executor._scheduled_retry_resumes
    async with sqlite_db.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_aged", 2))
        attempt.modified_at = now - timedelta(seconds=background_task_executor_module.LEASE_TAKEOVER_SECONDS + 1)
        await session.commit()
    await executor._recover_pending_retries_once(resume_fresh=False)
    await asyncio.gather(*executor._background_tasks)
    assert execute.await_count == int(ownership == "absent")


@pytest.mark.asyncio
async def test_concurrent_prepared_recovery_dispatches_only_claim_winner(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_race",
                    organization_id="org_test",
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    status="queued",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_race", organization_id="org_test", attempt_number=2, status="queued"
                ),
            ]
        )
        await session.commit()
    attempt = (await sqlite_db.workflow_run_attempts.get_attempts("wr_race"))[0]
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", AsyncMock(return_value="absent"))
    monkeypatch.setattr(
        sqlite_db.organizations, "get_organization", AsyncMock(return_value=SimpleNamespace(organization_id="org_test"))
    )
    both_ready = asyncio.Event()
    readers = 0

    async def token(_self: BackgroundTaskExecutor, _org: str) -> str:
        nonlocal readers
        readers += 1
        if readers == 2:
            both_ready.set()
        await both_ready.wait()
        return "token"

    monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", token)
    monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
    monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
    execute = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", execute)
    release = AsyncMock()
    monkeypatch.setattr(app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", release)
    await asyncio.wait_for(
        asyncio.gather(
            BackgroundTaskExecutor()._resume_pending_retry(attempt),
            BackgroundTaskExecutor()._resume_pending_retry(attempt),
        ),
        timeout=5,
    )
    execute.assert_awaited_once()
    release.assert_not_awaited()
    current = (await sqlite_db.workflow_run_attempts.get_attempts("wr_race"))[0]
    assert current.started_at is not None
    assert current.retry_decision is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrance, escaped_exception",
    [("in_process", False), ("in_process", True), ("recovery_pending", False), ("recovery_prepared", False)],
)
@pytest.mark.parametrize(
    "lane, outcome",
    [
        ("browser_session_id", "clear"),
        ("browser_address", "clear"),
        ("sequential_credential_id", "clear"),
        ("sequential_key", "clear"),
        ("reuse_bound_key", "clear"),
        ("run_sequentially", "clear"),
        ("depends_on_workflow_run_id", "clear"),
        ("sequential_credential_id", "cancel"),
        ("sequential_credential_id", "task_cancel"),
        ("sequential_credential_id", "timeout"),
        ("sequential_credential_id", "query_error"),
        ("none", "clear"),
    ],
)
async def test_in_process_retry_reacquires_serialized_lane(
    sqlite_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    entrance: str,
    escaped_exception: bool,
    lane: str,
    outcome: str,
) -> None:
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    svc = app.WORKFLOW_SERVICE
    now = datetime.now(UTC).replace(tzinfo=None)
    identity = {lane: "shared-lane"} if lane not in {"none", "run_sequentially", "depends_on_workflow_run_id"} else {}
    async with sqlite_db.Session() as session:
        session.add(OrganizationModel(organization_id="org_test", organization_name="Test Organization"))
        await session.commit()
        session.add_all(
            [
                WorkflowModel(
                    workflow_id="wf_lane",
                    workflow_permanent_id="wpid_lane",
                    organization_id="org_test",
                    title="Retry lane",
                    version=1,
                    run_sequentially=lane == "run_sequentially",
                    workflow_definition={
                        "blocks": [],
                        "parameters": [],
                        "retry_policy": {"max_retries": 1, "delay_seconds": 0, "retry_on": [{"status": "failed"}]},
                    },
                ),
                WorkflowRunModel(
                    workflow_run_id="wr_retry",
                    workflow_id="wf_lane",
                    workflow_permanent_id="wpid_lane",
                    organization_id="org_test",
                    status="failed",
                    created_at=now - timedelta(seconds=40),
                    started_at=now - timedelta(seconds=30),
                    finished_at=now,
                    max_elapsed_time_minutes=1,
                    depends_on_workflow_run_id="wr_owner" if lane == "depends_on_workflow_run_id" else None,
                    **identity,
                ),
                WorkflowRunModel(
                    workflow_run_id="wr_owner",
                    workflow_id="wf_lane",
                    workflow_permanent_id="wpid_lane",
                    organization_id="org_test",
                    status="running",
                    queued_at=now,
                    **identity,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_retry",
                    organization_id="org_test",
                    attempt_number=1,
                    status="failed",
                    retry_decision="retry",
                    started_at=now - timedelta(seconds=30),
                    finished_at=now,
                    interim_webhook_sent_at=now,
                    next_attempt_at=now,
                    pinned_browser_session_id=identity.get("browser_session_id"),
                ),
            ]
        )
        await session.commit()

    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "remove_workflow_run_context", MagicMock())
    monkeypatch.setattr(svc, "_run_interim_side_effects_with_retries", AsyncMock(return_value="released"))
    final_effects = AsyncMock(return_value="released")
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", final_effects)
    monkeypatch.setattr(app.DATABASE.tasks, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_cascade_child_entities_on_terminal", AsyncMock())
    monkeypatch.setattr(svc, "_sync_task_run_from_workflow_run", AsyncMock())
    waiting = asyncio.Event()
    release = asyncio.Event()
    executed: list[int] = []

    async def execute(**kwargs: object) -> WorkflowRun:
        number = kwargs["attempt_number"]
        assert isinstance(number, int)
        executed.append(number)
        if number == 1:
            if escaped_exception:
                raise RuntimeError("attempt failed")
            return await svc.get_workflow_run("wr_retry", "org_test")
        if lane != "none":
            owner = await svc.get_workflow_run("wr_owner", "org_test")
            assert owner.status == WorkflowRunStatus.completed, "retry started while the serialized lane was occupied"
        completed = await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id="wr_retry", status=WorkflowRunStatus.completed
        )
        assert completed is not None
        await service_module.on_terminal_transition(completed, completed.status, None, None, attempt_number=number)
        return completed

    async def sleep(seconds: float) -> None:
        if seconds == 0:
            await asyncio.sleep(0)
            return
        waiting.set()
        await release.wait()

    monkeypatch.setattr(svc, "execute_workflow", execute)
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))
    clearance_query = sqlite_db.workflow_runs.get_blocking_sequential_workflow_run
    query_failed = False

    async def check_lane(workflow_run_id: str) -> WorkflowRun | None:
        nonlocal query_failed
        assert lane != "none", "unserialized retry must not enter the admission gate"
        if outcome == "query_error" and not query_failed:
            query_failed = True
            raise RuntimeError("primary unavailable")
        return await clearance_query(workflow_run_id)

    monkeypatch.setattr(sqlite_db.workflow_runs, "get_blocking_sequential_workflow_run", check_lane)
    remaining_budget = service_module._get_workflow_run_max_elapsed_timeout_seconds

    def budget(run: WorkflowRun) -> float:
        remaining = remaining_budget(run)
        assert 55 < remaining <= 60, "gate must use the prepared attempt's fresh runtime budget"
        return 0.05 if outcome == "timeout" else remaining

    monkeypatch.setattr(service_module, "_get_workflow_run_max_elapsed_timeout_seconds", budget)
    recovering = entrance != "in_process"
    if recovering:
        monkeypatch.setattr(BackgroundTaskExecutor, "_get_valid_api_key", AsyncMock(return_value="token"))
        monkeypatch.setattr(background_task_executor_module, "initialize_skyvern_state_file", AsyncMock())
        monkeypatch.setattr(background_task_executor_module, "prepare_org_llm_runtime", AsyncMock())
        if entrance == "recovery_prepared":
            preparation = await service_module.prepare_next_attempt_result(
                "wr_retry", "org_test", 1, clear_browser_address=False
            )
            assert preparation.status == "inserted"
        recovered_attempt = (await sqlite_db.workflow_run_attempts.get_attempts("wr_retry"))[-1]
        task = asyncio.create_task(BackgroundTaskExecutor()._resume_pending_retry(recovered_attempt))
    else:
        task = asyncio.create_task(
            svc.execute_workflow_with_retries(
                workflow_run_id="wr_retry", api_key=None, organization=SimpleNamespace(organization_id="org_test")
            )
        )
    wait_task = asyncio.create_task(waiting.wait())
    try:
        if lane == "none":
            await asyncio.wait_for(task, timeout=30)
            result = await svc.get_workflow_run("wr_retry", "org_test")
            assert result.status == WorkflowRunStatus.completed
            assert executed == ([2] if recovering else [1, 2])
            assert not waiting.is_set()
            return

        # A generous bound: the assertion is about ordering, and a loaded CI shard can take seconds to get here.
        done, _pending = await asyncio.wait({task, wait_task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert wait_task in done, "retry did not wait for the occupied lane"
        attempts = await sqlite_db.workflow_run_attempts.get_attempts("wr_retry")
        assert attempts[-1].attempt_number == 2
        assert (attempts[-1].started_at is not None) == (entrance == "recovery_prepared")
        assert executed == ([] if recovering else [1])
        if outcome in {"clear", "query_error"}:
            await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
                workflow_run_id="wr_owner", status=WorkflowRunStatus.completed
            )
            release.set()
        elif outcome == "cancel":
            await svc.mark_workflow_run_as_canceled("wr_retry")
            release.set()
        elif outcome == "task_cancel":
            task.cancel()

        if outcome == "task_cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            result = await svc.get_workflow_run("wr_retry", "org_test")
        else:
            await asyncio.wait_for(task, timeout=5)
            result = await svc.get_workflow_run("wr_retry", "org_test")
        if outcome in {"clear", "query_error"}:
            assert result.status == WorkflowRunStatus.completed
            assert executed == ([2] if recovering else [1, 2])
        elif outcome == "task_cancel":
            # Only shutdown cancels the owner: the prepared attempt stays recoverable, nothing is finalized.
            assert not result.status.is_final()
            assert executed == ([] if recovering else [1])
            attempt = (await sqlite_db.workflow_run_attempts.get_attempts("wr_retry"))[-1]
            assert attempt.attempt_number == 2
            assert attempt.retry_decision is None
            assert (attempt.started_at is not None) == (entrance == "recovery_prepared")
            final_effects.assert_not_awaited()
        else:
            assert result.status == (
                WorkflowRunStatus.timed_out if outcome == "timeout" else WorkflowRunStatus.canceled
            )
            assert executed == ([] if recovering else [1])
            attempt = (await sqlite_db.workflow_run_attempts.get_attempts("wr_retry"))[-1]
            assert (attempt.started_at is not None) == (entrance == "recovery_prepared")
            assert attempt.retry_decision in {"final", "revoked", "abandoned"}
            assert not await sqlite_db.workflow_run_attempts.claim_prepared_attempt_execution("wr_retry", "org_test", 2)
            assert final_effects.await_args.args[1].attempt_number == 2
    finally:
        for pending in [task, wait_task]:
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, wait_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_mark_attempt_started_never_revives_a_finalized_attempt(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel can finalize the attempt between the run's running write and the attempt's; the
    late running write must lose."""
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    async with sqlite_db.Session() as session:
        session.add(OrganizationModel(organization_id="org_test", organization_name="Test Organization"))
        await session.commit()
        session.add_all(
            [
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_live", organization_id="org_test", attempt_number=1, status="queued"
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_canceled",
                    organization_id="org_test",
                    attempt_number=1,
                    status="canceled",
                    retry_decision="revoked",
                    decision_reason="cancel",
                ),
            ]
        )
        await session.commit()

    assert await mark_attempt_started("wr_live", 1) is True
    assert await mark_attempt_started("wr_canceled", 1) is False

    live = (await sqlite_db.workflow_run_attempts.get_attempts("wr_live"))[0]
    canceled = (await sqlite_db.workflow_run_attempts.get_attempts("wr_canceled"))[0]
    assert (live.status, live.started_at is not None) == ("running", True)
    assert (canceled.status, canceled.started_at, canceled.retry_decision) == ("canceled", None, "revoked")


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_decision", ["retry", "final"])
async def test_late_owner_failure_leaves_a_recorded_retry_to_the_next_sweep(
    monkeypatch: pytest.MonkeyPatch, durable_decision: str
) -> None:
    # The decision read after execution is outside the bounded lookup path. Its failure must not
    # strand a recorded retry until the periodic sweep abandons it as stale.
    executor = BackgroundTaskExecutor()
    attempt = SimpleNamespace(workflow_run_id="wr_late", organization_id="org_late", attempt_number=1, started_at=None)
    latest = SimpleNamespace(
        workflow_run_id="wr_late",
        organization_id="org_late",
        attempt_number=2,
        retry_decision=durable_decision,
        next_attempt_prepared_at=None,
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setattr(app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt, latest]))

    async def entry(**kwargs: Any) -> None:
        kwargs["on_execution_start"]()
        raise RuntimeError("decision read unavailable")

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "execute_workflow_with_retries", AsyncMock(side_effect=entry))

    with pytest.raises(RuntimeError, match="decision read unavailable"):
        await executor._dispatch_retry_resume(attempt, {"workflow_run_id": "wr_late", "attempt_number": 2})

    key = ("wr_late", 2)
    assert key not in executor._retry_dispatches_needing_recovery
    assert (key in executor._retry_resumes_needing_recovery) is (durable_decision == "retry")

    scheduled = Mock()
    monkeypatch.setattr(executor, "_schedule_retry_resume", scheduled)
    monkeypatch.setattr(executor, "_recover_stale_dispatch_claims", AsyncMock())
    monkeypatch.setattr(executor, "_release_unreleased_terminal_effects", AsyncMock())

    async def recover(*, process_attempt: Any) -> None:
        await process_attempt(latest, False)

    monkeypatch.setattr(app.WORKFLOW_SERVICE, "recover_pending_workflow_attempts", recover)
    await executor._recover_pending_retries_once(resume_fresh=False)

    if durable_decision == "retry":
        scheduled.assert_called_once_with(latest)
    else:
        scheduled.assert_not_called()
        assert key not in executor._retry_resumes_needing_recovery
