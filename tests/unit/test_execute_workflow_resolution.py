"""Unit tests for execute_workflow's early resolution and terminal short-circuits."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.exceptions import WorkflowNotFoundForWorkflowRun, WorkflowRetryAttemptLookupError
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import WorkflowModel, WorkflowRunAttemptModel, WorkflowRunModel
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import RetryDecision
from skyvern.forge.sdk.workflow.service import WorkflowService
from tests.unit.scoped_asyncio import ScopedAsyncio


class _StopForTest(Exception):
    """Sentinel to abort execute_workflow right after workflow resolution."""


@pytest.mark.asyncio
async def test_execute_workflow_resolves_pinned_definition_including_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _capture_resolution(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise _StopForTest

    workflow_run = SimpleNamespace(
        workflow_permanent_id="wpid_1",
        workflow_id="w_v7",
        status=WorkflowRunStatus.queued,
    )
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    # Latest-by-permanent-id must NOT be used for execution resolution anymore.
    monkeypatch.setattr(
        service,
        "get_workflow_by_permanent_id",
        AsyncMock(side_effect=AssertionError("execution must resolve by run.workflow_id")),
    )
    monkeypatch.setattr(service, "get_workflow_by_workflow_run_id", _capture_resolution)

    organization = SimpleNamespace(organization_id="o_1")
    with pytest.raises(_StopForTest):
        await service.execute_workflow(
            workflow_run_id="wr_1",
            api_key="k",
            organization=cast(Any, organization),
        )

    assert captured == {"workflow_run_id": "wr_1", "organization_id": "o_1", "filter_deleted": False}


@pytest.mark.asyncio
async def test_execute_workflow_canceled_run_skips_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run canceled while queued short-circuits BEFORE workflow resolution, so a run whose
    stamped version was deleted after cancellation does not raise WorkflowNotFound."""
    workflow_run = SimpleNamespace(
        workflow_permanent_id="wpid_1",
        workflow_id="w_deleted",
        status=WorkflowRunStatus.canceled,
    )
    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    get_workflow = AsyncMock(side_effect=AssertionError("must not resolve a canceled run's workflow"))
    monkeypatch.setattr(service, "get_workflow_by_workflow_run_id", get_workflow)

    organization = SimpleNamespace(organization_id="o_1")
    result = await service.execute_workflow(
        workflow_run_id="wr_1",
        api_key="k",
        organization=cast(Any, organization),
    )

    assert result is workflow_run
    get_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_fails_empty_definition_before_marking_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = SimpleNamespace(
        workflow_id="wf_empty",
        workflow_permanent_id="wpid_empty",
        workflow_definition=SimpleNamespace(blocks=[]),
    )
    workflow_run = SimpleNamespace(
        workflow_run_id="wr_empty",
        workflow_id=workflow.workflow_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        browser_profile_id=None,
        browser_session_id=None,
        browser_address=None,
        run_with="agent",
        status=WorkflowRunStatus.created,
    )
    failed_workflow_run = SimpleNamespace(
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_permanent_id=workflow.workflow_permanent_id,
        status=WorkflowRunStatus.failed,
    )

    service = WorkflowService()
    monkeypatch.setattr(service, "get_workflow_run", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(service, "get_workflow_by_workflow_run_id", AsyncMock(return_value=workflow))
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(service, "bind_browser_action_policy", AsyncMock())
    mark_workflow_run_as_running = AsyncMock(
        side_effect=AssertionError("empty workflow should stop before mark_workflow_run_as_running")
    )
    monkeypatch.setattr(service, "mark_workflow_run_as_running", mark_workflow_run_as_running)
    mark_workflow_run_as_failed = AsyncMock(return_value=failed_workflow_run)
    clean_up_workflow = AsyncMock()
    monkeypatch.setattr(service, "mark_workflow_run_as_failed", mark_workflow_run_as_failed)
    monkeypatch.setattr(service, "clean_up_workflow", clean_up_workflow)

    result = await service.execute_workflow(
        workflow_run_id=workflow_run.workflow_run_id,
        api_key="api_key",
        organization=cast(Any, SimpleNamespace(organization_id="o_test")),
    )

    assert result is failed_workflow_run
    mark_workflow_run_as_failed.assert_awaited_once_with(
        workflow_run_id=workflow_run.workflow_run_id,
        failure_reason="Workflow has no executable blocks.",
    )
    clean_up_workflow.assert_awaited_once_with(
        workflow=workflow,
        workflow_run=failed_workflow_run,
        api_key="api_key",
        browser_session_id=None,
        close_browser_on_completion=True,
        need_call_webhook=True,
        attempt_number=1,
    )
    mark_workflow_run_as_running.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_attempt_rows", [True, False])
async def test_cleanup_derives_ownership_when_eligibility_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, has_attempt_rows: bool
) -> None:
    service = WorkflowService()
    run = SimpleNamespace(
        workflow_run_id="wr_1",
        organization_id="o_1",
        status=WorkflowRunStatus.failed,
        failure_reason="temporary failure",
        failure_category=None,
        finished_at=None,
    )
    attempt_rows = [SimpleNamespace(attempt_number=1, retry_decision="final", decision_reason="matched")]
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=attempt_rows if has_attempt_rows else []),
    )
    monkeypatch.setattr(
        service_module, "is_retry_eligible_run", AsyncMock(side_effect=RuntimeError("read unavailable"))
    )
    monkeypatch.setattr(service_module, "mark_stream_closing", Mock())
    monkeypatch.setattr(service_module.analytics, "capture", Mock())
    monkeypatch.setattr(
        service_module, "get_recorded_decision", AsyncMock(return_value=RetryDecision(False, 1, 0, True, "matched"))
    )
    terminal_hook = AsyncMock(side_effect=_StopForTest)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_terminal", terminal_hook)

    with pytest.raises(_StopForTest):
        await service.clean_up_workflow(
            workflow=cast(Any, SimpleNamespace()),
            workflow_run=cast(Any, run),
            need_call_webhook=True,
        )

    terminal_hook.assert_awaited_once()
    assert terminal_hook.await_args.kwargs["is_final_attempt"] is not has_attempt_rows


