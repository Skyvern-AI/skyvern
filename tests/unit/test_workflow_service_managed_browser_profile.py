from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import BrowserSeedSource
from skyvern.forge.sdk.db.models import (
    BrowserProfileModel,
    CredentialModel,
    PersistentBrowserSessionModel,
    TaskRunModel,
    WorkflowModel,
    WorkflowRunAttemptModel,
    WorkflowRunCredentialSelectionModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.repositories import workflow_runs as workflow_runs_repository
from skyvern.forge.sdk.db.repositories.workflow_runs import PrepareNextAttemptResult, WorkflowRunDispatchFinalization
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.schemas.persistent_browser_sessions import BrowserSessionCloseReason
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.browser_profile_key import (
    build_browser_profile_key_digest,
    build_workflow_browser_session_storage_key,
)
from skyvern.forge.sdk.workflow.models.block import BlockType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRequestBody, WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import (
    RETRY_DECISION_FINAL,
    RETRY_DECISION_RETRY,
    RETRY_DECISION_REVOKED,
    RetryDecision,
)
from skyvern.forge.sdk.workflow.service import (
    WorkflowBrowserCleanupResult,
    WorkflowService,
)
from skyvern.schemas.workflows import BlockStatus, WorkflowRetryPolicy
from skyvern.webeye.persistent_session_errors import BrowserSessionCreditAdmissionRefusal
from skyvern.webeye.real_browser_manager import RealBrowserManager
from tests.unit.force_stub_app import make_workflow_run_attempts_fake
from tests.unit.scoped_asyncio import ScopedAsyncio


def _workflow(browser_profile_key: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        persist_browser_session=True,
        reuse_browser_session=False,
        pin_saved_session_ip=False,
        browser_profile_key=browser_profile_key,
        workflow_permanent_id="wpid_test",
        title="Workflow",
    )


def _workflow_run(browser_profile_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_run_id="wr_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_profile_id=browser_profile_id,
        start_fresh_browser=None,
        reuse_browser_session=None,
        proxy_location=None,
    )


def _execute_workflow(*, retry_policy: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        workflow_id="wf_1",
        persist_browser_session=True,
        reuse_browser_session=False,
        workflow_permanent_id="wpid_test",
        title="Workflow",
        organization_id="o_test",
        generate_script_on_terminal=False,
        model=None,
        workflow_definition=SimpleNamespace(
            parameters=[],
            finally_block_label=None,
            blocks=[SimpleNamespace(block_type=BlockType.TASK)],
            retry_policy=retry_policy,
        ),
    )


def _execute_workflow_run(status: WorkflowRunStatus) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        workflow_run_id="wr_test",
        workflow_id="wf_1",
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        browser_session_id=None,
        browser_profile_id="bp_managed",
        browser_seed_source=BrowserSeedSource.own_memory,
        browser_address=None,
        start_fresh_browser=None,
        reuse_browser_session=None,
        reuse_bound_key=None,
        status=status,
        failure_reason=None,
        ignore_inherited_workflow_system_prompt=False,
        parent_workflow_run_id=None,
        depends_on_workflow_run_id=None,
        proxy_location=None,
        max_elapsed_time_minutes=1,
        started_at=now,
        created_at=now,
        code_gen=None,
        debug_session_id=None,
        copilot_session_id=None,
        run_with="agent",
    )


def _browser_cleanup_result() -> WorkflowBrowserCleanupResult:
    browser_state = SimpleNamespace(
        browser_artifacts=SimpleNamespace(browser_session_dir="/tmp/fake_profile"),
        browser_context=SimpleNamespace(),
    )
    return WorkflowBrowserCleanupResult(
        browser_state=browser_state,
        tasks=[],
        all_workflow_task_ids=[],
        child_workflow_run_ids=[],
        close_browser_on_completion=True,
    )


def _patch_execute_workflow_deps(
    monkeypatch: pytest.MonkeyPatch,
    svc: WorkflowService,
    workflow: SimpleNamespace,
    refreshed_run: SimpleNamespace,
) -> None:
    created_run = _execute_workflow_run(WorkflowRunStatus.created)
    running_run = _execute_workflow_run(WorkflowRunStatus.running)
    workflow_run_context = SimpleNamespace(
        browser_session_id=None,
        drain_failure_evidence_capture=AsyncMock(),
    )
    workflow_context_manager = SimpleNamespace(
        initialize_workflow_run_context=AsyncMock(),
        has_workflow_run_context=lambda _workflow_run_id: True,
        get_workflow_run_context=lambda _workflow_run_id: workflow_run_context,
        remove_workflow_run_context=lambda _workflow_run_id: None,
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(
            get_workflow_run=AsyncMock(return_value=refreshed_run),
            update_workflow_run=AsyncMock(),
            # Conditional finalize used by mark_workflow_run_as_failed_if_not_final: returns the
            # run when this caller won the terminal transition, None when it was already final.
            update_workflow_run_if_not_final=AsyncMock(return_value=refreshed_run),
            get_workflow_runs_by_parent_workflow_run_id=AsyncMock(return_value=[]),
        ),
        debug=SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False)),
        artifacts=SimpleNamespace(claim_session_download_artifacts_for_run=AsyncMock(return_value=0)),
        workflow_run_attempts=make_workflow_run_attempts_fake(),
    )

    monkeypatch.setattr(service_module.app, "WORKFLOW_CONTEXT_MANAGER", workflow_context_manager)
    monkeypatch.setattr(service_module.app, "DATABASE", database)
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.workflow_script_service, "workflow_has_conditionals", lambda _workflow: False)
    monkeypatch.setattr(
        service_module.workflow_script_service,
        "get_workflow_script",
        AsyncMock(return_value=(None, None, False)),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module, "is_adaptive_caching", lambda _workflow, _workflow_run: False)

    monkeypatch.setattr(app.AGENT_FUNCTION, "before_workflow_run_start", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=created_run))
    monkeypatch.setattr(svc, "get_workflow_by_workflow_run_id", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "bind_browser_action_policy", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", AsyncMock(return_value=running_run))
    monkeypatch.setattr(service_module, "mark_attempt_started", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "get_workflow_run_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_workflow_output_parameters", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_collect_inherited_workflow_system_prompt", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "auto_create_browser_session_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(return_value=(refreshed_run, set())))
    monkeypatch.setattr(svc, "generate_script_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "should_run_script", AsyncMock(return_value=False))


def _patch_browser_cleanup(monkeypatch: pytest.MonkeyPatch, svc: WorkflowService, order: list[str]) -> AsyncMock:
    clean_up_browser = AsyncMock(side_effect=lambda **_kwargs: order.append("teardown") or _browser_cleanup_result())
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", clean_up_browser)
    return clean_up_browser


def _patch_finalize(
    monkeypatch: pytest.MonkeyPatch,
    svc: WorkflowService,
    order: list[str],
    finalized_run: SimpleNamespace,
) -> None:
    monkeypatch.setattr(
        svc,
        "_finalize_workflow_run_status",
        AsyncMock(side_effect=lambda **_kwargs: order.append("finalize") or finalized_run),
    )


async def _run_execute_workflow(svc: WorkflowService, **kwargs: Any) -> SimpleNamespace:
    return await svc.execute_workflow(
        workflow_run_id="wr_test",
        api_key=None,
        organization=SimpleNamespace(organization_id="o_test"),
        **kwargs,
    )


def _mock_storage(monkeypatch: pytest.MonkeyPatch, *, legacy_dir: str | None) -> AsyncMock:
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", AsyncMock(return_value=legacy_dir))
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)
    return store


@pytest.mark.asyncio
async def test_ensure_managed_browser_profile_returns_managed_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    _mock_storage(monkeypatch, legacy_dir=None)

    result = await WorkflowService()._ensure_managed_browser_profile(
        workflow=_workflow(browser_profile_key="{{ credential_id }}"),
        workflow_run=_workflow_run(),
        parameter_values={"credential_id": "cred_123"},
    )

    assert result == "bp_managed"
    get_or_create.assert_awaited_once_with(
        organization_id="o_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest=build_browser_profile_key_digest("cred_123"),
        name="Workflow (auto-saved: cred_123)",
    )


@pytest.mark.asyncio
async def test_ensure_managed_browser_profile_uses_empty_digest_for_unkeyed_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    _mock_storage(monkeypatch, legacy_dir=None)

    await WorkflowService()._ensure_managed_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    get_or_create.assert_awaited_once_with(
        organization_id="o_test",
        workflow_permanent_id="wpid_test",
        browser_profile_key_digest="",
        name="Workflow (auto-saved session)",
    )


@pytest.mark.asyncio
async def test_ensure_managed_browser_profile_skips_non_persist_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_or_create = AsyncMock()
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    workflow = _workflow()
    workflow.persist_browser_session = False

    result = await WorkflowService()._ensure_managed_browser_profile(
        workflow=workflow,
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    assert result is None
    get_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_managed_browser_profile_seeds_new_profile_from_legacy_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    store = _mock_storage(monkeypatch, legacy_dir="/tmp/legacy_session")

    await WorkflowService()._ensure_managed_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    store.assert_awaited_once_with(
        "o_test",
        profile_id="bp_managed",
        directory="/tmp/legacy_session",
    )


@pytest.mark.asyncio
async def test_ensure_managed_browser_profile_rolls_back_on_seed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    monkeypatch.setattr(
        app.DATABASE.browser_sessions,
        "get_or_create_managed_browser_profile",
        AsyncMock(return_value=(profile, True)),
    )
    hard_delete = AsyncMock()
    monkeypatch.setattr(app.DATABASE.browser_sessions, "hard_delete_browser_profile", hard_delete)
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", AsyncMock(return_value="/tmp/legacy_session"))
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", AsyncMock(side_effect=RuntimeError("upload failed")))

    result = await WorkflowService()._ensure_managed_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    assert result is None
    hard_delete.assert_awaited_once_with(profile_id="bp_managed", organization_id="o_test")


@pytest.mark.asyncio
async def test_ensure_managed_browser_profile_does_not_seed_existing_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(browser_profile_id="bp_managed")
    get_or_create = AsyncMock(return_value=(profile, False))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_or_create_managed_browser_profile", get_or_create)
    retrieve = AsyncMock(return_value="/tmp/legacy_session")
    monkeypatch.setattr(app.STORAGE, "retrieve_browser_session", retrieve)
    store = AsyncMock()
    monkeypatch.setattr(app.STORAGE, "store_browser_profile", store)

    result = await WorkflowService()._ensure_managed_browser_profile(
        workflow=_workflow(),
        workflow_run=_workflow_run(),
        parameter_values={},
    )

    assert result == "bp_managed"
    retrieve.assert_not_awaited()
    store.assert_not_awaited()


def test_legacy_storage_key_matches_managed_digest_for_keyed_workflow() -> None:
    # Read-compat invariant: the managed profile digest and the legacy archive segment must
    # derive from the same rendered key, so seeding finds the right archive.
    rendered = "cred_123"
    storage_key = build_workflow_browser_session_storage_key("wpid_test", rendered)
    assert storage_key.endswith(build_browser_profile_key_digest(rendered))


@pytest.mark.asyncio
async def test_auto_create_browser_session_for_human_interaction_loads_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_session = AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_test"))
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "create_session", create_session)
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            blocks=[SimpleNamespace(block_type=BlockType.HUMAN_INTERACTION, timeout_seconds=60)]
        )
    )

    browser_session = await WorkflowService().auto_create_browser_session_if_needed(
        "o_test",
        workflow,
        browser_profile_id="bp_managed",
    )

    assert browser_session.persistent_browser_session_id == "pbs_test"
    create_session.assert_awaited_once_with(
        organization_id="o_test",
        timeout_minutes=61,
        browser_profile_id="bp_managed",
        proxy_location=None,
        inherit_profile_proxy=True,
    )


@pytest.mark.asyncio
async def test_browser_profile_is_managed_distinguishes_user_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    get_browser_profile = AsyncMock(return_value=SimpleNamespace(is_managed=False))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_browser_profile", get_browser_profile)

    is_managed = await WorkflowService()._browser_profile_is_managed(
        organization_id="o_test",
        browser_profile_id="bp_user",
    )

    assert is_managed is False
    get_browser_profile.assert_awaited_once_with(profile_id="bp_user", organization_id="o_test")


@pytest.mark.asyncio
async def test_browser_profile_is_managed_detects_managed_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    get_browser_profile = AsyncMock(return_value=SimpleNamespace(is_managed=True))
    monkeypatch.setattr(app.DATABASE.browser_sessions, "get_browser_profile", get_browser_profile)

    is_managed = await WorkflowService()._browser_profile_is_managed(
        organization_id="o_test",
        browser_profile_id="bp_managed",
    )

    assert is_managed is True


@pytest.mark.asyncio
async def test_execute_workflow_persists_managed_profile_before_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    clean_up_browser = _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("store")),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook")))

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order == ["teardown", "store", "finalize", "video", "webhook"]
    clean_up_browser.assert_awaited_once()
    svc._persist_workflow_browser_session_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_with_attempt_row_still_records_final_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("store")),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    webhook = AsyncMock(side_effect=lambda *a, **k: order.append("webhook"))
    monkeypatch.setattr(svc, "execute_workflow_webhook", webhook)

    attempt = SimpleNamespace(attempt_number=1, retry_decision=RETRY_DECISION_FINAL)
    service_module.app.DATABASE.workflow_run_attempts.get_attempts = AsyncMock(return_value=[attempt])
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        service_module,
        "get_recorded_decision",
        AsyncMock(return_value=RetryDecision(False, 1, 0, True, RETRY_DECISION_FINAL)),
    )
    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order == ["teardown", "store", "finalize", "video", "webhook"]
    webhook.assert_awaited_once_with(
        completed_run,
        None,
        claim_kind="final",
        skip_side_effects_lease_check=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_replaced", [False, True], ids=["claim_current", "claim_replaced"])
async def test_execute_workflow_with_retries_stops_when_its_dispatch_claim_was_replaced(
    monkeypatch: pytest.MonkeyPatch, claim_replaced: bool
) -> None:
    """The sweep releases a stale dispatch claim and re-issues it to another dispatch. The superseded
    dispatch must neither run blocks nor fail the run its new owner is executing."""
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    final_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, final_run)
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", AsyncMock())
    _patch_finalize(monkeypatch, svc, order, final_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", AsyncMock())
    monkeypatch.setattr(service_module, "mark_attempt_started", AsyncMock(return_value=False))
    claimed_at = datetime.now(UTC).replace(tzinfo=None)
    row_started_at = claimed_at + timedelta(seconds=1) if claim_replaced else claimed_at
    attempt = SimpleNamespace(
        attempt_number=1, retry_decision=None, started_at=row_started_at, pinned_browser_session_id=None
    )
    service_module.app.DATABASE.workflow_run_attempts.get_attempts = AsyncMock(return_value=[attempt])
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        service_module,
        "get_recorded_decision",
        AsyncMock(return_value=RetryDecision(False, 1, 0, True, RETRY_DECISION_FINAL)),
    )
    fail_run = AsyncMock()
    monkeypatch.setattr(svc, "mark_workflow_run_as_failed_if_not_final", fail_run)

    result = await svc.execute_workflow_with_retries(
        workflow_run_id="wr_test",
        api_key=None,
        organization=SimpleNamespace(organization_id="o_test"),
        prepared_attempt_claimed=True,
        dispatch_claim_started_at=claimed_at,
    )

    if claim_replaced:
        # The superseded owner hands back the run as currently read, untouched.
        assert result is svc.get_workflow_run.return_value
        assert svc._execute_workflow_blocks.await_count == 0
    else:
        assert result is final_run
        assert svc._execute_workflow_blocks.await_count == 1
    fail_run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt", "expected_block_runs"),
    [
        (SimpleNamespace(attempt_number=1, retry_decision=RETRY_DECISION_REVOKED, started_at=None), 0),
        (SimpleNamespace(attempt_number=1, retry_decision=None, started_at=datetime.now(UTC)), 1),
    ],
    ids=["cancel_finalized_the_attempt", "activity_re_entered_a_started_attempt"],
)
async def test_execute_workflow_stops_when_the_attempt_was_finalized_before_it_started(
    monkeypatch: pytest.MonkeyPatch,
    attempt: SimpleNamespace,
    expected_block_runs: int,
) -> None:
    """The attempt-start CAS loses to a cancel that lands after the running write; the run must not
    resolve secrets or run blocks after its final webhook. A re-entered activity still continues."""
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    final_status = WorkflowRunStatus.canceled if expected_block_runs == 0 else WorkflowRunStatus.completed
    final_run = _execute_workflow_run(final_status)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, final_run)
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", AsyncMock())
    _patch_finalize(monkeypatch, svc, order, final_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())
    monkeypatch.setattr(service_module, "mark_attempt_started", AsyncMock(return_value=False))
    service_module.app.DATABASE.workflow_run_attempts.get_attempts = AsyncMock(return_value=[attempt])
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        service_module,
        "get_recorded_decision",
        AsyncMock(return_value=RetryDecision(False, 1, 0, True, RETRY_DECISION_FINAL)),
    )

    result = await _run_execute_workflow(svc)

    assert result is final_run
    assert svc._execute_workflow_blocks.await_count == expected_block_runs
    assert svc.get_workflow_run_parameter_tuples.await_count == expected_block_runs


@pytest.mark.asyncio
async def test_execute_workflow_abandons_retry_for_non_retry_aware_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    completed_run.finished_at = datetime.now(UTC)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("store")),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    webhook = AsyncMock(side_effect=lambda *a, **k: order.append("webhook"))
    monkeypatch.setattr(svc, "execute_workflow_webhook", webhook)

    attempt = SimpleNamespace(attempt_number=1, retry_decision=RETRY_DECISION_RETRY)
    service_module.app.DATABASE.workflow_run_attempts.get_attempts = AsyncMock(return_value=[attempt])
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        service_module,
        "get_recorded_decision",
        AsyncMock(return_value=RetryDecision(True, 1, 5, False, None)),
    )
    finalize_abandoned = AsyncMock(return_value=RetryDecision(False, 1, 0, True, "caller_not_retry_aware"))
    monkeypatch.setattr(service_module, "finalize_abandoned_attempt", finalize_abandoned)

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order == ["teardown", "store", "finalize", "video", "webhook"]
    finalize_abandoned.assert_awaited_once_with(
        workflow_run_id="wr_test",
        organization_id="o_test",
        reason="caller_not_retry_aware",
        status=WorkflowRunStatus.completed,
        failure_reason=None,
        finished_at=completed_run.finished_at,
        attempt_number=1,
    )
    webhook.assert_awaited_once_with(
        completed_run,
        None,
        claim_kind="final",
        skip_side_effects_lease_check=True,
    )


@pytest.mark.asyncio
async def test_execute_workflow_with_retries_retries_escaped_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    organization = SimpleNamespace(organization_id="o_test")
    failed_run = _execute_workflow_run(WorkflowRunStatus.failed)
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    attempt_rows = [
        SimpleNamespace(
            attempt_number=1,
            retry_decision=None,
            finished_at=None,
            decision_reason=None,
            next_attempt_at=None,
        )
    ]
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(
                get_attempts=AsyncMock(return_value=attempt_rows),
                claim_prepared_attempt_execution=AsyncMock(return_value=datetime(2026, 1, 2, tzinfo=UTC)),
            ),
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(return_value=completed_run),
            ),
        ),
    )

    async def execute_attempt(**kwargs: Any) -> WorkflowRun:
        kwargs["on_execution_authority"](kwargs["attempt_number"], kwargs["dispatch_claim_started_at"])
        if kwargs["attempt_number"] == 1:
            raise RuntimeError("escaped")
        return completed_run

    execute = AsyncMock(side_effect=execute_attempt)
    monkeypatch.setattr(svc, "execute_workflow", execute)
    mark_failed = AsyncMock(
        return_value=WorkflowRunDispatchFinalization("accepted", failed_run, 1, None, False, transitioned=True)
    )
    monkeypatch.setattr(svc, "finalize_workflow_run_for_dispatch", mark_failed)
    side_effects = AsyncMock()
    monkeypatch.setattr(svc, "run_terminal_side_effects", side_effects)
    decisions = [
        RetryDecision(True, 1, 0, False, "matched"),
        RetryDecision(True, 1, 0, False, "matched"),
        RetryDecision(False, 2, 0, True, "no_match"),
    ]
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(side_effect=decisions))
    prepare = AsyncMock(
        return_value=PrepareNextAttemptResult(
            status="inserted",
            pinned_browser_session_id="browser-2",
        )
    )
    monkeypatch.setattr(service_module, "prepare_next_attempt_result", prepare)

    result = await svc.execute_workflow_with_retries(
        workflow_run_id="wr_test",
        api_key=None,
        organization=organization,
    )

    assert result is completed_run
    assert [call.kwargs["attempt_number"] for call in execute.await_args_list] == [1, 2]
    assert execute.await_args_list[1].kwargs["dispatch_claim_started_at"] == datetime(2026, 1, 2, tzinfo=UTC)
    mark_failed.assert_awaited_once_with(
        workflow_run_id="wr_test",
        failure_reason="escaped",
        organization_id="o_test",
        attempt_number=1,
        dispatch_claim_started_at=None,
        status=WorkflowRunStatus.failed,
        pre_execution=False,
    )
    prepare.assert_awaited_once_with(
        workflow_run_id="wr_test",
        organization_id="o_test",
        from_attempt=1,
        clear_browser_address=False,
    )
    assert [call.args[1] for call in side_effects.await_args_list] == [decisions[0], decisions[2]]


@pytest.mark.asyncio
async def test_execute_workflow_with_retries_leaves_pending_retry_for_recovery_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    organization = SimpleNamespace(organization_id="o_test")
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    completed_run.finished_at = datetime.now(UTC)
    attempt_rows = [
        SimpleNamespace(
            attempt_number=1,
            retry_decision=None,
            finished_at=None,
            decision_reason=None,
            next_attempt_at=None,
        )
    ]
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=attempt_rows)),
            workflow_runs=SimpleNamespace(
                get_workflow_run=AsyncMock(return_value=completed_run),
            ),
        ),
    )
    monkeypatch.setattr(svc, "execute_workflow", AsyncMock(return_value=completed_run))
    retry_decision = RetryDecision(True, 1, 30, False, "matched")
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(return_value=retry_decision))

    side_effects_started = asyncio.Event()

    async def record_side_effects(*_args: Any, **_kwargs: Any) -> None:
        side_effects_started.set()

    side_effects = AsyncMock(side_effect=record_side_effects)
    monkeypatch.setattr(svc, "run_terminal_side_effects", side_effects)
    prepare = AsyncMock()
    monkeypatch.setattr(service_module, "prepare_next_attempt_result", prepare)
    finalize_abandoned = AsyncMock()
    monkeypatch.setattr(service_module, "finalize_abandoned_attempt", finalize_abandoned)

    task = asyncio.create_task(
        svc.execute_workflow_with_retries(
            workflow_run_id="wr_test",
            api_key=None,
            organization=organization,
        )
    )
    await asyncio.wait_for(side_effects_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Shutdown must not convert the durable retry; recovery resumes it on the next start.
    finalize_abandoned.assert_not_awaited()
    assert [call.args[1] for call in side_effects.await_args_list] == [retry_decision]
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_with_retries_sleeps_until_the_recorded_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The decision stamps next_attempt_at before cleanup and interim webhooks run; the owner must not
    # add the full policy delay on top of that work.
    svc = WorkflowService()
    organization = SimpleNamespace(organization_id="o_test")
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    completed_run.finished_at = datetime.now(UTC)
    attempt_rows = [
        SimpleNamespace(
            attempt_number=1, retry_decision=None, finished_at=None, decision_reason=None, next_attempt_at=None
        )
    ]
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=attempt_rows)),
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=completed_run)),
        ),
    )
    monkeypatch.setattr(svc, "execute_workflow", AsyncMock(return_value=completed_run))
    retry_decision = RetryDecision(
        True, 1, 30, False, "matched", next_attempt_at=datetime.now(UTC) + timedelta(seconds=2)
    )
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(return_value=retry_decision))
    monkeypatch.setattr(svc, "run_terminal_side_effects", AsyncMock())
    monkeypatch.setattr(svc, "_prepare_recorded_retry", AsyncMock(return_value=None))
    sleep = AsyncMock()
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(sleep=sleep))

    result = await svc.execute_workflow_with_retries(workflow_run_id="wr_test", api_key=None, organization=organization)

    assert result is completed_run
    (slept,) = (call.args[0] for call in sleep.await_args_list)
    assert 0 < slept <= 2


@pytest.mark.asyncio
async def test_execute_workflow_with_retries_re_raises_escaped_exception_after_final_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    organization = SimpleNamespace(organization_id="o_test")
    running_run = _execute_workflow_run(WorkflowRunStatus.running)
    failed_run = _execute_workflow_run(WorkflowRunStatus.failed)
    attempt_rows = [SimpleNamespace(attempt_number=1)]
    get_run = AsyncMock(return_value=running_run)
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=attempt_rows)),
            workflow_runs=SimpleNamespace(get_workflow_run=get_run),
        ),
    )
    error = RuntimeError("escaped")

    async def execute_attempt(**kwargs: Any) -> WorkflowRun:
        kwargs["on_execution_authority"](kwargs["attempt_number"], kwargs["dispatch_claim_started_at"])
        raise error

    monkeypatch.setattr(svc, "execute_workflow", AsyncMock(side_effect=execute_attempt))
    mark_failed = AsyncMock(
        return_value=WorkflowRunDispatchFinalization("accepted", failed_run, 1, None, False, transitioned=True)
    )
    monkeypatch.setattr(svc, "finalize_workflow_run_for_dispatch", mark_failed)
    side_effects = AsyncMock()
    monkeypatch.setattr(svc, "run_terminal_side_effects", side_effects)
    final_decision = RetryDecision(False, 1, 0, True, "no_match")
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(return_value=final_decision))

    with pytest.raises(RuntimeError, match="escaped") as raised:
        await svc.execute_workflow_with_retries(
            workflow_run_id="wr_test",
            api_key=None,
            organization=organization,
        )

    assert raised.value is error
    get_run.assert_not_awaited()
    mark_failed.assert_awaited_once_with(
        workflow_run_id="wr_test",
        failure_reason="escaped",
        organization_id="o_test",
        attempt_number=1,
        dispatch_claim_started_at=None,
        status=WorkflowRunStatus.failed,
        pre_execution=False,
    )
    side_effects.assert_awaited_once()
    assert side_effects.await_args.args == (failed_run, final_decision)
    assert side_effects.await_args.kwargs["api_key"] is None


@pytest.mark.asyncio
async def test_cleanup_claims_session_downloads_from_retry_attempt_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    workflow_run = _execute_workflow_run(WorkflowRunStatus.completed)
    workflow_run.created_at = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    attempt_started_at = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    attempt = SimpleNamespace(attempt_number=2, started_at=attempt_started_at)
    claim = AsyncMock(return_value=1)
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=[attempt])),
            debug=SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False)),
            artifacts=SimpleNamespace(claim_session_download_artifacts_for_run=claim),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            on_workflow_run_terminal=AsyncMock(),
            is_block_scoped_workflow_run=AsyncMock(return_value=False),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(
            remove_workflow_run_context=lambda _workflow_run_id: None,
            has_workflow_run_context=lambda _workflow_run_id: False,
        ),
    )
    monkeypatch.setattr(
        service_module.skyvern_context,
        "current",
        lambda: SkyvernContext(browser_session_id="bs_retry", run_id=workflow_run.workflow_run_id),
    )
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(
        service_module, "get_recorded_decision", AsyncMock(return_value=RetryDecision(False, 2, 0, True, "final"))
    )
    monkeypatch.setattr(svc, "run_terminal_side_effects", AsyncMock())

    await svc.clean_up_workflow(
        workflow=workflow,
        workflow_run=workflow_run,
        browser_cleanup_result=WorkflowBrowserCleanupResult(
            browser_state=None,
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        ),
        attempt_number=2,
    )

    claim.assert_awaited_once_with(
        run_id=workflow_run.workflow_run_id,
        browser_session_id="bs_retry",
        organization_id=workflow_run.organization_id,
        run_started_at=attempt_started_at,
    )