@pytest.mark.asyncio
async def test_retry_entry_lookup_transient_failure_preserves_cleanup_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WorkflowService()
    run = SimpleNamespace(
        workflow_run_id="wr_1",
        organization_id="o_1",
        status=WorkflowRunStatus.failed,
        failure_reason="temporary failure",
        failure_category=None,
        finished_at=None,
    )
    attempt = SimpleNamespace(attempt_number=1, retry_decision="retry", decision_reason="matched")
    get_attempts = AsyncMock(side_effect=[RuntimeError("read unavailable"), [attempt], [attempt]])
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", get_attempts)
    monkeypatch.setattr(service_module.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(service_module, "is_retry_eligible_run", AsyncMock(return_value=True))
    monkeypatch.setattr(service_module, "mark_stream_closing", Mock())
    monkeypatch.setattr(service_module.analytics, "capture", Mock())
    monkeypatch.setattr(
        service_module.app.AGENT_FUNCTION, "on_workflow_run_terminal", AsyncMock(side_effect=_StopForTest)
    )
    decision = RetryDecision(True, 1, 0, False, "matched")
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(return_value=decision))
    abandon = AsyncMock(return_value=RetryDecision(False, 1, 0, True, "caller_not_retry_aware"))
    monkeypatch.setattr(service_module, "finalize_abandoned_attempt", abandon)
    interim = AsyncMock(return_value="lease_held_by_other_young")
    monkeypatch.setattr(service, "_run_interim_side_effects_with_retries", interim)
    sleep = AsyncMock()
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))

    async def execute(**kwargs: Any) -> Any:
        with pytest.raises(_StopForTest):
            await service.clean_up_workflow(
                workflow=cast(Any, SimpleNamespace()),
                workflow_run=cast(Any, run),
                need_call_webhook=kwargs["need_call_webhook"],
            )
        return run

    execute_mock = AsyncMock(side_effect=execute)
    monkeypatch.setattr(service, "execute_workflow", execute_mock)
    result = await service.execute_workflow_with_retries(
        workflow_run_id="wr_1",
        api_key="k",
        organization=cast(Any, SimpleNamespace(organization_id="o_1")),
    )

    assert result is run
    abandon.assert_not_awaited()
    assert attempt.retry_decision == "retry"
    assert attempt.decision_reason == "matched"
    assert execute_mock.await_args.kwargs["need_call_webhook"] is False
    interim.assert_awaited_once_with(run, decision, api_key="k")
    sleep.assert_awaited_once_with(service_module.WORKFLOW_ATTEMPT_LOOKUP_RETRY_DELAY_SECONDS)
    assert get_attempts.await_count == 3