@pytest.mark.asyncio
async def test_cleanup_uses_legacy_path_when_retry_attempt_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    workflow = _execute_workflow()
    workflow_run = _execute_workflow_run(WorkflowRunStatus.completed)
    get_attempts = AsyncMock(side_effect=RuntimeError("attempt lookup unavailable"))
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=get_attempts),
            debug=SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False)),
        ),
    )
    terminal_hook = AsyncMock()
    monkeypatch.setattr(
        service_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            on_workflow_run_terminal=terminal_hook,
            is_block_scoped_workflow_run=AsyncMock(return_value=False),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(remove_workflow_run_context=Mock(), has_workflow_run_context=lambda _workflow_run_id: False),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    save_downloads = AsyncMock()
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", save_downloads)
    wait_for_uploads = AsyncMock()
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", wait_for_uploads)
    delete_attachments = AsyncMock()
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)
    legacy_webhook = AsyncMock()
    monkeypatch.setattr(svc, "execute_workflow_webhook", legacy_webhook)
    policy_effects = AsyncMock()
    monkeypatch.setattr(svc, "run_terminal_side_effects", policy_effects)

    await svc.clean_up_workflow(
        workflow=workflow,
        workflow_run=workflow_run,
        browser_cleanup_result=WorkflowBrowserCleanupResult(
            browser_state=None,
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        ),
        schedule_credential_fallback_retry=False,
    )

    get_attempts.assert_awaited_once_with(workflow_run.workflow_run_id)
    terminal_hook.assert_awaited_once_with(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=workflow_run.organization_id,
        status=workflow_run.status,
        is_final_attempt=True,
    )
    wait_for_uploads.assert_awaited_once_with([])
    save_downloads.assert_awaited_once_with(
        organization_id=workflow_run.organization_id,
        run_id=workflow_run.workflow_run_id,
    )
    delete_attachments.assert_awaited_once_with(run_id=workflow_run.workflow_run_id)
    legacy_webhook.assert_awaited_once_with(workflow_run, None)
    policy_effects.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_skips_logical_effects_when_policy_attempt_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = WorkflowService()
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    workflow_run = _execute_workflow_run(WorkflowRunStatus.completed)
    get_attempts = AsyncMock(side_effect=RuntimeError("attempt lookup unavailable"))
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=get_attempts),
            debug=SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False)),
        ),
    )
    terminal_hook = AsyncMock()
    monkeypatch.setattr(
        service_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            on_workflow_run_terminal=terminal_hook,
            is_block_scoped_workflow_run=AsyncMock(return_value=False),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(remove_workflow_run_context=Mock(), has_workflow_run_context=lambda _workflow_run_id: False),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    delete_attachments = AsyncMock()
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)
    legacy_webhook = AsyncMock()
    monkeypatch.setattr(svc, "execute_workflow_webhook", legacy_webhook)
    policy_effects = AsyncMock()
    monkeypatch.setattr(svc, "run_terminal_side_effects", policy_effects)
    credential_fallback = Mock()
    monkeypatch.setattr(svc, "_schedule_credential_fallback_retry", credential_fallback)

    await svc.clean_up_workflow(
        workflow=workflow,
        workflow_run=workflow_run,
        browser_cleanup_result=WorkflowBrowserCleanupResult(
            browser_state=None,
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        ),
    )

    terminal_hook.assert_awaited_once_with(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=workflow_run.organization_id,
        status=workflow_run.status,
        is_final_attempt=False,
    )
    delete_attachments.assert_not_awaited()
    legacy_webhook.assert_not_awaited()
    policy_effects.assert_not_awaited()
    credential_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_uses_legacy_path_when_policy_run_has_no_attempt_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Enrollment is the attempt row. A definition with a policy but no row ran the ordinary path, so
    # cleanup owes the legacy webhook and attachment deletion; no recovery sweep can supply them later.
    svc = WorkflowService()
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    workflow_run = _execute_workflow_run(WorkflowRunStatus.completed)
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=[])),
            debug=SimpleNamespace(has_block_run_for_workflow_run=AsyncMock(return_value=False)),
        ),
    )
    terminal_hook = AsyncMock()
    monkeypatch.setattr(
        service_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            on_workflow_run_terminal=terminal_hook,
            is_block_scoped_workflow_run=AsyncMock(return_value=False),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(remove_workflow_run_context=Mock(), has_workflow_run_context=lambda _workflow_run_id: False),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    delete_attachments = AsyncMock()
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)
    legacy_webhook = AsyncMock()
    monkeypatch.setattr(svc, "execute_workflow_webhook", legacy_webhook)
    policy_effects = AsyncMock()
    monkeypatch.setattr(svc, "run_terminal_side_effects", policy_effects)

    await svc.clean_up_workflow(
        workflow=workflow,
        workflow_run=workflow_run,
        browser_cleanup_result=WorkflowBrowserCleanupResult(
            browser_state=None,
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        ),
        schedule_credential_fallback_retry=False,
    )

    terminal_hook.assert_awaited_once_with(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=workflow_run.organization_id,
        status=workflow_run.status,
        is_final_attempt=True,
    )
    delete_attachments.assert_awaited_once_with(run_id=workflow_run.workflow_run_id)
    legacy_webhook.assert_awaited_once_with(workflow_run, None)
    policy_effects.assert_not_awaited()


async def _assert_policy_definition_uses_legacy_cleanup_for_ineligible_run(
    monkeypatch: pytest.MonkeyPatch,
    workflow_run: SimpleNamespace,
    *,
    block_scoped: bool,
) -> None:
    svc = WorkflowService()
    workflow = _execute_workflow(retry_policy=WorkflowRetryPolicy(max_retries=1, retry_on=[{"status": "failed"}]))
    attempt = SimpleNamespace(attempt_number=1, retry_decision=RETRY_DECISION_FINAL)
    terminal_hook = AsyncMock()
    delete_attachments = AsyncMock()
    legacy_webhook = AsyncMock()
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=[attempt])),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "AGENT_FUNCTION",
        SimpleNamespace(
            on_workflow_run_terminal=terminal_hook,
            is_block_scoped_workflow_run=AsyncMock(return_value=block_scoped),
        ),
    )
    monkeypatch.setattr(
        service_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        SimpleNamespace(remove_workflow_run_context=Mock(), has_workflow_run_context=lambda _workflow_run_id: False),
    )
    monkeypatch.setattr(service_module.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(service_module.app.STORAGE, "save_downloaded_files", AsyncMock())
    monkeypatch.setattr(service_module.app.ARTIFACT_MANAGER, "wait_for_upload_aiotasks", AsyncMock())
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)
    monkeypatch.setattr(svc, "execute_workflow_webhook", legacy_webhook)
    policy_effects = AsyncMock()
    monkeypatch.setattr(svc, "run_terminal_side_effects", policy_effects)

    await svc.clean_up_workflow(
        workflow=workflow,
        workflow_run=workflow_run,
        browser_cleanup_result=WorkflowBrowserCleanupResult(
            browser_state=None,
            tasks=[],
            all_workflow_task_ids=[],
            child_workflow_run_ids=[],
            close_browser_on_completion=True,
        ),
        schedule_credential_fallback_retry=False,
    )

    terminal_hook.assert_awaited_once_with(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=workflow_run.organization_id,
        status=workflow_run.status,
        is_final_attempt=True,
    )
    delete_attachments.assert_awaited_once_with(run_id=workflow_run.workflow_run_id)
    legacy_webhook.assert_awaited_once_with(workflow_run, None)
    policy_effects.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_scoped_policy_run_uses_legacy_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_run = _execute_workflow_run(WorkflowRunStatus.completed)
    await _assert_policy_definition_uses_legacy_cleanup_for_ineligible_run(
        monkeypatch,
        workflow_run,
        block_scoped=True,
    )


@pytest.mark.asyncio
async def test_child_policy_run_uses_legacy_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow_run = _execute_workflow_run(WorkflowRunStatus.completed)
    workflow_run.parent_workflow_run_id = "parent_run"
    await _assert_policy_definition_uses_legacy_cleanup_for_ineligible_run(
        monkeypatch,
        workflow_run,
        block_scoped=False,
    )


@pytest.mark.asyncio
async def test_retry_decision_rejects_non_terminal_status() -> None:
    with pytest.raises(ValueError, match="non-terminal status: running"):
        await service_module.on_terminal_transition(
            SimpleNamespace(workflow_run_id="wr_test"),
            WorkflowRunStatus.running,
            None,
            None,
        )


@pytest.mark.asyncio
async def test_filter_downloaded_files_keeps_identical_redownload_touched_in_current_attempt() -> None:
    svc = WorkflowService()
    attempt_started_at = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    files = [
        FileInfo(
            url="https://example.test/old.pdf",
            checksum="old",
            filename="only-attempt-1.pdf",
            modified_at=attempt_started_at.replace(hour=11, minute=59),
            artifact_id="a_old",
        ),
        FileInfo(
            url="https://example.test/report.pdf",
            checksum="same",
            filename="report.pdf",
            modified_at=attempt_started_at,
            artifact_id="a_report",
        ),
    ]

    filtered = svc._filter_downloaded_files_to_attempt(
        files,
        attempt_rows=[SimpleNamespace(attempt_number=2, started_at=attempt_started_at)],
        attempt_number=2,
        artifact_ids={"a_report"},
    )

    assert [file_info.filename for file_info in filtered] == ["report.pdf"]


def test_filter_downloaded_files_bounds_an_unstarted_attempt_by_its_creation() -> None:
    svc = WorkflowService()
    prepared_at = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
    files = [
        FileInfo(
            url="https://example.test/old.pdf",
            checksum="old",
            filename="attempt-1.pdf",
            modified_at=prepared_at.replace(hour=11),
            artifact_id="a_old",
        ),
        FileInfo(
            url="https://example.test/claimed.pdf",
            checksum="claimed",
            filename="claimed.pdf",
            modified_at=prepared_at.replace(hour=11, minute=30),
            artifact_id="a_claimed",
        ),
        FileInfo(url="https://example.test/unknown.pdf", checksum="unknown", filename="unknown.pdf"),
    ]
    attempt_rows = [
        SimpleNamespace(
            attempt_number=1, started_at=prepared_at.replace(hour=10), created_at=prepared_at.replace(hour=9)
        ),
        SimpleNamespace(attempt_number=2, started_at=None, created_at=prepared_at),
    ]

    filtered = svc._filter_downloaded_files_to_attempt(
        files, attempt_rows=attempt_rows, attempt_number=2, artifact_ids={"a_claimed"}
    )

    assert [file_info.filename for file_info in filtered] == ["claimed.pdf"]
    # The live attempt keeps the permissive rule for a file whose timestamp is unknown.
    live = svc._filter_downloaded_files_to_attempt(
        files[2:], attempt_rows=attempt_rows[:1], attempt_number=1, artifact_ids=set()
    )
    assert [file_info.filename for file_info in live] == ["unknown.pdf"]


@pytest.mark.asyncio
async def test_execute_workflow_persists_profile_when_only_finally_block_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    workflow.workflow_definition.finally_block_label = "cleanup"
    failed_run = _execute_workflow_run(WorkflowRunStatus.failed)
    failed_run.failure_reason = "cleanup failed"
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(
        svc,
        "_execute_finally_block_if_configured",
        AsyncMock(
            return_value=(
                SimpleNamespace(label="cleanup", block_type="cloud_storage", continue_on_failure=False),
                SimpleNamespace(
                    status=BlockStatus.failed,
                    failure_reason="cleanup failed",
                    output_parameter_value=None,
                ),
            )
        ),
    )
    apply_finally_result = AsyncMock(
        return_value=(failed_run, WorkflowRunStatus.failed, failed_run.failure_reason),
    )
    monkeypatch.setattr(svc, "_apply_finally_block_result", apply_finally_result)
    clean_up_browser = _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("store")),
    )
    _patch_finalize(monkeypatch, svc, order, failed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook")))

    result = await _run_execute_workflow(svc)

    assert result is failed_run
    assert order == ["teardown", "store", "finalize", "video", "webhook"]
    clean_up_browser.assert_awaited_once()
    svc._persist_workflow_browser_session_if_needed.assert_awaited_once()
    assert apply_finally_result.await_args is not None
    assert apply_finally_result.await_args.kwargs["pre_finally_status"] == WorkflowRunStatus.running
    assert apply_finally_result.await_args.kwargs["defer_status_write"] is True


@pytest.mark.asyncio
async def test_execute_workflow_defers_finally_failure_status_until_after_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    workflow.workflow_definition.finally_block_label = "cleanup"
    running_run = _execute_workflow_run(WorkflowRunStatus.running)
    failed_run = _execute_workflow_run(WorkflowRunStatus.failed)
    failed_run.failure_reason = "cloud_storage block failed. failure reason: upload failed"
    finally_block = SimpleNamespace(
        block_type="cloud_storage",
        label="cleanup",
        continue_on_failure=False,
    )
    finally_result = SimpleNamespace(
        status=BlockStatus.failed,
        failure_reason="upload failed",
        output_parameter_value=None,
    )

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, running_run)
    monkeypatch.setattr(
        svc,
        "_execute_finally_block_if_configured",
        AsyncMock(return_value=(finally_block, finally_result)),
    )
    status_write = AsyncMock(side_effect=RuntimeError("status write must remain deferred"))
    monkeypatch.setattr(svc, "_update_workflow_run_status_if_not_final", status_write)
    order: list[str] = []
    _patch_browser_cleanup(monkeypatch, svc, order)
    persist_browser_session = AsyncMock(side_effect=lambda **_kwargs: order.append("store"))
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        persist_browser_session,
    )

    async def finalize_side_effect(**_kwargs: object) -> SimpleNamespace:
        order.append("finalize")
        return failed_run

    finalize = AsyncMock(side_effect=finalize_side_effect)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize)
    clean_up = AsyncMock()
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up)

    result = await _run_execute_workflow(svc)

    assert result is failed_run
    assert order == ["teardown", "store", "finalize"]
    status_write.assert_not_awaited()
    persist_browser_session.assert_awaited_once()
    assert persist_browser_session.await_args is not None
    assert persist_browser_session.await_args.kwargs["workflow_run_status"] == WorkflowRunStatus.completed
    finalize.assert_awaited_once()
    assert finalize.await_args is not None
    assert finalize.await_args.kwargs["pre_finally_status"] == WorkflowRunStatus.failed
    assert (
        finalize.await_args.kwargs["pre_finally_failure_reason"]
        == "cloud_storage block failed. failure reason: upload failed"
    )
    assert finalize.await_args.kwargs["pre_finally_failure_category"]
    clean_up.assert_awaited_once()
    assert clean_up.await_args is not None
    assert clean_up.await_args.kwargs["browser_persistence_status"] == WorkflowRunStatus.completed
    assert clean_up.await_args.kwargs["schedule_credential_fallback_retry"] is False


@pytest.mark.asyncio
async def test_execute_workflow_skips_profile_persistence_when_timeout_precedes_body_status_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    running_run = _execute_workflow_run(WorkflowRunStatus.running)
    timed_out_run = _execute_workflow_run(WorkflowRunStatus.timed_out)
    timed_out_run.failure_reason = "Workflow exceeded its elapsed-time limit."

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, running_run)

    async def slow_refresh(**_kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0.05)
        return running_run

    service_module.app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(side_effect=slow_refresh)
    timeout_seconds = iter([10.0, 0.01])
    monkeypatch.setattr(
        service_module,
        "_get_workflow_run_max_elapsed_timeout_seconds",
        lambda _workflow_run: next(timeout_seconds),
    )
    monkeypatch.setattr(
        svc,
        "_shield_post_run_elapsed_timeout",
        AsyncMock(
            return_value=(
                timed_out_run,
                WorkflowRunStatus.timed_out,
                timed_out_run.failure_reason,
            )
        ),
    )
    prefinal_browser_cleanup = AsyncMock()
    persist_browser_session = AsyncMock()
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", prefinal_browser_cleanup)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", persist_browser_session)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", AsyncMock(return_value=timed_out_run))
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "clean_up_workflow", AsyncMock())
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

    result = await _run_execute_workflow(svc)

    assert result is timed_out_run
    prefinal_browser_cleanup.assert_not_awaited()
    persist_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_rechecks_terminal_winner_before_healthy_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    running_run = _execute_workflow_run(WorkflowRunStatus.running)
    timed_out_run = _execute_workflow_run(WorkflowRunStatus.timed_out)
    timed_out_run.failure_reason = "Workflow exceeded its elapsed-time limit."

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, running_run)
    initial_run = svc.get_workflow_run.return_value
    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(side_effect=[initial_run, timed_out_run]))
    prefinal_browser_cleanup = AsyncMock()
    persist_browser_session = AsyncMock()
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", prefinal_browser_cleanup)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", persist_browser_session)
    finalize = AsyncMock(return_value=timed_out_run)
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", finalize)
    clean_up = AsyncMock()
    monkeypatch.setattr(svc, "clean_up_workflow", clean_up)

    result = await _run_execute_workflow(svc)

    assert result is timed_out_run
    prefinal_browser_cleanup.assert_not_awaited()
    persist_browser_session.assert_not_awaited()
    finalize.assert_awaited_once()
    assert finalize.await_args is not None
    assert finalize.await_args.kwargs["workflow"] is workflow
    clean_up.assert_awaited_once()
    assert clean_up.await_args is not None
    assert clean_up.await_args.kwargs["browser_persistence_status"] == WorkflowRunStatus.timed_out


@pytest.mark.asyncio
async def test_execute_workflow_retries_write_back_when_pre_final_persist_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []
    persist_calls = {"n": 0}

    async def persist_side_effect(**_kwargs: object) -> None:
        persist_calls["n"] += 1
        if persist_calls["n"] == 1:
            order.append("store_fail")
            raise RuntimeError("storage down")
        order.append("store_retry")

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", AsyncMock(side_effect=persist_side_effect))
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook")))

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    # Sequential dependents remain blocked until the write-back retry has also completed.
    assert order == ["teardown", "store_fail", "store_retry", "finalize", "video", "webhook"]
    assert svc._persist_workflow_browser_session_if_needed.await_count == 2


@pytest.mark.asyncio
async def test_execute_workflow_exhausts_write_back_before_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []
    persist_calls = {"n": 0}

    async def persist_side_effect(**_kwargs: object) -> None:
        persist_calls["n"] += 1
        order.append(f"store_fail_{persist_calls['n']}")
        raise RuntimeError("storage down")

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    _patch_browser_cleanup(monkeypatch, svc, order)
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", AsyncMock(side_effect=persist_side_effect))
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=lambda *a, **k: order.append("webhook")))

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order == ["teardown", "store_fail_1", "store_fail_2", "finalize", "video", "webhook"]
    assert svc._persist_workflow_browser_session_if_needed.await_count == 2


@pytest.mark.asyncio
async def test_execute_workflow_does_not_prestore_blob_for_failed_canceled_or_timed_out_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for status in (WorkflowRunStatus.failed, WorkflowRunStatus.canceled, WorkflowRunStatus.timed_out):
        workflow = _execute_workflow()
        terminal_run = _execute_workflow_run(status)
        order: list[str] = []

        svc = WorkflowService()
        _patch_execute_workflow_deps(monkeypatch, svc, workflow, terminal_run)
        _patch_browser_cleanup(monkeypatch, svc, order)
        monkeypatch.setattr(
            service_module.app.STORAGE,
            "store_browser_profile",
            AsyncMock(side_effect=lambda *a, **k: order.append("store_profile")),
        )
        monkeypatch.setattr(service_module.app.STORAGE, "store_browser_session", AsyncMock())
        _patch_finalize(monkeypatch, svc, order, terminal_run)
        monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
        monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

        result = await _run_execute_workflow(svc)

        assert result is terminal_run
        assert order[:2] == ["finalize", "teardown"]
        assert "store_profile" not in order


@pytest.mark.asyncio
async def test_execute_workflow_non_persist_workflow_writes_sink_before_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    workflow.persist_browser_session = False
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    clean_up_browser = _patch_browser_cleanup(monkeypatch, svc, order)
    persist_browser_session = AsyncMock(side_effect=lambda **_kwargs: order.append("store"))
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", persist_browser_session)
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock(side_effect=lambda *a, **k: order.append("video")))
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order[:3] == ["teardown", "store", "finalize"]
    clean_up_browser.assert_awaited_once()
    persist_browser_session.assert_awaited_once()
    assert persist_browser_session.await_args is not None
    assert persist_browser_session.await_args.kwargs["workflow_run_status"] == WorkflowRunStatus.completed


@pytest.mark.asyncio
async def test_execute_workflow_finalizes_when_pre_status_browser_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    def cleanup_side_effect(**_kwargs: object) -> WorkflowBrowserCleanupResult:
        if "teardown_error" not in order:
            order.append("teardown_error")
            raise RuntimeError("cleanup failed")
        order.append("teardown_cleanup")
        return _browser_cleanup_result()

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", AsyncMock(side_effect=cleanup_side_effect))
    monkeypatch.setattr(
        svc,
        "_persist_workflow_browser_session_if_needed",
        AsyncMock(side_effect=lambda **_kwargs: order.append("persist_helper")),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    assert order[:4] == ["teardown_error", "teardown_cleanup", "persist_helper", "finalize"]
    assert svc._clean_up_workflow_browser.await_count == 2


def _patch_session_backed_run(monkeypatch: pytest.MonkeyPatch, svc: WorkflowService) -> AsyncMock:
    monkeypatch.setattr(
        service_module.skyvern_context,
        "ensure_context",
        lambda: SkyvernContext(
            browser_session_id=None,
            browser_session_runnable_id=None,
            browser_session_runnable_generation_id=None,
        ),
    )
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "begin_session", AsyncMock(return_value="lease_1"))
    close_session = AsyncMock()
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "close_session", close_session)
    monkeypatch.setattr(svc, "persist_video_data", AsyncMock())
    monkeypatch.setattr(svc, "_persist_workflow_browser_session_if_needed", AsyncMock())
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock())
    return close_session


@pytest.mark.asyncio
async def test_execute_workflow_does_not_close_caller_supplied_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_session_backed_run(monkeypatch, svc)
    _patch_browser_cleanup(monkeypatch, svc, order)
    _patch_finalize(monkeypatch, svc, order, completed_run)

    result = await _run_execute_workflow(svc, browser_session_id="pbs_caller")

    assert result is completed_run
    close_session.assert_not_awaited()


def _patch_human_interaction_session(monkeypatch: pytest.MonkeyPatch, svc: WorkflowService) -> AsyncMock:
    close_session = _patch_session_backed_run(monkeypatch, svc)
    # The fixture run carries a browser profile; the auto-create only runs for managed profiles.
    monkeypatch.setattr(svc, "_browser_profile_is_managed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        svc,
        "auto_create_browser_session_if_needed",
        AsyncMock(return_value=SimpleNamespace(persistent_browser_session_id="pbs_human")),
    )
    return close_session


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [WorkflowRunStatus.completed, WorkflowRunStatus.failed])
async def test_execute_workflow_closes_auto_created_human_interaction_session(
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: WorkflowRunStatus,
) -> None:
    workflow = _execute_workflow()
    terminal_run = _execute_workflow_run(terminal_status)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_human_interaction_session(monkeypatch, svc)
    _patch_browser_cleanup(monkeypatch, svc, order)
    _patch_finalize(monkeypatch, svc, order, terminal_run)

    result = await _run_execute_workflow(svc)

    assert result is terminal_run
    close_session.assert_awaited_once_with("o_test", "pbs_human", reason=BrowserSessionCloseReason.run_ended)


@pytest.mark.asyncio
async def test_execute_workflow_completes_cleanup_when_owned_session_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_human_interaction_session(monkeypatch, svc)
    close_session.side_effect = RuntimeError("close failed")
    _patch_browser_cleanup(monkeypatch, svc, order)
    _patch_finalize(monkeypatch, svc, order, completed_run)

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    close_session.assert_awaited_once()
    svc.execute_workflow_webhook.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_workflow_closes_owned_session_when_begin_session_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    failed_run = _execute_workflow_run(WorkflowRunStatus.failed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_human_interaction_session(monkeypatch, svc)
    monkeypatch.setattr(
        app.PERSISTENT_SESSIONS_MANAGER, "begin_session", AsyncMock(side_effect=RuntimeError("begin failed"))
    )
    monkeypatch.setattr(svc, "mark_workflow_run_as_failed", AsyncMock(return_value=failed_run))
    _patch_browser_cleanup(monkeypatch, svc, order)

    result = await _run_execute_workflow(svc)

    assert result is failed_run
    close_session.assert_awaited_once_with("o_test", "pbs_human", reason=BrowserSessionCloseReason.run_ended)


@pytest.mark.asyncio
async def test_owned_session_close_deferred_while_child_runs_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_human_interaction_session(monkeypatch, svc)
    cleanup_result = _browser_cleanup_result()
    cleanup_result.child_workflow_run_ids = ["wr_child"]
    monkeypatch.setattr(
        svc,
        "_clean_up_workflow_browser",
        AsyncMock(side_effect=lambda **_kwargs: order.append("teardown") or cleanup_result),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[_execute_workflow_run(WorkflowRunStatus.running)]),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_session_closes_when_child_runs_are_all_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_human_interaction_session(monkeypatch, svc)
    cleanup_result = _browser_cleanup_result()
    cleanup_result.child_workflow_run_ids = ["wr_child"]
    monkeypatch.setattr(
        svc,
        "_clean_up_workflow_browser",
        AsyncMock(side_effect=lambda **_kwargs: order.append("teardown") or cleanup_result),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_runs_by_parent_workflow_run_id",
        AsyncMock(return_value=[_execute_workflow_run(WorkflowRunStatus.completed)]),
    )
    _patch_finalize(monkeypatch, svc, order, completed_run)

    result = await _run_execute_workflow(svc)

    assert result is completed_run
    close_session.assert_awaited_once_with("o_test", "pbs_human", reason=BrowserSessionCloseReason.run_ended)


@pytest.mark.asyncio
async def test_execute_workflow_closes_owned_session_when_cancelled_mid_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    order: list[str] = []

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_human_interaction_session(monkeypatch, svc)
    monkeypatch.setattr(svc, "_execute_workflow_blocks", AsyncMock(side_effect=asyncio.CancelledError()))
    _patch_browser_cleanup(monkeypatch, svc, order)
    _patch_finalize(monkeypatch, svc, order, _execute_workflow_run(WorkflowRunStatus.canceled))

    with pytest.raises(asyncio.CancelledError):
        await _run_execute_workflow(svc)

    close_session.assert_awaited_once_with("o_test", "pbs_human", reason=BrowserSessionCloseReason.run_ended)


@pytest.mark.asyncio
async def test_clean_up_workflow_closes_owned_session_when_webhook_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _execute_workflow()
    completed_run = _execute_workflow_run(WorkflowRunStatus.completed)

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.running))
    close_session = _patch_session_backed_run(monkeypatch, svc)
    monkeypatch.setattr(svc, "execute_workflow_webhook", AsyncMock(side_effect=RuntimeError("webhook down")))

    with pytest.raises(RuntimeError, match="webhook down"):
        await svc.clean_up_workflow(
            workflow=workflow,
            workflow_run=completed_run,
            api_key=None,
            browser_session_id="pbs_human",
            browser_cleanup_result=_browser_cleanup_result(),
            owned_browser_session_id="pbs_human",
        )

    close_session.assert_awaited_once_with("o_test", "pbs_human", reason=BrowserSessionCloseReason.run_ended)