@pytest.mark.asyncio
async def test_retry_entry_lookup_persistent_failure_preserves_attempt_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkflowService()
    error = RuntimeError("read unavailable")
    attempt = SimpleNamespace(attempt_number=1, retry_decision="retry", decision_reason="matched")
    get_attempts = AsyncMock(side_effect=error)
    repository = SimpleNamespace(get_attempts=get_attempts, attempts=[attempt])
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", repository)
    execute = AsyncMock()
    monkeypatch.setattr(service, "execute_workflow", execute)
    sleep = AsyncMock()
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))

    with pytest.raises(WorkflowRetryAttemptLookupError) as raised:
        await service.execute_workflow_with_retries(
            workflow_run_id="wr_1",
            api_key="k",
            organization=cast(Any, SimpleNamespace(organization_id="o_1")),
        )

    assert raised.value.__cause__ is error
    execute.assert_not_awaited()
    assert get_attempts.await_count == 3
    assert sleep.await_args_list == [call(0.1), call(0.2)]
    assert repository.attempts == [attempt]
    assert vars(attempt) == {"attempt_number": 1, "retry_decision": "retry", "decision_reason": "matched"}


@pytest.mark.asyncio
@pytest.mark.parametrize("need_call_webhook", [True, False])
async def test_retry_entry_lookup_empty_first_attempt_keeps_plain_execution(
    monkeypatch: pytest.MonkeyPatch,
    need_call_webhook: bool,
) -> None:
    service = WorkflowService()
    get_attempts = AsyncMock(return_value=[])
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", get_attempts)
    execute = AsyncMock()
    monkeypatch.setattr(service, "execute_workflow", execute)
    organization = cast(Any, SimpleNamespace(organization_id="o_1"))
    kwargs: dict[str, Any] = {
        "workflow_run_id": "wr_1",
        "api_key": "k",
        "organization": organization,
        "block_labels": ["block_1"],
        "block_outputs": {"prior": 1},
        "browser_session_id": "pbs_1",
        "need_call_webhook": need_call_webhook,
        "workflow_override": None,
        "requested_completion_contract": {"result": "done"},
        "attempt_number": 1,
    }

    result = await service.execute_workflow_with_retries(**kwargs)

    assert result is execute.return_value
    execute.assert_awaited_once_with(**kwargs)
    get_attempts.assert_awaited_once_with("wr_1")


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_loop", [False, True])
async def test_retry_execution_rejects_missing_pinned_version(
    monkeypatch: pytest.MonkeyPatch, sqlite_engine: AsyncEngine, retry_loop: bool
) -> None:
    database = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    async with database.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_1",
                    workflow_id="wf_missing",
                    workflow_permanent_id="wpid_1",
                    organization_id="o_1",
                    status="queued",
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_1", organization_id="o_1", attempt_number=2, status="queued"
                ),
                WorkflowModel(
                    workflow_id="wf_latest",
                    workflow_permanent_id="wpid_1",
                    organization_id="o_1",
                    title="Workflow",
                    workflow_definition={"parameters": [], "blocks": []},
                ),
            ]
        )
        await session.commit()

    service = WorkflowService()
    execute = service.execute_workflow_with_retries if retry_loop else service.execute_workflow
    with pytest.raises(WorkflowNotFoundForWorkflowRun):
        await execute(
            workflow_run_id="wr_1",
            api_key=None,
            organization=cast(Any, SimpleNamespace(organization_id="o_1")),
            attempt_number=2,
        )
    run = await database.workflow_runs.get_workflow_run("wr_1", "o_1")
    assert run is not None
    assert run.status == WorkflowRunStatus.queued
    assert run.workflow_id == "wf_missing"