@pytest_asyncio.fixture
async def forced_session_setup(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: AsyncEngine,
) -> tuple[AgentDB, WorkflowService, Workflow]:
    database = AgentDB("sqlite+aiosqlite://", db_engine=sqlite_engine)
    await database.organizations.create_organization("Test", organization_id="o_test")
    async with database.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_1",
                workflow_permanent_id="wpid_test",
                organization_id="o_test",
                title="Workflow",
                workflow_definition={
                    "parameters": [],
                    "blocks": [],
                    "retry_policy": {"retry_on": [{"status": "failed"}], "delay_seconds": 0},
                },
            )
        )
        await session.commit()
    workflow = await database.workflows.get_workflow("wf_1", organization_id="o_test")
    assert workflow is not None
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app.WORKFLOW_CONTEXT_MANAGER, "remove_workflow_run_context", Mock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_block_scoped_workflow_run", AsyncMock(return_value=False))
    monkeypatch.setattr(
        app.EXPERIMENTATION_PROVIDER,
        "is_feature_enabled_cached",
        AsyncMock(side_effect=lambda flag, *args, **kwargs: flag == "FORCE_BROWSER_SESSION"),
    )

    async def create_session(**kwargs: Any) -> SimpleNamespace:
        async with database.Session() as session:
            if await session.get(PersistentBrowserSessionModel, "pbs_forced") is None:
                session.add(
                    PersistentBrowserSessionModel(
                        persistent_browser_session_id="pbs_forced",
                        organization_id="o_test",
                        status="running",
                        runnable_type="forced_workflow_run",
                    )
                )
                await session.commit()
        run = await database.workflow_runs.get_workflow_run(kwargs["workflow_run_id"], "o_test")
        if run is not None and (run.reuse_bound_key or "").startswith("off:force_pending:"):
            await database.workflow_runs.update_workflow_run(kwargs["workflow_run_id"], browser_session_id="pbs_forced")
        return SimpleNamespace(persistent_browser_session_id="pbs_forced")

    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "create_session", AsyncMock(side_effect=create_session))
    service = WorkflowService()
    monkeypatch.setattr(service, "_resolve_managed_browser_profile_for_run_request", AsyncMock(return_value=None))
    return database, service, workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("forced_session", [True, False])
async def test_block_scoped_setup_does_not_enroll_a_retry_attempt(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    forced_session: bool,
) -> None:
    """A block run creates its block-run rows only after setup, so enrolment must trust the
    caller's block-scoped intent; a policy definition alone must not enroll it."""
    database, service, workflow = forced_session_setup
    monkeypatch.setattr(
        app.EXPERIMENTATION_PROVIDER,
        "is_feature_enabled_cached",
        AsyncMock(side_effect=lambda flag, *args, **kwargs: forced_session and flag == "FORCE_BROWSER_SESSION"),
    )
    block_run = await service.create_workflow_run(
        workflow_request=WorkflowRequestBody(),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        organization_id="o_test",
        workflow=workflow,
        block_scoped=True,
    )
    full_run = await service.create_workflow_run(
        workflow_request=WorkflowRequestBody(),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        organization_id="o_test",
        workflow=workflow,
    )
    assert await database.workflow_run_attempts.get_attempts(block_run.workflow_run_id) == []
    enrolled = await database.workflow_run_attempts.get_attempts(full_run.workflow_run_id)
    assert [attempt.attempt_number for attempt in enrolled] == [1]


@pytest_asyncio.fixture(params=["sequential", "browser_reuse"])
async def retry_gate_setup(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    request: pytest.FixtureRequest,
) -> tuple[AgentDB, WorkflowService, WorkflowRun]:
    database, service, _ = forced_session_setup
    async with database.Session() as session:
        workflow_model = await session.get(WorkflowModel, "wf_1")
        assert workflow_model is not None
        workflow_model.run_sequentially = request.param == "sequential"
        workflow_model.reuse_browser_session = request.param == "browser_reuse"
        workflow_model.workflow_definition = {
            "parameters": [],
            "blocks": [],
            "retry_policy": {"retry_on": [{"status": "timed_out"}], "delay_seconds": 0, "max_retries": 1},
        }
        await session.commit()
    workflow = await database.workflows.get_workflow("wf_1", organization_id="o_test")
    assert workflow is not None
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False))
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", service)
    monkeypatch.setattr(service, "_run_interim_side_effects_with_retries", AsyncMock(return_value="released"))
    monkeypatch.setattr(service, "_run_terminal_side_effects_with_retries", AsyncMock(return_value="released"))
    run = await service.create_workflow_run(
        workflow_request=WorkflowRequestBody(start_fresh_browser=False),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        organization_id="o_test",
        workflow=workflow,
        max_elapsed_time_minutes=1,
    )
    expired_start = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=2)
    run = await database.workflow_runs.update_workflow_run(
        run.workflow_run_id, status=WorkflowRunStatus.running, started_at=expired_start
    )
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, (run.workflow_run_id, 1))
        assert attempt is not None
        attempt.started_at = expired_start
        await session.commit()

    async def mark_timed_out(workflow_run_id: str, failure_reason: str) -> WorkflowRun:
        return await database.workflow_runs.update_workflow_run(
            workflow_run_id,
            status=WorkflowRunStatus.timed_out,
            failure_reason=failure_reason,
            finished_at=datetime.now(UTC).replace(tzinfo=None),
        )

    monkeypatch.setattr(service, "mark_workflow_run_as_timed_out", mark_timed_out)
    return database, service, run


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("escaped_exception", [False, True])
async def test_timed_out_retry_gets_fresh_admission_budget(
    monkeypatch: pytest.MonkeyPatch,
    retry_gate_setup: tuple[AgentDB, WorkflowService, WorkflowRun],
    blocked: bool,
    escaped_exception: bool,
) -> None:
    database, service, run = retry_gate_setup
    executed_attempts: list[int] = []
    admission_budgets: list[float] = []
    gate_sleeps: list[float] = []

    async def execute_attempt(*, attempt_number: int, **kwargs: Any) -> WorkflowRun:
        kwargs["on_execution_authority"](attempt_number, kwargs["dispatch_claim_started_at"])
        executed_attempts.append(attempt_number)
        result = await database.workflow_runs.update_workflow_run(
            run.workflow_run_id,
            status=WorkflowRunStatus.timed_out if attempt_number == 1 else WorkflowRunStatus.completed,
        )
        if attempt_number == 1 and escaped_exception:
            raise RuntimeError("attempt timed out")
        return result

    admission_timeout: asyncio.Timeout | None = None

    def record_admission_timeout(seconds: float) -> asyncio.Timeout:
        nonlocal admission_timeout
        admission_budgets.append(seconds)
        admission_timeout = asyncio.timeout(None)
        return admission_timeout

    async def expire_timeout_at_gate_sleep(seconds: float) -> None:
        if seconds > 0:
            gate_sleeps.append(seconds)
            assert admission_timeout is not None
            # Cancel at the gate sleep, after the database read and session cleanup complete.
            admission_timeout.reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(0)

    monkeypatch.setattr(service, "execute_workflow", execute_attempt)
    monkeypatch.setattr(
        database.workflow_runs, "get_blocking_sequential_workflow_run", AsyncMock(return_value=run if blocked else None)
    )
    monkeypatch.setattr(
        service_module,
        "asyncio",
        ScopedAsyncio(timeout=record_admission_timeout, sleep=expire_timeout_at_gate_sleep),
    )

    result = await service.execute_workflow_with_retries(
        run.workflow_run_id, api_key=None, organization=SimpleNamespace(organization_id="o_test")
    )

    assert len(admission_budgets) == 1
    assert 55 < admission_budgets[0] <= 60
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert attempts[0].status == "timed_out"
    assert attempts[0].retry_decision == RETRY_DECISION_RETRY
    if blocked:
        assert gate_sleeps
        assert executed_attempts == [1]
        assert result.status == WorkflowRunStatus.timed_out
        assert attempts[1].started_at is None
        assert attempts[1].decision_reason == "sequential_gate_stopped"
    else:
        assert not gate_sleeps
        assert executed_attempts == [1, 2]
        assert result.status == WorkflowRunStatus.completed
        assert attempts[1].started_at is not None


@pytest.mark.asyncio
async def test_prepared_retry_recovery_keeps_its_admission_deadline(
    monkeypatch: pytest.MonkeyPatch,
    retry_gate_setup: tuple[AgentDB, WorkflowService, WorkflowRun],
) -> None:
    database, service, run = retry_gate_setup
    admission_deadlines: list[datetime] = []
    admission_budgets: list[float] = []
    elapsed_timeout = service_module._get_workflow_run_max_elapsed_timeout_seconds

    class WorkerLost(BaseException):
        pass

    def record_budget(gate_run: WorkflowRun) -> float:
        assert gate_run.started_at is not None
        admission_deadlines.append(gate_run.started_at.replace(tzinfo=UTC) + timedelta(minutes=1))
        remaining = elapsed_timeout(gate_run)
        admission_budgets.append(remaining)
        return remaining

    async def execute_attempt(**kwargs: Any) -> WorkflowRun:
        return await database.workflow_runs.update_workflow_run(run.workflow_run_id, status=WorkflowRunStatus.timed_out)

    execute = AsyncMock(side_effect=execute_attempt)
    monkeypatch.setattr(service, "execute_workflow", execute)
    monkeypatch.setattr(service_module, "_get_workflow_run_max_elapsed_timeout_seconds", record_budget)
    monkeypatch.setattr(
        database.workflow_runs, "get_blocking_sequential_workflow_run", AsyncMock(side_effect=WorkerLost)
    )

    with pytest.raises(WorkerLost):
        await service.execute_workflow_with_retries(
            run.workflow_run_id, api_key=None, organization=SimpleNamespace(organization_id="o_test")
        )

    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    prepared_at = attempts[1].created_at.replace(tzinfo=UTC)
    assert attempts[1].started_at is None
    assert 55 < admission_budgets[0] <= 60
    current_time = prepared_at + timedelta(seconds=30)

    class RecoveryClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return current_time.astimezone(tz) if tz is not None else current_time.replace(tzinfo=None)

    monkeypatch.setattr(service_module, "datetime", RecoveryClock)
    with pytest.raises(WorkerLost):
        await service.execute_workflow_with_retries(
            run.workflow_run_id, api_key=None, organization=SimpleNamespace(organization_id="o_test"), attempt_number=2
        )

    assert admission_budgets[1] == 30
    current_time = prepared_at + timedelta(minutes=1)
    result = await service.execute_workflow_with_retries(
        run.workflow_run_id, api_key=None, organization=SimpleNamespace(organization_id="o_test"), attempt_number=2
    )

    assert admission_deadlines == [prepared_at + timedelta(minutes=1)] * 3
    assert admission_budgets[2] == 0
    assert result.status == WorkflowRunStatus.timed_out
    assert [call.kwargs["attempt_number"] for call in execute.await_args_list] == [1]
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert attempts[1].started_at is None
    assert attempts[1].decision_reason == "sequential_gate_stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped_exception", [False, True])
async def test_forced_session_is_pinned_for_first_and_prepared_retry_attempt(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    escaped_exception: bool,
) -> None:
    database, service, workflow = forced_session_setup

    run = await service.create_workflow_run(
        workflow_request=WorkflowRequestBody(),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        organization_id="o_test",
        workflow=workflow,
    )

    assert run.browser_session_id == "pbs_forced"
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].pinned_browser_session_id == "pbs_forced"

    context = skyvern_context.SkyvernContext(workflow_run_id=run.workflow_run_id, organization_id="o_test")
    monkeypatch.setattr(skyvern_context, "current", lambda: context)
    monkeypatch.setattr(skyvern_context, "ensure_context", lambda: context)
    monkeypatch.setattr(app, "BROWSER_MANAGER", RealBrowserManager())
    generations: list[str] = []
    released_generations: list[str] = []
    browser_accesses: list[tuple[int, str | None]] = []
    owned_generation: str | None = None

    async def begin_session(**kwargs: Any) -> str:
        nonlocal owned_generation
        assert kwargs["browser_session_id"] == "pbs_forced"
        assert kwargs["runnable_id"] == run.workflow_run_id
        assert owned_generation is None
        owned_generation = f"lease_{len(generations) + 1}"
        generations.append(owned_generation)
        return owned_generation

    async def release_session(**kwargs: Any) -> bool:
        nonlocal owned_generation
        assert kwargs["expected_runnable_id"] == run.workflow_run_id
        assert kwargs["expected_runnable_generation_id"] == owned_generation
        assert owned_generation is not None
        released_generations.append(owned_generation)
        owned_generation = None
        return True

    async def execute_attempt(*, attempt_number: int, browser_session_id: str | None, **kwargs: Any) -> WorkflowRun:
        kwargs["on_execution_authority"](attempt_number, kwargs["dispatch_claim_started_at"])
        current_run = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "o_test")
        assert current_run is not None
        session_id = browser_session_id or current_run.browser_session_id
        assert session_id == "pbs_forced"
        if attempt_number > 1:
            assert context.browser_session_id == session_id
        await service._ensure_browser_session_lease(
            organization_id="o_test", workflow_run_id=run.workflow_run_id, browser_session_id=session_id
        )
        generation = context.browser_session_runnable_generation_id
        browser_accesses.append((attempt_number, generation))
        owns_session = (
            context.browser_session_runnable_id == run.workflow_run_id
            and generation == owned_generation
            and generation not in released_generations
        )
        if owns_session:
            await service._clean_up_workflow_browser(
                workflow_run=current_run, close_browser_on_completion=False, browser_session_id=session_id
            )
        status = WorkflowRunStatus.completed if attempt_number == 2 and owns_session else WorkflowRunStatus.failed
        current_run = await database.workflow_runs.update_workflow_run(run.workflow_run_id, status=status)
        async with database.Session() as session:
            attempt = await session.scalar(
                select(WorkflowRunAttemptModel).where(
                    WorkflowRunAttemptModel.workflow_run_id == run.workflow_run_id,
                    WorkflowRunAttemptModel.attempt_number == attempt_number,
                )
            )
            assert attempt is not None
            attempt.status = status.value
            attempt.retry_decision = RETRY_DECISION_RETRY if attempt_number == 1 else RETRY_DECISION_FINAL
            await session.commit()
        if attempt_number == 1 and escaped_exception:
            raise RuntimeError("escaped after browser cleanup")
        return current_run

    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "begin_session", begin_session)
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "release_browser_session", release_session)
    monkeypatch.setattr(service, "execute_workflow", execute_attempt)
    monkeypatch.setattr(service, "_wait_for_retry_sequential_clearance", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_run_interim_side_effects_with_retries", AsyncMock(return_value="released"))
    monkeypatch.setattr(service, "_run_terminal_side_effects_with_retries", AsyncMock(return_value="released"))

    result = await service.execute_workflow_with_retries(
        run.workflow_run_id, api_key=None, organization=SimpleNamespace(organization_id="o_test")
    )

    assert browser_accesses == [(1, "lease_1"), (2, "lease_2")]
    assert generations == released_generations == ["lease_1", "lease_2"]
    assert result.status == WorkflowRunStatus.completed
    assert context.browser_session_id == "pbs_forced"
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert [(attempt.attempt_number, attempt.pinned_browser_session_id) for attempt in attempts] == [
        (1, "pbs_forced"),
        (2, "pbs_forced"),
    ]
    prepared_run = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "o_test")
    assert prepared_run is not None
    assert prepared_run.browser_session_id == "pbs_forced"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_source", ["forced", "explicit", "unpinned"])
async def test_retry_preparation_preserves_session_after_transient_forced_pin_failure(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    session_source: str,
) -> None:
    database, service, workflow = forced_session_setup
    pin_session = database.workflow_run_attempts.pin_first_attempt_browser_session_if_unset
    pin_calls = 0

    async def fail_first_pin(**kwargs: Any) -> None:
        nonlocal pin_calls
        pin_calls += 1
        if pin_calls == 1:
            raise RuntimeError("pin failed")
        await pin_session(**kwargs)

    monkeypatch.setattr(database.workflow_run_attempts, "pin_first_attempt_browser_session_if_unset", fail_first_pin)
    if session_source != "forced":
        monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False))
    if session_source == "explicit":
        monkeypatch.setattr(
            database.browser_sessions,
            "get_persistent_browser_session",
            AsyncMock(return_value=SimpleNamespace(browser_profile_id=None, runnable_id="wr_other")),
        )
    run = await service.create_workflow_run(
        workflow_request=WorkflowRequestBody(
            browser_session_id="pbs_explicit" if session_source == "explicit" else None
        ),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        organization_id="o_test",
        workflow=workflow,
    )
    await database.workflow_runs.update_workflow_run(
        run.workflow_run_id,
        status=WorkflowRunStatus.failed,
        browser_session_id="pbs_leased" if session_source == "unpinned" else run.browser_session_id,
    )
    async with database.Session() as session:
        attempt = await session.get(WorkflowRunAttemptModel, (run.workflow_run_id, 1))
        assert attempt is not None
        attempt.status = WorkflowRunStatus.failed.value
        attempt.retry_decision = RETRY_DECISION_RETRY
        await session.commit()

    preparation = await service_module.prepare_next_attempt_result(run.workflow_run_id, "o_test", 1)

    expected_session_id = {"forced": "pbs_forced", "explicit": "pbs_explicit", "unpinned": None}[session_source]
    assert preparation.status == "inserted"
    assert preparation.pinned_browser_session_id == expected_session_id
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert [attempt.pinned_browser_session_id for attempt in attempts] == [expected_session_id, expected_session_id]
    reopened = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "o_test")
    assert reopened is not None
    assert reopened.browser_session_id == expected_session_id
    assert pin_calls == (2 if session_source == "forced" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["assignment", "pin"])
async def test_forced_session_assignment_precedes_required_pin(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    failure_point: str,
) -> None:
    database, service, workflow = forced_session_setup
    close_session = AsyncMock()
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "close_session", close_session)
    if failure_point == "assignment":
        monkeypatch.setattr(
            database.workflow_runs, "update_workflow_run", AsyncMock(side_effect=RuntimeError("assignment failed"))
        )
    else:
        monkeypatch.setattr(
            database.workflow_run_attempts,
            "pin_first_attempt_browser_session_if_unset",
            AsyncMock(side_effect=RuntimeError("pin failed")),
        )

    if failure_point == "pin":
        with pytest.raises(RuntimeError, match="pin failed"):
            await service.create_workflow_run(
                workflow_request=WorkflowRequestBody(),
                workflow_permanent_id="wpid_test",
                workflow_id="wf_1",
                workflow_run_id="wr_pin_failure",
                organization_id="o_test",
                workflow=workflow,
            )
        run = await database.workflow_runs.get_workflow_run("wr_pin_failure", "o_test")
        assert run is not None
        assert run.status == WorkflowRunStatus.failed
        assert run.finished_at is not None
        assert run.failure_reason == "Failed to pin forced browser session for workflow retry"
        close_session.assert_awaited_once_with("o_test", "pbs_forced", reason=BrowserSessionCloseReason.aborted)
        assert database.workflow_run_attempts.pin_first_attempt_browser_session_if_unset.await_count == 2
    else:
        run = await service.create_workflow_run(
            workflow_request=WorkflowRequestBody(),
            workflow_permanent_id="wpid_test",
            workflow_id="wf_1",
            organization_id="o_test",
            workflow=workflow,
        )

    persisted_run = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "o_test")
    assert persisted_run is not None
    expected_session_id = None if failure_point == "assignment" else "pbs_forced"
    assert run.browser_session_id == persisted_run.browser_session_id == expected_session_id
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert len(attempts) == 1
    assert attempts[0].pinned_browser_session_id is None
    if failure_point == "pin":
        assert attempts[0].status == WorkflowRunStatus.failed.value
        assert attempts[0].retry_decision == "abandoned"


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted_version", [False, True])
@pytest.mark.parametrize("seed_source", [BrowserSeedSource.credential, BrowserSeedSource.own_memory])
async def test_retry_execution_resolves_rotated_seed_before_browser_creation(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_engine: AsyncEngine,
    deleted_version: bool,
    seed_source: BrowserSeedSource,
) -> None:
    database = AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)
    now = datetime.now(UTC)
    credential_parameter = {
        "parameter_type": "credential",
        "key": "login",
        "credential_parameter_id": "cp_login",
        "workflow_id": "wf_1",
        "credential_id": "cred_a",
        "credential_ids": ["cred_a", "cred_b"],
        "created_at": now.isoformat(),
        "modified_at": now.isoformat(),
    }
    own_memory = seed_source == BrowserSeedSource.own_memory
    async with database.Session() as session:
        session.add_all(
            [
                WorkflowModel(
                    workflow_id="wf_1",
                    workflow_permanent_id="wpid_test",
                    organization_id="o_test",
                    title="Workflow",
                    persist_browser_session=own_memory,
                    browser_profile_key="{{ login }}" if own_memory else None,
                    workflow_definition={
                        "parameters": [credential_parameter],
                        "blocks": [
                            {
                                "block_type": "login",
                                "label": "login",
                                "parameters": [credential_parameter],
                                "output_parameter": {
                                    "parameter_type": "output",
                                    "key": "login_output",
                                    "output_parameter_id": "op_login",
                                    "workflow_id": "wf_1",
                                    "created_at": now.isoformat(),
                                    "modified_at": now.isoformat(),
                                },
                            }
                        ],
                        "retry_policy": {"max_retries": 1, "retry_on": [{"status": "failed"}]},
                    },
                ),
                WorkflowRunModel(
                    workflow_run_id="wr_test",
                    workflow_id="wf_1",
                    workflow_permanent_id="wpid_test",
                    organization_id="o_test",
                    status="failed",
                    browser_profile_id="bp_a",
                    browser_seed_source=seed_source,
                    browser_sink_profile_id="bp_a" if own_memory else None,
                    max_elapsed_time_minutes=1,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_test",
                    organization_id="o_test",
                    attempt_number=1,
                    status="failed",
                    retry_decision="retry",
                ),
                WorkflowRunCredentialSelectionModel(
                    workflow_run_id="wr_test",
                    organization_id="o_test",
                    workflow_permanent_id="wpid_test",
                    parameter_key="login",
                    credential_id="cred_a",
                ),
                *[
                    CredentialModel(
                        credential_id=f"cred_{suffix}",
                        organization_id="o_test",
                        name=f"Credential {suffix}",
                        credential_type="password",
                        item_id=f"item_{suffix}",
                        run_sequentially=False,
                        browser_profile_id=None if own_memory else f"bp_{suffix}",
                    )
                    for suffix in ("a", "b")
                ],
                *[
                    BrowserProfileModel(
                        browser_profile_id=f"bp_{suffix}",
                        organization_id="o_test",
                        name=f"Profile {suffix}",
                        is_managed=own_memory,
                        workflow_permanent_id="wpid_test" if own_memory else None,
                        browser_profile_key_digest=(
                            build_browser_profile_key_digest(f"cred_{suffix}") if own_memory else None
                        ),
                    )
                    for suffix in ("a", "b")
                ],
            ]
        )
        await session.commit()
    workflow = await database.workflows.get_workflow("wf_1", organization_id="o_test")
    assert workflow is not None
    if deleted_version:
        async with database.Session() as session:
            row = await session.get(WorkflowModel, "wf_1")
            assert row is not None
            row.deleted_at = now
            await session.commit()
        assert await database.workflows.get_workflow("wf_1", organization_id="o_test") is None

    svc = WorkflowService()
    _patch_execute_workflow_deps(monkeypatch, svc, workflow, _execute_workflow_run(WorkflowRunStatus.completed))
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(svc, "get_workflow_run", WorkflowService.get_workflow_run.__get__(svc))
    monkeypatch.setattr(svc, "get_workflow", WorkflowService.get_workflow.__get__(svc))
    monkeypatch.setattr(
        svc, "get_workflow_by_workflow_run_id", WorkflowService.get_workflow_by_workflow_run_id.__get__(svc)
    )
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_browser_memory_engine_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "_managed_browser_profile_has_content", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "_reconcile_managed_browser_profile_proxy_pin", AsyncMock())
    monkeypatch.setattr(svc, "_maybe_pin_credential_profile_ip", AsyncMock(side_effect=lambda **kw: kw["workflow_run"]))
    monkeypatch.setattr(svc, "clean_up_workflow", AsyncMock())
    monkeypatch.setattr(svc, "_clean_up_workflow_browser", AsyncMock(return_value=_browser_cleanup_result()))
    monkeypatch.setattr(svc, "_finalize_workflow_run_status", AsyncMock(side_effect=lambda **kw: kw["workflow_run"]))
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", AsyncMock())
    monkeypatch.setattr(
        service_module, "get_recorded_decision", AsyncMock(return_value=RetryDecision(False, 2, 0, True, "no_match"))
    )

    async def mark_running(workflow_run_id: str) -> WorkflowRun:
        return await database.workflow_runs.update_workflow_run(
            workflow_run_id, status=WorkflowRunStatus.running, started_at=datetime.now(UTC).replace(tzinfo=None)
        )

    monkeypatch.setattr(svc, "mark_workflow_run_as_running", mark_running)
    browser_seeds: list[str | None] = []

    async def execute_blocks(**kwargs: Any) -> tuple[WorkflowRun, set[str]]:
        browser_seeds.append(kwargs["browser_profile_id"])
        return await database.workflow_runs.update_workflow_run("wr_test", status=WorkflowRunStatus.completed), set()

    monkeypatch.setattr(svc, "_execute_workflow_blocks", execute_blocks)
    preparation = await database.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id="wr_test",
        organization_id="o_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=None,
    )
    assert preparation.status == "inserted"

    result = await svc.execute_workflow_with_retries(
        workflow_run_id="wr_test",
        api_key=None,
        organization=SimpleNamespace(organization_id="o_test"),
        attempt_number=2,
    )

    assert result.status == WorkflowRunStatus.completed
    assert browser_seeds == ["bp_b"]
    assert result.browser_profile_id == "bp_b"
    assert result.browser_seed_source == seed_source
    assert result.browser_sink_profile_id == ("bp_b" if own_memory else None)
    assert await database.workflow_run_credential_selections.get_selection("wr_test", "login") == "cred_b"


@pytest.mark.asyncio
async def test_run_admission_failure_finishes_without_browser_or_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = WorkflowService()
    failed_run = _execute_workflow_run(WorkflowRunStatus.failed)
    _patch_execute_workflow_deps(monkeypatch, svc, _execute_workflow(), failed_run)
    monkeypatch.setattr(
        app.AGENT_FUNCTION, "before_workflow_run_start", AsyncMock(side_effect=RuntimeError("admission denied"))
    )
    monkeypatch.setattr(svc, "_mark_workflow_run_as_failed_for_dispatch", AsyncMock(return_value=failed_run))
    cleanup = AsyncMock()
    monkeypatch.setattr(svc, "clean_up_workflow", cleanup)

    result = await _run_execute_workflow(svc)

    assert result.status == WorkflowRunStatus.failed
    svc.mark_workflow_run_as_running.assert_not_awaited()
    svc.auto_create_browser_session_if_needed.assert_not_awaited()
    svc._execute_workflow_blocks.assert_not_awaited()
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_superseded_admission_does_not_finalize_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = WorkflowService()
    _patch_execute_workflow_deps(
        monkeypatch, svc, _execute_workflow(), _execute_workflow_run(WorkflowRunStatus.running)
    )
    monkeypatch.setattr(
        app.AGENT_FUNCTION,
        "before_workflow_run_start",
        AsyncMock(side_effect=service_module.WorkflowAttemptDispatchSuperseded("wr_test", 1)),
    )
    finalize = AsyncMock()
    monkeypatch.setattr(svc, "mark_workflow_run_as_failed_if_not_final", finalize)
    with pytest.raises(service_module.WorkflowAttemptDispatchSuperseded):
        await _run_execute_workflow(svc)
    finalize.assert_not_awaited()
    svc.mark_workflow_run_as_running.assert_not_awaited()
    svc._execute_workflow_blocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_forced_browser_deferral_preserves_preparation_and_first_attempt_pin(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
) -> None:
    database, svc, workflow = forced_session_setup
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=True))
    request = WorkflowRequestBody(data={})
    run = await svc.create_workflow_run(
        workflow_request=request,
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id="o_test",
        workflow=workflow,
    )
    assert run.browser_session_id is None
    app.PERSISTENT_SESSIONS_MANAGER.create_session.assert_not_awaited()
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert attempts[0].pinned_browser_session_id is None

    run = await svc._prepare_forced_browser_session(
        workflow=workflow, workflow_run=run, workflow_request=request, has_attempt_row=True
    )
    assert run.browser_session_id == "pbs_forced"
    attempts = await database.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert attempts[0].pinned_browser_session_id == "pbs_forced"
    assert app.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["workflow_run_id"] == run.workflow_run_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner", ["replaced", "next_attempt", "current", "no_policy", "canceled", "finalized", "write_error"]
)
async def test_admission_error_respects_persisted_dispatch_owner(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    owner: str,
) -> None:
    database, svc, workflow = forced_session_setup
    _patch_execute_workflow_deps(
        monkeypatch,
        svc,
        _execute_workflow(retry_policy=None if owner == "no_policy" else workflow.workflow_definition.retry_policy),
        _execute_workflow_run(WorkflowRunStatus.failed),
    )
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", svc)
    monkeypatch.setattr(svc, "get_workflow_run", WorkflowService.get_workflow_run.__get__(svc))
    monkeypatch.setattr(svc, "clean_up_workflow", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "record_run_duration", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_schedule_workflow_run_terminal_hooks", Mock())
    save_logs = AsyncMock()
    monkeypatch.setattr(workflow_runs_repository, "save_workflow_run_logs", save_logs)
    async with database.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_test",
                workflow_id="wf_1",
                workflow_permanent_id="wpid_test",
                organization_id="o_test",
                status="queued",
            )
        )
        session.add(
            TaskRunModel(
                run_id="wr_test",
                organization_id="o_test",
                task_run_type="workflow",
                status="queued",
            )
        )
        await session.commit()
    claim = None
    if owner != "no_policy":
        await database.workflow_run_attempts.create_attempt("wr_test", "o_test", 1, "queued")
        claim = await database.workflow_run_attempts.claim_prepared_attempt_execution("wr_test", "o_test", 1)
        assert claim is not None
    replacement_claim = None

    async def fail_admission(*args: Any, **kwargs: Any) -> None:
        nonlocal replacement_claim
        if owner == "replaced":
            assert await database.workflow_run_attempts.release_stale_dispatch_claim(
                "wr_test", "o_test", 1, stale_before=datetime.now(UTC) + timedelta(minutes=1)
            )
            replacement_claim = await database.workflow_run_attempts.claim_prepared_attempt_execution(
                "wr_test", "o_test", 1
            )
            assert replacement_claim is not None and replacement_claim != claim
        elif owner == "next_attempt":
            await database.workflow_run_attempts.create_attempt("wr_test", "o_test", 2, "queued")
        elif owner == "canceled":
            await database.workflow_runs.update_workflow_run_if_not_final("wr_test", WorkflowRunStatus.canceled)
        elif owner == "finalized":
            await database.workflow_run_attempts.finalize_attempt(
                "wr_test",
                1,
                status="failed",
                failure_reason="previous owner finished",
                failure_category=None,
                error_codes=[],
                finished_at=datetime.now(UTC),
                retry_decision=RETRY_DECISION_FINAL,
                decision_reason="no_match",
                next_attempt_at=None,
            )
        elif owner == "write_error":
            monkeypatch.setattr(
                database.workflow_runs,
                "finalize_workflow_run_for_dispatch",
                AsyncMock(side_effect=RuntimeError("finalization unavailable")),
            )
        raise RuntimeError("admission database unavailable")

    monkeypatch.setattr(app.AGENT_FUNCTION, "before_workflow_run_start", fail_admission)
    save_logs.reset_mock()
    if owner in {"replaced", "next_attempt", "finalized"}:
        with pytest.raises(service_module.WorkflowAttemptDispatchSuperseded):
            await _run_execute_workflow(svc, dispatch_claim_started_at=claim)
    elif owner == "write_error":
        with pytest.raises(RuntimeError, match="finalization unavailable"):
            await _run_execute_workflow(svc, dispatch_claim_started_at=claim)
    else:
        await _run_execute_workflow(svc, dispatch_claim_started_at=claim)
    if svc._background_tasks:
        await asyncio.gather(*list(svc._background_tasks))
    persisted = await database.workflow_runs.get_workflow_run("wr_test", "o_test")
    assert persisted is not None
    attempts = await database.workflow_run_attempts.get_attempts("wr_test")
    won = owner in {"current", "no_policy"}
    assert persisted.status == (
        WorkflowRunStatus.failed
        if won
        else WorkflowRunStatus.canceled
        if owner == "canceled"
        else WorkflowRunStatus.queued
    )
    if won:
        assert persisted.finished_at is not None and persisted.failure_reason
        svc.clean_up_workflow.assert_awaited_once()
        save_logs.assert_awaited_once_with("wr_test")
        app.AGENT_FUNCTION.record_run_duration.assert_awaited_once()
        if attempts:
            assert attempts[0].retry_decision == RETRY_DECISION_RETRY
            assert attempts[0].finished_at is not None
        async with database.Session() as session:
            mirror = await session.scalar(select(TaskRunModel).where(TaskRunModel.run_id == "wr_test"))
            assert mirror is not None and mirror.status == "failed"
            assert mirror.finished_at == persisted.finished_at.replace(tzinfo=None)
    else:
        svc.clean_up_workflow.assert_not_awaited()
        app.AGENT_FUNCTION.record_run_duration.assert_not_awaited()
        if owner != "canceled":
            save_logs.assert_not_awaited()
        if owner == "replaced":
            assert attempts[0].started_at == replacement_claim
            assert attempts[0].retry_decision is None
            assert attempts[0].finished_at is None
    svc.mark_workflow_run_as_running.assert_not_awaited()
    svc.auto_create_browser_session_if_needed.assert_not_awaited()
    svc._execute_workflow_blocks.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pin_ip", [False, True])
@pytest.mark.parametrize(
    "seed_source,start_fresh",
    [
        (BrowserSeedSource.override, False),
        (BrowserSeedSource.override, True),
        (BrowserSeedSource.own_memory, False),
        (BrowserSeedSource.credential, False),
        (BrowserSeedSource.picked, False),
        (BrowserSeedSource.fresh, True),
        (BrowserSeedSource.fresh, False),
    ],
)
async def test_deferred_execution_uses_persisted_seed_for_forced_browser(
    monkeypatch: pytest.MonkeyPatch,
    forced_session_setup: tuple[AgentDB, WorkflowService, Workflow],
    seed_source: BrowserSeedSource,
    start_fresh: bool,
    pin_ip: bool,
) -> None:
    database, svc, workflow = forced_session_setup
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=True))
    profile_id = None if seed_source == BrowserSeedSource.fresh else "bp_seed"
    workflow.persist_browser_session = True
    workflow.pin_saved_session_ip = pin_ip
    proxy = service_module.ProxyLocation.RESIDENTIAL_ISP
    async with database.Session() as session:
        session.add(
            BrowserProfileModel(
                browser_profile_id="bp_seed",
                organization_id="o_test",
                name="Saved seed",
                is_managed=True,
            )
        )
        await session.commit()
    run = await svc.create_workflow_run(
        workflow_request=WorkflowRequestBody(
            start_fresh_browser=start_fresh,
            proxy_location=proxy,
            browser_type="msedge",
        ),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        workflow_run_id="wr_test",
        organization_id="o_test",
        workflow=workflow,
    )
    assert run.browser_session_id is None
    app.PERSISTENT_SESSIONS_MANAGER.create_session.assert_not_awaited()
    # Setup has resolved the seed before the executing worker reconstructs its request.
    run = await database.workflow_runs.update_workflow_run(
        "wr_test",
        browser_profile_id=profile_id,
        browser_seed_source=seed_source,
        browser_sink_profile_id="bp_seed" if seed_source == BrowserSeedSource.own_memory else None,
    )
    execution_workflow = _execute_workflow()
    execution_workflow.pin_saved_session_ip = pin_ip
    execution_workflow.proxy_location = proxy
    execution_workflow.browser_profile_key = None
    _patch_execute_workflow_deps(monkeypatch, svc, execution_workflow, run)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(svc, "get_workflow_run", WorkflowService.get_workflow_run.__get__(svc))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", WorkflowService.mark_workflow_run_as_running.__get__(svc))
    monkeypatch.setattr(
        svc,
        "_resolve_managed_browser_profile_for_run_request",
        WorkflowService._resolve_managed_browser_profile_for_run_request.__get__(svc),
    )
    monkeypatch.setattr(svc, "_seed_managed_browser_profile_from_legacy_session", AsyncMock())
    monkeypatch.setattr(svc, "_reconcile_managed_browser_profile_proxy_pin", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=True))
    monkeypatch.setattr(
        app.WORKFLOW_CONTEXT_MANAGER,
        "initialize_workflow_run_context",
        AsyncMock(side_effect=BaseException("stop after preparation")),
    )
    with pytest.raises(BaseException, match="stop after preparation"):
        await _run_execute_workflow(svc)
    if svc._background_tasks:
        await asyncio.gather(*list(svc._background_tasks))
    persisted = await database.workflow_runs.get_workflow_run("wr_test", "o_test")
    assert persisted is not None and persisted.browser_profile_id == profile_id
    assert persisted.browser_seed_source == seed_source
    assert persisted.reuse_bound_key == service_module.REUSE_ADMISSION_OFF_DISABLED
    attempts = await database.workflow_run_attempts.get_attempts("wr_test")
    create_session = app.PERSISTENT_SESSIONS_MANAGER.create_session
    if pin_ip and profile_id is None:
        create_session.assert_not_awaited()
        assert persisted.browser_session_id is None and attempts[0].pinned_browser_session_id is None
    else:
        create_session.assert_awaited_once()
        creation = create_session.await_args.kwargs
        assert creation["browser_profile_id"] == profile_id
        assert creation["proxy_location"] == proxy
        assert creation["browser_type"].value == "msedge"
        assert creation["inherit_profile_proxy"] is True
        assert creation["workflow_run_id"] == "wr_test"
        assert persisted.browser_session_id == attempts[0].pinned_browser_session_id == "pbs_forced"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "on",
        "off",
        "fallback",
        "refused",
        "assignment",
        "replaced",
        "stale_fallback",
        "address",
        "reuse",
        "refresh_none",
        "refresh_supplied",
    ],
)
async def test_deferred_force_launch_decision_survives_publication_and_reentry(monkeypatch, forced_session_setup, case):
    database, svc, workflow = forced_session_setup
    selected = case != "off"
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=True))
    flag = AsyncMock(side_effect=lambda name, *a, **k: selected and name == "FORCE_BROWSER_SESSION")
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", flag)
    run = await svc.create_workflow_run(
        workflow_request=WorkflowRequestBody(
            start_fresh_browser=case not in {"reuse", "address"},
            reuse_browser_session=case != "address",
            browser_address="http://browser.invalid" if case == "address" else None,
        ),
        workflow_permanent_id="wpid_test",
        workflow_id="wf_1",
        workflow_run_id="wr_test",
        organization_id="o_test",
        workflow=workflow,
    )
    run, reuse = await svc.resolve_and_persist_reuse_bound_key(
        workflow=workflow, workflow_run=run, effective_reuse=case == "reuse"
    )
    assert reuse is (case == "reuse")
    assert "reuse_bound_key" not in run.model_dump()
    run = await database.workflow_runs.update_workflow_run("wr_test", browser_seed_source=BrowserSeedSource.fresh)
    assert await database.workflow_runs.queue_initial_dispatch("wr_test", 1)
    claim = await database.workflow_run_attempts.claim_prepared_attempt_execution("wr_test", "o_test", 1)
    assert claim is not None
    execution = _execute_workflow()
    execution.pin_saved_session_ip = False
    execution.proxy_location = None
    _patch_execute_workflow_deps(monkeypatch, svc, execution, run)
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(svc, "get_workflow_run", WorkflowService.get_workflow_run.__get__(svc))
    monkeypatch.setattr(svc, "mark_workflow_run_as_running", WorkflowService.mark_workflow_run_as_running.__get__(svc))
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=True))
    flag.side_effect = lambda name, *a, **k: not selected and name == "FORCE_BROWSER_SESSION"
    flag.reset_mock()
    monkeypatch.setattr(svc, "clean_up_workflow", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_schedule_workflow_run_terminal_hooks", Mock())
    monkeypatch.setattr(workflow_runs_repository, "save_workflow_run_logs", AsyncMock())
    monkeypatch.setattr(app.AGENT_FUNCTION, "record_run_duration", AsyncMock())
    # Stop at actual application execution, after the ordinary browser entry point.
    svc._execute_workflow_blocks.side_effect = asyncio.CancelledError()
    _patch_browser_cleanup(monkeypatch, svc, [])
    _patch_finalize(monkeypatch, svc, [], run)

    if case.startswith("refresh_"):
        original = "pbs_original" if case == "refresh_supplied" else None

        async def observe_predecessor(workflow_run, **kwargs):
            workflow_run.browser_session_id = original
            await database.workflow_runs.update_workflow_run("wr_test", browser_session_id=original)

        monkeypatch.setattr(app.AGENT_FUNCTION, "before_workflow_run_start", observe_predecessor)
        mark_running = svc.mark_workflow_run_as_running

        async def refresh_after_competing_publication(**kwargs):
            await database.workflow_runs.update_workflow_run("wr_test", browser_session_id="pbs_replacement")
            return await mark_running(**kwargs)

        monkeypatch.setattr(svc, "mark_workflow_run_as_running", refresh_after_competing_publication)

    async def create(**kwargs):
        if case.startswith("refresh_") and kwargs["expected_browser_session_id"] != "pbs_replacement":
            raise service_module.WorkflowAttemptDispatchSuperseded("wr_test", 1)
        if case in {"replaced", "stale_fallback"}:
            async with database.Session() as session:
                await session.execute(
                    update(WorkflowRunAttemptModel).values(started_at=datetime.now(UTC) + timedelta(seconds=1))
                )
                await session.commit()
            if case == "stale_fallback":
                raise RuntimeError("recoverable provisioning failure")
            raise BrowserSessionCreditAdmissionRefusal()
        if case == "refused":
            raise BrowserSessionCreditAdmissionRefusal()
        if case == "fallback":
            raise RuntimeError("recoverable provisioning failure")
        # The manager commits the association before returning the created session.
        async with database.Session() as session:
            session.add(
                PersistentBrowserSessionModel(
                    persistent_browser_session_id="pbs_forced",
                    organization_id="o_test",
                    status="running",
                    runnable_type="forced_workflow_run",
                )
            )
            await session.commit()
        update_run = database.workflow_runs.update_workflow_run
        await update_run(kwargs["workflow_run_id"], browser_session_id="pbs_forced")
        if case == "assignment":

            async def reject_duplicate_assignment(*args: Any, **kwargs: Any) -> WorkflowRun:
                if kwargs.get("browser_session_id") == "pbs_forced":
                    raise RuntimeError("duplicate assignment failed")
                return await update_run(*args, **kwargs)

            monkeypatch.setattr(
                database.workflow_runs, "update_workflow_run", AsyncMock(side_effect=reject_duplicate_assignment)
            )
        return SimpleNamespace(persistent_browser_session_id="pbs_forced")

    app.PERSISTENT_SESSIONS_MANAGER.create_session.side_effect = create
    # Address and admitted reuse are setup controls; their separate attachment tests cover execution.
    if case in {"address", "reuse"}:
        assert "force_pending" not in (run.reuse_bound_key or "")
        app.PERSISTENT_SESSIONS_MANAGER.create_session.assert_not_awaited()
        return
    if case in {"replaced", "stale_fallback", "refresh_none", "refresh_supplied"}:
        with pytest.raises(service_module.WorkflowAttemptDispatchSuperseded):
            await _run_execute_workflow(svc, dispatch_claim_started_at=claim)
    elif case == "refused":
        await _run_execute_workflow(svc, dispatch_claim_started_at=claim)
    else:
        monkeypatch.setattr(svc, "_ensure_browser_session_lease", AsyncMock())
        with pytest.raises(asyncio.CancelledError):
            await _run_execute_workflow(svc, dispatch_claim_started_at=claim)
    if svc._background_tasks:
        await asyncio.gather(*list(svc._background_tasks))
    persisted = await database.workflow_runs.get_workflow_run("wr_test", "o_test")
    assert persisted is not None
    assert not any(c.args[0] == "FORCE_BROWSER_SESSION" for c in flag.await_args_list)
    if case in {"refused", "replaced", "stale_fallback", "refresh_none", "refresh_supplied"}:
        svc.auto_create_browser_session_if_needed.assert_not_awaited()
        svc._execute_workflow_blocks.assert_not_awaited()
        assert persisted.status == (WorkflowRunStatus.failed if case == "refused" else WorkflowRunStatus.running)
        if case.startswith("refresh_"):
            assert persisted.browser_session_id == "pbs_replacement"
            assert (
                app.PERSISTENT_SESSIONS_MANAGER.create_session.await_args.kwargs["expected_browser_session_id"]
                == original
            )
    else:
        assert persisted.reuse_bound_key == service_module.REUSE_ADMISSION_OFF_DISABLED
        assert svc.auto_create_browser_session_if_needed.await_count == 1
        session_id = persisted.browser_session_id
        assert session_id == ("pbs_forced" if case in {"on", "assignment"} else None)
        assert svc.auto_create_browser_session_if_needed.await_args.kwargs["browser_session_id"] == session_id
        assert app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context("wr_test").browser_session_id == session_id
        attempts = await database.workflow_run_attempts.get_attempts("wr_test")
        assert attempts[0].pinned_browser_session_id == session_id
        svc._execute_workflow_blocks.assert_awaited_once()
        if case == "assignment":
            assert not any(
                "browser_session_id" in call.kwargs
                for call in database.workflow_runs.update_workflow_run.await_args_list
            )
        calls = app.PERSISTENT_SESSIONS_MANAGER.create_session.await_count
        for attempt in (1, 2):
            if attempt == 2:
                await database.workflow_runs.update_workflow_run("wr_test", status=WorkflowRunStatus.failed)
                await database.workflow_run_attempts.finalize_attempt(
                    "wr_test",
                    1,
                    status="failed",
                    failure_reason="retry preparation",
                    failure_category=None,
                    error_codes=[],
                    finished_at=datetime.now(UTC),
                    retry_decision=RETRY_DECISION_RETRY,
                    decision_reason="matched_rule",
                    next_attempt_at=None,
                )
                preparation = await service_module.prepare_next_attempt_result("wr_test", "o_test", 1)
                assert preparation.status == "inserted"
                assert preparation.pinned_browser_session_id == persisted.browser_session_id
                claim = await database.workflow_run_attempts.claim_prepared_attempt_execution("wr_test", "o_test", 2)
                assert claim is not None
            with pytest.raises(asyncio.CancelledError):
                await _run_execute_workflow(svc, attempt_number=attempt, dispatch_claim_started_at=claim)
        assert app.PERSISTENT_SESSIONS_MANAGER.create_session.await_count == calls == int(selected)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pin_cancelled", "cancelled", "timeout", "failed", "denied", "success"])
async def test_forced_retirement_claim_propagates_cancellation(monkeypatch, forced_session_setup, case):
    database, svc, workflow = forced_session_setup
    monkeypatch.setattr(app.AGENT_FUNCTION, "should_defer_workflow_browser_creation", AsyncMock(return_value=True))
    request = WorkflowRequestBody(data={})
    run = await svc.create_workflow_run(
        workflow_request=request,
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id="o_test",
        workflow=workflow,
    )
    pin_error = asyncio.CancelledError() if case == "pin_cancelled" else RuntimeError("pin failed")
    monkeypatch.setattr(database.workflow_runs, "pin_browser_session_for_dispatch", AsyncMock(side_effect=pin_error))
    entered, release = asyncio.Event(), asyncio.Event()
    order, budgets = [], []
    timeout_context = None
    original_claim = database.workflow_runs.claim_browser_session_retirement

    def bounded_timeout(seconds):
        nonlocal timeout_context
        budgets.append(seconds)
        timeout_context = asyncio.timeout(None)
        return timeout_context

    async def claim(**kwargs):
        entered.set()
        order.append("entered")
        try:
            if case in {"cancelled", "timeout"}:
                if case == "timeout" and timeout_context is not None:
                    timeout_context.reschedule(asyncio.get_running_loop().time())
                await release.wait()
            if case == "failed":
                raise RuntimeError("claim failed")
            if case == "success":
                assert await original_claim(**kwargs)
                order.append("claimed")
                return True
            return False
        finally:
            order.append("drained")

    retirement = AsyncMock(side_effect=claim)
    monkeypatch.setattr(database.workflow_runs, "claim_browser_session_retirement", retirement)
    close = AsyncMock(side_effect=lambda *args, **kwargs: order.append("closed"))
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "close_session", close)
    monkeypatch.setattr(service_module, "asyncio", ScopedAsyncio(timeout=bounded_timeout))
    task = asyncio.create_task(
        svc._prepare_forced_browser_session(
            workflow=workflow,
            workflow_run=run,
            workflow_request=request,
            has_attempt_row=True,
            use_resolved_run_seed=True,
            on_finalization_denied=lambda: order.append("denied"),
        )
    )
    expected = (
        asyncio.CancelledError
        if case in {"pin_cancelled", "cancelled"}
        else TimeoutError
        if case == "timeout"
        else RuntimeError
    )
    try:
        if case != "pin_cancelled":
            await asyncio.wait_for(entered.wait(), timeout=5)
        if case == "cancelled":
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "retirement claim did not propagate cancellation or timeout"
        with pytest.raises(expected) as raised:
            await task
        if case == "success":
            assert order == ["entered", "claimed", "drained", "closed"]
            assert service_module.BROWSER_RETIREMENT_DENIED_NOTE not in getattr(raised.value, "__notes__", ())
        else:
            assert order == (["denied"] if case == "pin_cancelled" else ["entered", "drained", "denied"])
            assert service_module.BROWSER_RETIREMENT_DENIED_NOTE in raised.value.__notes__
            close.assert_not_awaited()
        assert budgets == ([] if case == "pin_cancelled" else [5])
        assert retirement.await_count == int(case != "pin_cancelled")
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["claim", "pin_cancelled", "orphan", "orphan_cancelled"])
async def test_superseded_forced_startup_cannot_pin_or_close_replacement(monkeypatch, forced_session_setup, case):
    database, svc, workflow = forced_session_setup
    pin_cancelled = case == "pin_cancelled"
    orphan = case.startswith("orphan")
    monkeypatch.setattr(app.PERSISTENT_SESSIONS_MANAGER, "close_session", AsyncMock())
    async with database.Session() as session:
        session.add(
            PersistentBrowserSessionModel(
                persistent_browser_session_id="pbs_existing", organization_id="o_test", status="running"
            )
        )
        await session.commit()
    run = await svc.create_workflow_run(
        workflow_request=WorkflowRequestBody(data={}, browser_session_id="pbs_existing"),
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id="o_test",
        workflow=workflow,
    )
    claim = datetime(2026, 1, 1)
    async with database.Session() as session:
        row = await session.get(service_module.WorkflowRunModel, run.workflow_run_id)
        row.status, row.browser_session_id = "running", None
        attempt = await session.scalar(select(WorkflowRunAttemptModel))
        attempt.status, attempt.started_at, attempt.pinned_browser_session_id = "running", claim, None
        await session.commit()
    run = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "o_test")

    async def finish_obsolete_start(**kwargs):
        assert kwargs["attempt_number"] == 1 and kwargs["dispatch_claim_started_at"] == claim
        async with database.Session() as session:
            row = await session.get(service_module.WorkflowRunModel, run.workflow_run_id)
            row.browser_session_id = "pbs_obsolete" if pin_cancelled else "pbs_replacement"
            attempt = await session.scalar(select(WorkflowRunAttemptModel))
            if pin_cancelled or orphan:
                session.add(
                    PersistentBrowserSessionModel(
                        persistent_browser_session_id="pbs_obsolete",
                        organization_id="o_test",
                        status="running",
                        runnable_id=run.workflow_run_id if pin_cancelled else None,
                        runnable_type="workflow_run" if pin_cancelled else "forced_workflow_run",
                        runnable_generation_id="new" if pin_cancelled else None,
                    )
                )
            else:
                attempt.started_at = claim + timedelta(seconds=1)
            if orphan:
                session.add(
                    PersistentBrowserSessionModel(
                        persistent_browser_session_id="pbs_replacement", organization_id="o_test", status="running"
                    )
                )
                attempt.pinned_browser_session_id = "pbs_replacement"
            await session.commit()
        return SimpleNamespace(persistent_browser_session_id="pbs_obsolete")

    app.PERSISTENT_SESSIONS_MANAGER.create_session.side_effect = finish_obsolete_start
    denied = []
    if pin_cancelled:
        monkeypatch.setattr(
            database.workflow_runs, "pin_browser_session_for_dispatch", AsyncMock(side_effect=asyncio.CancelledError())
        )
    if case == "orphan_cancelled":
        app.PERSISTENT_SESSIONS_MANAGER.close_session.side_effect = asyncio.CancelledError()
    with pytest.raises(
        asyncio.CancelledError if "cancelled" in case else service_module.WorkflowAttemptDispatchSuperseded
    ) as raised:
        await svc._prepare_forced_browser_session(
            workflow=workflow,
            workflow_run=run,
            workflow_request=WorkflowRequestBody(data={}),
            has_attempt_row=True,
            use_resolved_run_seed=True,
            attempt_number=1,
            dispatch_claim_started_at=claim,
            on_finalization_denied=lambda: denied.append(True),
        )
    assert denied and service_module.BROWSER_RETIREMENT_DENIED_NOTE in getattr(raised.value, "__notes__", ())
    persisted = await database.workflow_runs.get_workflow_run(run.workflow_run_id, "o_test")
    assert persisted.status == WorkflowRunStatus.running
    assert persisted.browser_session_id == ("pbs_obsolete" if pin_cancelled else "pbs_replacement")
    attempt = (await database.workflow_run_attempts.get_attempts(run.workflow_run_id))[0]
    assert attempt.pinned_browser_session_id == ("pbs_replacement" if orphan else None)
    if orphan:
        assert attempt.started_at == claim
        retired = await database.browser_sessions.get_persistent_browser_session("pbs_obsolete", "o_test")
        replacement = await database.browser_sessions.get_persistent_browser_session("pbs_replacement", "o_test")
        assert retired.close_requested_at is not None
        assert replacement.status == "running" and replacement.close_requested_at is None
        app.PERSISTENT_SESSIONS_MANAGER.close_session.assert_awaited_once_with(
            "o_test", "pbs_obsolete", reason=BrowserSessionCloseReason.aborted
        )
    else:
        app.PERSISTENT_SESSIONS_MANAGER.close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_guarded_binding_clear_uses_and_commits_held_dispatch_session(monkeypatch, forced_session_setup):
    database, svc, workflow = forced_session_setup
    claim = datetime(2026, 9, 23)
    async with database.Session() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_current",
                    workflow_id="wf_1",
                    workflow_permanent_id="wpid_test",
                    organization_id="o_test",
                    status="running",
                    started_at=claim,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_current",
                    organization_id="o_test",
                    attempt_number=1,
                    status="running",
                    started_at=claim,
                ),
                PersistentBrowserSessionModel(
                    persistent_browser_session_id="pbs_previous",
                    organization_id="o_test",
                    status="running",
                    runnable_type="workflow_run",
                    runnable_id="wr_previous",
                    runnable_generation_id="gen_previous",
                    bound_workflow_permanent_id="wpid_test",
                    bound_key="key_previous",
                    download_run_id="wr_previous",
                ),
            ]
        )
        await session.commit()
    browser = await database.browser_sessions.get_persistent_browser_session("pbs_previous", "o_test")
    assert browser is not None
    dispatch_session = svc._workflow_run_dispatch_session
    clear_binding = database.browser_sessions.clear_persistent_browser_session_binding
    held = None

    @asynccontextmanager
    async def track_dispatch_session(**kwargs):
        nonlocal held
        async with dispatch_session(**kwargs) as session:
            held = session
            yield session

    def no_nested_session():
        raise AssertionError("binding clear opened a second database session")

    async def clear_in_held_session(**kwargs):
        assert held is not None and held.in_transaction()
        assert kwargs.get("db_session") is held
        with monkeypatch.context() as scope:
            scope.setattr(database.browser_sessions, "Session", no_nested_session)
            return await clear_binding(**kwargs)

    monkeypatch.setattr(svc, "_workflow_run_dispatch_session", track_dispatch_session)
    monkeypatch.setattr(database.browser_sessions, "clear_persistent_browser_session_binding", clear_in_held_session)
    result = await svc._retire_reused_session_for_respawn(
        organization_id="o_test",
        workflow_run_id="wr_current",
        workflow_permanent_id=workflow.workflow_permanent_id,
        bound_key="key_previous",
        browser_session=browser,
        attempt_number=1,
        dispatch_claim_started_at=claim,
    )
    assert result is None
    persisted = await database.browser_sessions.get_persistent_browser_session("pbs_previous", "o_test")
    assert persisted is not None
    assert (persisted.bound_workflow_permanent_id, persisted.bound_key, persisted.download_run_id) == (None, None, None)
    assert (persisted.runnable_id, persisted.runnable_type, persisted.runnable_generation_id) == (
        "wr_previous",
        "workflow_run",
        "gen_previous",
    )
