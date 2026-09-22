"""Tests for WorkflowRunsRepository.create_workflow_run_parameters batch method."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.forge import app
from skyvern.forge.agent import _v3_failure_category
from skyvern.forge.failure_classifier import classify_from_failure_reason
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.db.agent_db import AgentDB, _build_engine
from skyvern.forge.sdk.db.enums import BrowserSeedSource
from skyvern.forge.sdk.db.models import (
    ArtifactModel,
    Base,
    CredentialModel,
    OrganizationModel,
    PersistentBrowserSessionModel,
    TaskModel,
    TaskRunModel,
    WorkflowModel,
    WorkflowRunAttemptModel,
    WorkflowRunBlockModel,
    WorkflowRunCredentialSelectionModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.repositories import workflow_run_attempts as attempts_repository_module
from skyvern.forge.sdk.db.repositories.workflow_run_attempts import TerminalSideEffectCheckpoint
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.schemas.persistent_browser_sessions import FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow import service as workflow_service_module
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import LEASE_TAKEOVER_SECONDS, is_retry_eligible_run
from skyvern.forge.taskv3.loop import LoopOutcome
from skyvern.schemas.run_enums import RunType
from skyvern.schemas.runs import MAX_SEARCH_FETCH_LIMIT
from skyvern.schemas.workflows import WorkflowRetryPolicy


def _make_workflow_parameter(
    key: str,
    *,
    workflow_parameter_type: WorkflowParameterType = WorkflowParameterType.STRING,
    default_value: str | int | float | bool | dict | list | None = None,
) -> WorkflowParameter:
    now = datetime.now(tz=timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_id="wf_test",
        key=key,
        workflow_parameter_type=workflow_parameter_type,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


class _SessionContext:
    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value


class _EmptyExecuteResult:
    def mappings(self) -> _EmptyExecuteResult:
        return self

    def all(self) -> list[Any]:
        return []


def _where_clause_sql(query: Any) -> str:
    return str(query.whereclause.compile(compile_kwargs={"literal_binds": True}))


def _query_sql(query: Any) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def _assert_not_filtering_copilot_authored_workflows(where_clause: str) -> None:
    assert "workflows.created_by" not in where_clause
    assert "workflows.edited_by" not in where_clause


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncEngine:
    engine = _build_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_db(sqlite_engine: AsyncEngine) -> AgentDB:
    return AgentDB("sqlite+aiosqlite:///:memory:", db_engine=sqlite_engine)


def _workflow_run_model(
    *,
    workflow_run_id: str,
    queued_at: datetime,
    browser_session_id: str | None = None,
    debug_session_id: str | None = None,
    sequential_key: str | None = None,
    workflow_permanent_id: str = "wpid_test",
    workflow_id: str = "wf_test",
    status: str = WorkflowRunStatus.queued.value,
    organization_id: str = "org_test",
    sequential_credential_id: str | None = None,
    failure_category: list[dict[str, Any]] | None = None,
) -> WorkflowRunModel:
    return WorkflowRunModel(
        workflow_run_id=workflow_run_id,
        workflow_id=workflow_id,
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
        browser_session_id=browser_session_id,
        debug_session_id=debug_session_id,
        status=status,
        sequential_key=sequential_key,
        sequential_credential_id=sequential_credential_id,
        created_at=queued_at,
        modified_at=queued_at,
        queued_at=queued_at,
        failure_category=failure_category,
    )


def _credential_run(
    workflow_run_id: str, queued_at: datetime, credential_id: str | None, **kwargs: Any
) -> WorkflowRunModel:
    kwargs.setdefault("browser_session_id", f"pbs_{workflow_run_id}")
    return _workflow_run_model(
        workflow_run_id=workflow_run_id,
        queued_at=queued_at,
        sequential_credential_id=credential_id,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clear_browser_address, replacement_browser_address, expected_browser_address",
    [
        (False, None, "caller-cdp.example.test"),
        (False, "healthy-cdp.example.test", "healthy-cdp.example.test"),
        (True, None, None),
    ],
)
async def test_prepare_next_attempt_clears_only_server_assigned_browser_address(
    sqlite_db: AgentDB,
    clear_browser_address: bool,
    replacement_browser_address: str | None,
    expected_browser_address: str | None,
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    workflow_run_id = "wr_prepare_address"
    workflow_run = _workflow_run_model(
        workflow_run_id=workflow_run_id,
        queued_at=now,
        status=WorkflowRunStatus.failed.value,
    )
    workflow_run.browser_address = "caller-cdp.example.test"
    attempt = WorkflowRunAttemptModel(
        workflow_run_id=workflow_run.workflow_run_id,
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now,
    )
    async with sqlite_db.Session() as session:
        session.add_all([workflow_run, attempt])
        await session.commit()

    preparation = await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=None,
        clear_browser_address=clear_browser_address,
        replacement_browser_address=replacement_browser_address,
    )

    assert preparation.status == "inserted"
    async with sqlite_db.Session() as session:
        reopened = await session.get(WorkflowRunModel, workflow_run_id)
        assert reopened is not None
        assert reopened.browser_address == expected_browser_address
        assert reopened.status == WorkflowRunStatus.queued.value


@pytest.mark.asyncio
async def test_prepare_next_attempt_reopens_the_task_run_mirror(sqlite_db: AgentDB) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    workflow_run_id = "wr_prepare_task_run"
    workflow_run = _workflow_run_model(
        workflow_run_id=workflow_run_id,
        queued_at=now,
        status=WorkflowRunStatus.failed.value,
    )
    attempt = WorkflowRunAttemptModel(
        workflow_run_id=workflow_run_id,
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now,
    )
    task_run = _task_run_model(
        run_id=workflow_run_id,
        created_at=now,
        workflow_permanent_id="wpid_test",
        status=WorkflowRunStatus.failed.value,
    )
    task_run.started_at = now.replace(tzinfo=None)
    task_run.finished_at = (now + timedelta(minutes=5)).replace(tzinfo=None)
    async with sqlite_db.Session() as session:
        session.add_all([workflow_run, attempt, task_run])
        await session.commit()

    preparation = await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=None,
    )

    assert preparation.status == "inserted"
    async with sqlite_db.Session() as session:
        mirrored = await session.get(TaskRunModel, f"tr_{workflow_run_id}")
        assert mirrored is not None
        assert (mirrored.status, mirrored.started_at, mirrored.finished_at) == (
            WorkflowRunStatus.queued.value,
            None,
            None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["round_robin", "random"])
@pytest.mark.parametrize(
    "seed_source, pool_size, sequential, retained_session, retained_address, replacement_address, resets_seed",
    [
        (BrowserSeedSource.credential, 2, False, None, None, None, True),
        (BrowserSeedSource.own_memory, 2, False, None, None, None, True),
        (BrowserSeedSource.credential, 1, False, None, None, None, False),
        (BrowserSeedSource.own_memory, 1, False, None, None, None, False),
        (BrowserSeedSource.credential, 2, True, None, None, None, False),
        (BrowserSeedSource.override, 2, False, None, None, None, False),
        (BrowserSeedSource.picked, 2, False, None, None, None, False),
        (BrowserSeedSource.fresh, 2, False, None, None, None, False),
        (BrowserSeedSource.credential, 2, False, "explicit", None, None, False),
        (BrowserSeedSource.own_memory, 2, False, "explicit", None, None, False),
        (BrowserSeedSource.credential, 2, False, "forced", None, None, False),
        (BrowserSeedSource.own_memory, 2, False, "forced", None, None, False),
        (BrowserSeedSource.credential, 2, False, None, "retained-cdp.example.test", None, False),
        (BrowserSeedSource.own_memory, 2, False, None, "retained-cdp.example.test", None, False),
        (BrowserSeedSource.credential, 2, False, None, "retained-cdp.example.test", "healthy-cdp.example.test", True),
        (BrowserSeedSource.own_memory, 2, False, None, "retained-cdp.example.test", "healthy-cdp.example.test", True),
    ],
)
async def test_prepare_next_attempt_invalidates_only_rotated_credential_seed(
    sqlite_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    seed_source: BrowserSeedSource,
    pool_size: int,
    sequential: bool,
    retained_session: str | None,
    retained_address: str | None,
    replacement_address: str | None,
    resets_seed: bool,
) -> None:
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    now = datetime.now(UTC)
    retained_session_id = "pbs_retained" if retained_session else None
    run = _workflow_run_model(
        workflow_run_id="wr_seed",
        queued_at=now,
        status="failed",
        browser_session_id=None if retained_address else retained_session_id or "pbs_unpinned",
    )
    run.browser_address = retained_address
    run.browser_profile_id = "bp_a"
    run.browser_seed_source = seed_source
    run.browser_sink_profile_id = "bp_sink_a"
    credential_ids = ["cred_a", "cred_b"][:pool_size]
    async with sqlite_db.Session() as session:
        session.add(OrganizationModel(organization_id="org_test", organization_name="Test Organization"))
        await session.flush()
        session.add_all(
            [
                run,
                WorkflowModel(
                    workflow_id="wf_test",
                    workflow_permanent_id="wpid_test",
                    organization_id="org_test",
                    title="Workflow",
                    workflow_definition={
                        "blocks": [],
                        "parameters": [
                            {
                                "parameter_type": "credential",
                                "key": "login",
                                "credential_parameter_id": "cp_login",
                                "workflow_id": "wf_test",
                                "created_at": now.isoformat(),
                                "modified_at": now.isoformat(),
                                "credential_id": "cred_a",
                                "credential_ids": credential_ids,
                                "selection_strategy": strategy,
                            }
                        ],
                    },
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_seed",
                    organization_id="org_test",
                    attempt_number=1,
                    status="failed",
                    retry_decision="retry",
                    pinned_browser_session_id=retained_session_id if retained_session == "explicit" else None,
                ),
                WorkflowRunCredentialSelectionModel(
                    workflow_run_id="wr_seed",
                    organization_id="org_test",
                    workflow_permanent_id="wpid_test",
                    parameter_key="login",
                    credential_id="cred_a",
                ),
                *[
                    CredentialModel(
                        credential_id=credential_id,
                        organization_id="org_test",
                        name=credential_id,
                        credential_type="password",
                        run_sequentially=sequential,
                    )
                    for credential_id in credential_ids
                ],
            ]
        )
        await session.commit()

    if retained_session == "forced":
        await sqlite_db.workflow_run_attempts.pin_first_attempt_browser_session_if_unset(
            workflow_run_id="wr_seed",
            organization_id="org_test",
            browser_session_id="pbs_retained",
        )
    attempts = await sqlite_db.workflow_run_attempts.get_attempts("wr_seed")
    preparation = await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id="wr_seed",
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=attempts[0].pinned_browser_session_id,
        clear_browser_address=False,
        replacement_browser_address=replacement_address,
    )

    assert preparation.status == "inserted"
    assert preparation.pinned_browser_session_id == retained_session_id
    reopened = await sqlite_db.workflow_runs.get_workflow_run("wr_seed", "org_test")
    assert reopened is not None
    assert reopened.browser_session_id == retained_session_id
    assert reopened.browser_address == (replacement_address or retained_address)
    # A retry that moved to another host has no cookies to keep paired, so it rotates like an unpinned retry.
    rotates = (
        pool_size == 2 and not sequential and not retained_session and (not retained_address or replacement_address)
    )
    selected = await sqlite_db.workflow_run_credential_selections.get_selection("wr_seed", "login")
    assert selected == ("cred_b" if rotates else "cred_a")
    actual_seed = (reopened.browser_profile_id, reopened.browser_seed_source, reopened.browser_sink_profile_id)
    assert actual_seed == ((None, None, None) if resets_seed else ("bp_a", seed_source, "bp_sink_a"))


@pytest.mark.asyncio
async def test_list_stale_pending_retries_leaves_fresh_and_prepared_attempts_untouched(
    sqlite_db: AgentDB,
) -> None:
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    stale = WorkflowRunAttemptModel(
        workflow_run_id="wr_stale_retry",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now - timedelta(seconds=601),
    )
    fresh = WorkflowRunAttemptModel(
        workflow_run_id="wr_fresh_retry",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now - timedelta(seconds=599),
    )
    prepared = WorkflowRunAttemptModel(
        workflow_run_id="wr_prepared_retry",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now - timedelta(seconds=601),
        next_attempt_prepared_at=now - timedelta(seconds=600),
    )
    async with sqlite_db.Session() as session:
        session.add_all([stale, fresh, prepared])
        await session.commit()

    attempts = await sqlite_db.workflow_run_attempts.list_stale_pending_retries(
        now - timedelta(seconds=600),
    )

    assert [attempt.workflow_run_id for attempt in attempts] == ["wr_stale_retry"]


@pytest.mark.asyncio
async def test_abandonment_skips_a_pending_retry_with_a_young_side_effects_lease(
    sqlite_db: AgentDB,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    young_claim = now - timedelta(seconds=LEASE_TAKEOVER_SECONDS - 1)
    old_claim = now - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1)
    young = WorkflowRunAttemptModel(
        workflow_run_id="wr_young_lease",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1),
        side_effects_released_at=young_claim,
    )
    old = WorkflowRunAttemptModel(
        workflow_run_id="wr_old_lease",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1),
        side_effects_released_at=old_claim,
    )
    async with sqlite_db.Session() as session:
        session.add_all([young, old])
        await session.commit()

    skipped = await sqlite_db.workflow_run_attempts.revoke_or_abandon_attempt(
        workflow_run_id="wr_young_lease",
        attempt_number=1,
        decision="abandoned",
        reason="stale_retry",
        lease_takeover_seconds=LEASE_TAKEOVER_SECONDS,
    )
    abandoned = await sqlite_db.workflow_run_attempts.revoke_or_abandon_attempt(
        workflow_run_id="wr_old_lease",
        attempt_number=1,
        decision="abandoned",
        reason="stale_retry",
        lease_takeover_seconds=LEASE_TAKEOVER_SECONDS,
    )

    assert skipped is None
    assert abandoned is not None
    assert abandoned.retry_decision == "abandoned"
    assert abandoned.side_effects_released_at is None
    async with sqlite_db.Session() as session:
        unchanged = await session.get(WorkflowRunAttemptModel, ("wr_young_lease", 1))
        assert unchanged is not None
        assert unchanged.retry_decision == "retry"
        assert unchanged.side_effects_released_at == young_claim


@pytest.mark.asyncio
async def test_side_effect_claim_after_abandonment_requires_the_expected_retry_decision(
    sqlite_db: AgentDB,
) -> None:
    attempt = WorkflowRunAttemptModel(
        workflow_run_id="wr_claim_after_abandonment",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
    )
    async with sqlite_db.Session() as session:
        session.add(attempt)
        await session.commit()

    abandoned = await sqlite_db.workflow_run_attempts.revoke_or_abandon_attempt(
        workflow_run_id="wr_claim_after_abandonment",
        attempt_number=1,
        decision="abandoned",
        reason="stale_retry",
        lease_takeover_seconds=LEASE_TAKEOVER_SECONDS,
    )
    claim = await sqlite_db.workflow_run_attempts.claim_attempt_side_effects(
        workflow_run_id="wr_claim_after_abandonment",
        attempt_number=1,
        kind="interim",
        expected_retry_decision="retry",
    )

    assert abandoned is not None
    assert claim is None


@pytest.mark.asyncio
async def test_reopened_retry_is_ordered_after_delay_admitted_lane_occupant(sqlite_db: AgentDB) -> None:
    now = datetime.now(UTC)
    occupant_queued_at = now + timedelta(hours=1)
    retry_run = _workflow_run_model(
        workflow_run_id="wr_retry_lane",
        queued_at=now - timedelta(hours=2),
        browser_session_id="pbs_shared",
        status=WorkflowRunStatus.failed.value,
    )
    delay_occupant = _workflow_run_model(
        workflow_run_id="wr_delay_occupant",
        queued_at=occupant_queued_at,
        browser_session_id="pbs_shared",
    )
    attempt = WorkflowRunAttemptModel(
        workflow_run_id="wr_retry_lane",
        attempt_number=1,
        organization_id="org_test",
        status=WorkflowRunStatus.failed.value,
        retry_decision="retry",
        next_attempt_at=now,
    )
    async with sqlite_db.Session() as session:
        session.add_all([retry_run, delay_occupant, attempt])
        await session.commit()

    preparation = await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id="wr_retry_lane",
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id="pbs_shared",
        clear_browser_address=False,
    )

    assert preparation.status == "inserted"
    assert preparation.serialized_identity is True
    async with sqlite_db.Session() as session:
        reopened = await session.get(WorkflowRunModel, "wr_retry_lane")
        assert reopened is not None
        assert reopened.queued_at is not None
        assert reopened.queued_at > occupant_queued_at.replace(tzinfo=None)
    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_retry_lane")
    assert blocker is not None
    assert blocker.workflow_run_id == "wr_delay_occupant"


@pytest.mark.asyncio
async def test_credential_gate_crosses_workflows_and_respects_scope_and_status(sqlite_db: AgentDB) -> None:
    # The three excluded peers (other-org / completed / disjoint-credential) are stamped strictly
    # EARLIER than the legitimate blocker, so if the org-scope, non-terminal-status, or credential
    # exact-equality predicate regressed, that peer would sort first and become the returned blocker.
    # The `blocker == wr_a_overlap` assertion is therefore falsifiable for each predicate, not an
    # artifact of the strict (queued_at, workflow_run_id) total order.
    early_at = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    mid_at = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    probe_at = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        # wf_seq is run_sequentially, so its runs serialize whole-workflow even with a disjoint cred.
        session.add(
            WorkflowModel(
                workflow_id="wf_seq",
                workflow_permanent_id="wpid_ws",
                title="seq",
                workflow_definition={},
                run_sequentially=True,
            )
        )
        session.add_all(
            [
                # Excluded peers, each strictly earlier than wr_a_overlap so a broken predicate surfaces here.
                _credential_run(
                    "wr_other_org",
                    early_at,
                    "cred_a",
                    workflow_permanent_id="wpid_other",
                    organization_id="org_other",
                ),
                _credential_run(
                    "wr_completed",
                    early_at,
                    "cred_a",
                    workflow_permanent_id="wpid_other",
                    status=WorkflowRunStatus.completed.value,
                ),
                _credential_run("wr_disjoint", early_at, "cred_b", workflow_permanent_id="wpid_other"),
                # The one legitimate cross-workflow same-credential blocker.
                _credential_run("wr_a_overlap", mid_at, "cred_a", workflow_permanent_id="wpid_other"),
                _credential_run("wr_b_self", probe_at, "cred_a"),
                # Disjoint-credential peers that must still serialize via a shared sequential_key or a
                # run_sequentially workflow: the credential lane composes with the legacy lanes.
                _credential_run("wr_key_prior", mid_at, "cred_x", workflow_permanent_id="wpid_key", sequential_key="K"),
                _workflow_run_model(
                    workflow_run_id="wr_key_plain_session",
                    queued_at=early_at,
                    workflow_permanent_id="wpid_key",
                    sequential_key="K",
                    browser_session_id="pbs_key_plain",
                ),
                _credential_run(
                    "wr_key_self", probe_at, "cred_y", workflow_permanent_id="wpid_key", sequential_key="K"
                ),
                # A plain browser-session occupant belongs only to its session lane and must not
                # displace the real whole-workflow predecessor for a credential-composed probe.
                _workflow_run_model(
                    workflow_run_id="wr_ws_plain_session",
                    queued_at=early_at,
                    workflow_permanent_id="wpid_ws",
                    workflow_id="wf_seq",
                    browser_session_id="pbs_plain",
                ),
                _credential_run("wr_ws_prior", mid_at, "cred_p", workflow_permanent_id="wpid_ws", workflow_id="wf_seq"),
                _credential_run(
                    "wr_ws_self", probe_at, "cred_q", workflow_permanent_id="wpid_ws", workflow_id="wf_seq"
                ),
            ]
        )
        await session.commit()

    # Same credential + same org + different workflow blocks; disjoint / other-org / completed do not,
    # even though all three are strictly earlier than both the probe and the legitimate blocker.
    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_b_self")
    assert blocker is not None
    assert blocker.workflow_run_id == "wr_a_overlap"
    # A disjoint credential still blocks via a shared manual key ...
    key_blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_key_self")
    assert key_blocker is not None and key_blocker.workflow_run_id == "wr_key_prior"
    # ... and via a run_sequentially workflow.
    ws_blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_ws_self")
    assert ws_blocker is not None and ws_blocker.workflow_run_id == "wr_ws_prior"

    async with sqlite_db.Session() as session:
        overlap = await session.get(WorkflowRunModel, "wr_a_overlap")
        assert overlap is not None
        overlap.status = WorkflowRunStatus.canceled.value
        await session.commit()

    # With the only legitimate blocker canceled, the strictly-earlier excluded peers still do not block.
    assert await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_b_self") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer_credential, peer_kwargs, guard",
    [
        ("cred_other", {"workflow_permanent_id": "wpid_other"}, "credential exact-equality"),
        ("cred_self", {"organization_id": "org_other"}, "org scoping"),
        ("cred_self", {"status": WorkflowRunStatus.completed.value}, "non-terminal status"),
    ],
)
async def test_credential_gate_excludes_earlier_non_matching_peer(
    sqlite_db: AgentDB, peer_credential: str, peer_kwargs: dict[str, Any], guard: str
) -> None:
    # A single earlier peer that shares the credential lane in every dimension but the one under test.
    # Because it is strictly earlier than the probe, a regression in the org-scope, non-terminal-status,
    # or credential exact-equality predicate would return it as the blocker — so `is None` here is a
    # falsifiable proof that the guarding predicate fires, not a total-ordering artifact.
    earlier = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add(
            _credential_run("wr_peer_earlier", earlier, peer_credential, browser_session_id=None, **peer_kwargs)
        )
        session.add(_credential_run("wr_probe_later", later, "cred_self", browser_session_id=None))
        await session.commit()

    assert await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_probe_later") is None, guard


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "debug_session_id, expected_blocker",
    [
        # A debug run is excluded from browser-session serialization at enqueue
        # (is_browser_session_workflow is False when debug_session_id is set), so the credential branch
        # must not re-add the browser-session lane for it — it gates only on its credential.
        ("dbg_1", None),
        # A non-debug credential run sharing the session still composes the browser-session lane.
        (None, "wr_session_peer"),
    ],
)
async def test_credential_gate_browser_session_lane_honors_debug_exclusion(
    sqlite_db: AgentDB, debug_session_id: str | None, expected_blocker: str | None
) -> None:
    earlier = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add(
            _credential_run(
                "wr_session_peer",
                earlier,
                "cred_other",
                browser_session_id="pbs_shared",
                workflow_permanent_id="wpid_other",
            )
        )
        session.add(
            _credential_run(
                "wr_probe",
                later,
                "cred_self",
                browser_session_id="pbs_shared",
                debug_session_id=debug_session_id,
                workflow_permanent_id="wpid_debug",
            )
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_probe")
    assert (blocker.workflow_run_id if blocker else None) == expected_blocker


@pytest.mark.asyncio
async def test_get_workflow_run_preserves_sequential_credential_id(sqlite_db: AgentDB) -> None:
    queued_at = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_seq_snapshot",
                queued_at=queued_at,
                sequential_credential_id="cred_a",
            )
        )
        await session.commit()

    # The enqueue executor and scheduled-setup activity read this identity off the
    # converted schema to arm credential serialization; it must survive conversion.
    fetched = await sqlite_db.workflow_runs.get_workflow_run("wr_seq_snapshot", organization_id="org_test")
    assert fetched is not None
    assert fetched.sequential_credential_id == "cred_a"


@pytest.mark.asyncio
async def test_composed_credential_queued_at_advances_past_cross_lane_publication_despite_clock_skew(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior_ticket = datetime(2099, 1, 1, tzinfo=timezone.utc)
    skewed_host_clock = datetime(2000, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "skyvern.forge.sdk.db.repositories.workflow_runs.naive_utc_now",
        lambda: skewed_host_clock.replace(tzinfo=None),
    )
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_first_publisher",
                    status=WorkflowRunStatus.created.value,
                    queued_at=None,
                    sequential_key="shared_lane",
                ),
                _workflow_run_model(
                    workflow_run_id="wr_second_publisher",
                    status=WorkflowRunStatus.created.value,
                    queued_at=None,
                    sequential_credential_id="cred_skew",
                ),
            ]
        )
        await session.commit()

    publication_lock = asyncio.Lock()
    first_published = asyncio.Event()

    async def publish_first() -> None:
        async with publication_lock:
            await sqlite_db.workflow_runs.update_workflow_run(
                "wr_first_publisher",
                status=WorkflowRunStatus.queued,
                queued_at=prior_ticket,
            )
            first_published.set()

    async def publish_second() -> Any:
        await first_published.wait()
        async with publication_lock:
            return await sqlite_db.workflow_runs.update_workflow_run(
                "wr_second_publisher",
                status=WorkflowRunStatus.queued,
            )

    _, second = await asyncio.gather(publish_first(), publish_second())

    assert second.queued_at is not None
    assert second.queued_at > prior_ticket.replace(tzinfo=None)


def _task_run_model(
    *,
    run_id: str,
    created_at: datetime,
    workflow_permanent_id: str | None,
    task_run_type: str = RunType.workflow_run.value,
    status: str = WorkflowRunStatus.completed.value,
) -> TaskRunModel:
    return TaskRunModel(
        task_run_id=f"tr_{run_id}",
        organization_id="org_test",
        task_run_type=task_run_type,
        run_id=run_id,
        status=status,
        workflow_permanent_id=workflow_permanent_id,
        created_at=created_at,
        modified_at=created_at,
    )


def _persistent_browser_session_model(
    *,
    persistent_browser_session_id: str,
    runnable_type: str | None = FORCED_WORKFLOW_SESSION_RUNNABLE_TYPE,
) -> PersistentBrowserSessionModel:
    now = datetime.now(tz=timezone.utc)
    return PersistentBrowserSessionModel(
        persistent_browser_session_id=persistent_browser_session_id,
        organization_id="org_test",
        runnable_type=runnable_type,
        created_at=now,
        modified_at=now,
    )


@pytest.mark.asyncio
async def test_batch_create_uses_add_all_flush_commit_not_refresh() -> None:
    """Batch insert should use add_all + flush + commit and never call refresh."""
    tracked_models: list = []
    session = MagicMock()
    session.add_all = MagicMock(side_effect=lambda models: tracked_models.extend(models))

    async def _flush() -> None:
        now = datetime.now(tz=timezone.utc)
        for model in tracked_models:
            model.created_at = now

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    string_param = _make_workflow_parameter("url")
    int_param = _make_workflow_parameter("count", workflow_parameter_type=WorkflowParameterType.INTEGER)

    created = await repo.create_workflow_run_parameters(
        workflow_run_id="wr_test",
        workflow_parameter_values=[
            (string_param, "https://example.com"),
            (int_param, "7"),
        ],
    )

    session.add_all.assert_called_once()
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()
    session.refresh.assert_not_awaited()

    assert [p.workflow_parameter_id for p in created] == [
        string_param.workflow_parameter_id,
        int_param.workflow_parameter_id,
    ]
    assert [p.value for p in created] == ["https://example.com", 7]
    assert all(p.created_at is not None for p in created)


# ── Infra-failure attribution write path (SKY-16588) ──────────────────────────

_ATTR_QUEUED_AT = datetime(2026, 9, 19, tzinfo=timezone.utc)


async def _read_failure_attribution(sqlite_db: AgentDB, workflow_run_id: str) -> Any:
    async with sqlite_db.Session() as session:
        row = (await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))).one()
        return row.failure_attribution


async def _failure_attribution_is_sql_null(sqlite_db: AgentDB, workflow_run_id: str) -> bool:
    """SQL-grain check: distinguishes real SQL NULL from the JSON token 'null'.

    ORM ``is None`` cannot tell them apart, but CDC/Redshift and ``IS NULL`` predicates can.
    """
    async with sqlite_db.Session() as session:
        result = await session.execute(
            text("SELECT failure_attribution IS NULL FROM workflow_runs WHERE workflow_run_id = :wr"),
            {"wr": workflow_run_id},
        )
        return bool(result.scalar_one())


@pytest.mark.asyncio
async def test_failed_terminal_write_persists_infra_attribution(sqlite_db: AgentDB) -> None:
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_attr_failed",
                queued_at=_ATTR_QUEUED_AT,
                status=WorkflowRunStatus.created.value,
            )
        )
        await session.commit()

    failure_category = classify_from_failure_reason("No proxy available for this run", fallback_to_unknown=True)
    await sqlite_db.workflow_runs.update_workflow_run(
        "wr_attr_failed",
        status=WorkflowRunStatus.failed,
        failure_reason="No proxy available for this run",
        failure_category=failure_category,
    )

    doc = await _read_failure_attribution(sqlite_db, "wr_attr_failed")
    assert doc is not None
    assert doc["primary_infra_component"] == "proxy"
    assert doc["failure_category"] == "PROXY_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "exception_name", "reason_code", "evidence_source"),
    [
        ("Timeout exceeded on locator.wait_for", None, "locator_wait_for_timeout", "reason_code"),
        ("Waiting for locator('#submit')", "TimeoutError", "locator_wait_for_timeout", "reason_code"),
        (
            "Secure CodeBlock runner is unavailable",
            None,
            "secure_codeblock_runner_unavailable",
            "reason_code",
        ),
        (
            "CodeBlock inputs exhausted the sandbox memory limit before the block started",
            None,
            "secure_codeblock_input_memory_limit",
            "reason_code",
        ),
        (
            "Secure CodeBlock runner failed before completing",
            None,
            "secure_codeblock_runner_internal",
            "reason_code",
        ),
        ("Secure CodeBlock sandbox process exited", None, "secure_codeblock_sandbox_exited", "reason_code"),
        (
            "CodeBlock runner is already executing another CodeBlock",
            None,
            "secure_codeblock_runner_busy",
            "reason_code",
        ),
        ("Captcha required", None, None, "keyword_only"),
        (None, "NoProxyAvailable", None, "exception_type"),
        ("Timeout while loading the page", None, None, "keyword_match"),
    ],
)
async def test_classifier_provenance_round_trips_through_terminal_write(
    sqlite_db: AgentDB,
    reason: str | None,
    exception_name: str | None,
    reason_code: str | None,
    evidence_source: str,
) -> None:
    workflow_run_id = "wr_attr_provenance"
    async with sqlite_db.Session() as session:
        session.add(_workflow_run_model(workflow_run_id=workflow_run_id, queued_at=_ATTR_QUEUED_AT))
        await session.commit()

    failure_reason = f"{reason or ''}; synthetic-private-marker"
    category = classify_from_failure_reason(failure_reason, exception_name=exception_name)
    await sqlite_db.workflow_runs.update_workflow_run(
        workflow_run_id,
        status=WorkflowRunStatus.failed,
        failure_reason=failure_reason,
        failure_category=category,
    )

    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc["evidence_source"] == evidence_source
    assert doc.get("reason_code") == reason_code
    assert doc["classifier_version"] == 2
    encoded = json.dumps(doc, allow_nan=False)
    assert json.loads(encoded) == doc
    assert "synthetic-private-marker" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_status", ["budget_exhausted", "failed"])
async def test_budget_exhaustion_producer_persists_non_infra_attribution(
    sqlite_db: AgentDB, outcome_status: str
) -> None:
    workflow_run_id = "wr_attr_budget"
    async with sqlite_db.Session() as session:
        session.add(_workflow_run_model(workflow_run_id=workflow_run_id, queued_at=_ATTR_QUEUED_AT))
        await session.commit()

    outcome = LoopOutcome(status=outcome_status, reason="Unfinished", cap_trip="max_turns (40) reached")
    category = _v3_failure_category(
        outcome,
        task_status=TaskStatus.failed,
        failure_reason=outcome.reason,
        completion_vetoed=False,
        missing_extraction=False,
    )
    await sqlite_db.workflow_runs.update_workflow_run(
        workflow_run_id, status=WorkflowRunStatus.failed, failure_category=category
    )

    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc["failure_category"] == "BUDGET_EXHAUSTED"
    assert doc["primary_infra_component"] == "non_infra"
    assert doc["heuristic_confidence"] == 1.0
    assert doc["evidence_source"] == "code_level"


@pytest.mark.asyncio
@pytest.mark.parametrize("late_status", [WorkflowRunStatus.failed, WorkflowRunStatus.completed])
async def test_stale_terminal_writer_updates_attribution_atomically(
    sqlite_db: AgentDB,
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    late_status: WorkflowRunStatus,
) -> None:
    workflow_run_id = f"wr_attr_stale_{late_status.value}"
    async with sqlite_db.Session() as session:
        session.add(_workflow_run_model(workflow_run_id=workflow_run_id, queued_at=_ATTR_QUEUED_AT))
        await session.commit()

    snapshot_loaded = asyncio.Event()
    winner_committed = asyncio.Event()
    async with async_sessionmaker(sqlite_engine, expire_on_commit=False)() as stale_session:
        original_scalars = stale_session.scalars

        async def pause_after_read(*args: Any, **kwargs: Any) -> Any:
            result = await original_scalars(*args, **kwargs)
            snapshot_loaded.set()
            await asyncio.wait_for(winner_committed.wait(), timeout=5)
            return result

        monkeypatch.setattr(stale_session, "scalars", pause_after_read)
        stale_repository = WorkflowRunsRepository(
            session_factory=lambda: stale_session, dialect_name=sqlite_engine.dialect.name
        )
        loser = asyncio.create_task(
            stale_repository.update_workflow_run(
                workflow_run_id,
                status=late_status,
                failure_reason="browser context closed",
                failure_category=classify_from_failure_reason("browser context closed"),
            )
        )
        try:
            await asyncio.wait_for(snapshot_loaded.wait(), timeout=5)
            await sqlite_db.workflow_runs.update_workflow_run(
                workflow_run_id,
                status=WorkflowRunStatus.failed,
                failure_category=classify_from_failure_reason("No proxy available"),
            )
        finally:
            winner_committed.set()
            await asyncio.wait_for(loser, timeout=5)

    async with sqlite_db.Session() as session:
        row = (await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))).one()
        assert row.status == late_status
        if late_status == WorkflowRunStatus.completed:
            assert row.failure_attribution is None
        else:
            assert row.failure_attribution["primary_infra_component"] == "proxy"
            assert row.failure_attribution["failure_category"] == "PROXY_ERROR"
        assert row.failure_reason == "browser context closed"
        assert row.failure_category[0]["category"] == "BROWSER_ERROR"
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id) == (
        late_status == WorkflowRunStatus.completed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("supply_category", [True, False])
async def test_final_write_fills_attribution_despite_stale_nonnull_snapshot(
    sqlite_db: AgentDB,
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    supply_category: bool,
) -> None:
    # A legacy/no-attempt finalizer loads a row that still has attribution, then a concurrent
    # reset clears it to SQL NULL before this write commits. The write must still fill a real
    # document (via the flush-time COALESCE) rather than trusting its stale non-NULL snapshot.
    workflow_run_id = "wr_attr_stale_reset"
    async with sqlite_db.Session() as session:
        run = _workflow_run_model(
            workflow_run_id=workflow_run_id,
            queued_at=_ATTR_QUEUED_AT,
            failure_category=classify_from_failure_reason("browser context closed"),
        )
        run.failure_attribution = {
            "schema_version": 1,
            "classifier_version": 2,
            "failure_category": "BROWSER_ERROR",
            "primary_infra_component": "browser",
            "evidence_source": "exception_type",
            "heuristic_confidence": 1.0,
        }
        session.add(run)
        await session.commit()

    snapshot_loaded = asyncio.Event()
    reset_committed = asyncio.Event()
    async with async_sessionmaker(sqlite_engine, expire_on_commit=False)() as stale_session:
        original_scalars = stale_session.scalars

        async def pause_after_read(*args: Any, **kwargs: Any) -> Any:
            result = await original_scalars(*args, **kwargs)
            snapshot_loaded.set()
            await asyncio.wait_for(reset_committed.wait(), timeout=5)
            return result

        monkeypatch.setattr(stale_session, "scalars", pause_after_read)
        stale_repository = WorkflowRunsRepository(
            session_factory=lambda: stale_session, dialect_name=sqlite_engine.dialect.name
        )
        finalizer = asyncio.create_task(
            stale_repository.update_workflow_run(
                workflow_run_id,
                status=WorkflowRunStatus.failed,
                failure_reason="No proxy available",
                failure_category=classify_from_failure_reason("No proxy available") if supply_category else None,
            )
        )
        try:
            await asyncio.wait_for(snapshot_loaded.wait(), timeout=5)
            await sqlite_db.workflow_runs.update_workflow_run(workflow_run_id, status=WorkflowRunStatus.created)
            assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)
            async with sqlite_db.Session() as session:
                reset = await session.get(WorkflowRunModel, workflow_run_id)
                assert reset.status == WorkflowRunStatus.created
                assert reset.failure_category is None
        finally:
            reset_committed.set()
            await asyncio.wait_for(finalizer, timeout=5)

    async with sqlite_db.Session() as session:
        row = (await session.scalars(select(WorkflowRunModel).filter_by(workflow_run_id=workflow_run_id))).one()
        assert row.status == WorkflowRunStatus.failed
        assert row.failure_attribution is not None
        assert row.failure_attribution["primary_infra_component"] == ("proxy" if supply_category else "unattributed")
        assert row.failure_attribution["failure_category"] == ("PROXY_ERROR" if supply_category else None)
        if not supply_category:
            assert row.failure_attribution["evidence_source"] == "none"
            assert row.failure_attribution["heuristic_confidence"] == 0.0
            assert (
                await session.execute(
                    text("SELECT failure_category IS NULL FROM workflow_runs WHERE workflow_run_id = :wr"),
                    {"wr": workflow_run_id},
                )
            ).scalar_one()
    assert not await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)


@pytest.mark.asyncio
async def test_completed_run_leaves_attribution_null(sqlite_db: AgentDB) -> None:
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_attr_done",
                queued_at=_ATTR_QUEUED_AT,
                status=WorkflowRunStatus.running.value,
            )
        )
        await session.commit()

    await sqlite_db.workflow_runs.update_workflow_run("wr_attr_done", status=WorkflowRunStatus.completed)

    assert await _read_failure_attribution(sqlite_db, "wr_attr_done") is None


@pytest.mark.asyncio
async def test_if_not_final_completed_clears_stale_attribution(sqlite_db: AgentDB) -> None:
    # A completed CAS winner via update_workflow_run_if_not_final (e.g. an old completion
    # finalizer winning during a reset window) must clear any lingering attribution, matching
    # update_workflow_run. Otherwise a completed run keeps a prior failure's attribution.
    workflow_run_id = "wr_attr_ifnf_completed"
    async with sqlite_db.Session() as session:
        run = _workflow_run_model(
            workflow_run_id=workflow_run_id,
            queued_at=_ATTR_QUEUED_AT,
            status=WorkflowRunStatus.running.value,
        )
        run.failure_attribution = {
            "schema_version": 1,
            "classifier_version": 2,
            "failure_category": "BROWSER_ERROR",
            "primary_infra_component": "browser",
            "evidence_source": "exception_type",
            "heuristic_confidence": 1.0,
        }
        session.add(run)
        await session.commit()

    updated = await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id=workflow_run_id,
        status=WorkflowRunStatus.completed,
    )
    assert updated is not None

    async with sqlite_db.Session() as session:
        row = await session.get(WorkflowRunModel, workflow_run_id)
        assert row.status == WorkflowRunStatus.completed.value
        assert row.failure_attribution is None
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)


@pytest.mark.asyncio
async def test_late_completion_clears_existing_failure_attribution(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_late_completion"
    async with sqlite_db.Session() as session:
        session.add(_workflow_run_model(workflow_run_id=workflow_run_id, queued_at=_ATTR_QUEUED_AT))
        await session.commit()

    await sqlite_db.workflow_runs.update_workflow_run(
        workflow_run_id,
        status=WorkflowRunStatus.failed,
        failure_category=classify_from_failure_reason("No proxy available"),
    )
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"
    # Legacy runs without attempt rows fall back to the unconditional updater after this CAS loses.
    assert (
        await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
            workflow_run_id, status=WorkflowRunStatus.completed
        )
        is None
    )
    completed = await sqlite_db.workflow_runs.update_workflow_run(workflow_run_id, status=WorkflowRunStatus.completed)

    assert completed.status == WorkflowRunStatus.completed
    assert completed.failure_category is not None
    assert completed.failure_category[0]["category"] == "PROXY_ERROR"
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)


@pytest.mark.asyncio
async def test_canceled_if_not_final_writes_unattributed_document(sqlite_db: AgentDB) -> None:
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_attr_canceled",
                queued_at=_ATTR_QUEUED_AT,
                status=WorkflowRunStatus.created.value,
            )
        )
        await session.commit()

    updated = await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id="wr_attr_canceled",
        status=WorkflowRunStatus.canceled,
        failure_reason="canceled by operator",
    )
    assert updated is not None

    doc = await _read_failure_attribution(sqlite_db, "wr_attr_canceled")
    assert doc is not None
    assert doc["primary_infra_component"] == "unattributed"


@pytest.mark.asyncio
async def test_if_not_final_cas_loser_cannot_change_attribution(sqlite_db: AgentDB) -> None:
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_attr_cas",
                queued_at=_ATTR_QUEUED_AT,
                status=WorkflowRunStatus.created.value,
            )
        )
        await session.commit()

    failure_category = classify_from_failure_reason("No proxy available for this run", fallback_to_unknown=True)
    winner = await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id="wr_attr_cas",
        status=WorkflowRunStatus.failed,
        failure_reason="No proxy available for this run",
        failure_category=failure_category,
    )
    assert winner is not None

    # The row is already terminal, so this late cancel is a no-op and must not clobber.
    loser = await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id="wr_attr_cas",
        status=WorkflowRunStatus.canceled,
    )
    assert loser is None

    doc = await _read_failure_attribution(sqlite_db, "wr_attr_cas")
    assert doc["primary_infra_component"] == "proxy"


@pytest.mark.asyncio
async def test_repair_strengthens_provisional_but_never_touches_a_finished_row(sqlite_db: AgentDB) -> None:
    proxy_category = classify_from_failure_reason("No proxy available for this run", fallback_to_unknown=True)

    # Unfinished (provisional) row from the bulk sweep: the repair strengthens the provisional
    # unattributed to the typed derived attribution and stamps finished_at.
    async with sqlite_db.Session() as session:
        provisional = _workflow_run_model(
            workflow_run_id="wr_attr_provisional",
            queued_at=_ATTR_QUEUED_AT,
            status=WorkflowRunStatus.timed_out.value,
        )
        provisional.failure_attribution = {"primary_infra_component": "unattributed"}
        provisional.finished_at = None
        session.add(provisional)
        await session.commit()

    result = await sqlite_db.workflow_runs.finish_preexisting_timed_out_workflow_run(
        "wr_attr_provisional", failure_reason="No proxy available", failure_category=proxy_category
    )
    assert result is not None
    assert (await _read_failure_attribution(sqlite_db, "wr_attr_provisional"))["primary_infra_component"] == "proxy"

    # Finished row (a genuine finalizer already stamped finished_at): the finished_at IS NULL
    # guard makes the repair a no-op, so it never overwrites a genuine finalizer's document.
    genuine = {
        "schema_version": 1,
        "classifier_version": 2,
        "failure_category": "BROWSER_ERROR",
        "primary_infra_component": "browser",
        "evidence_source": "exception_type",
        "heuristic_confidence": 0.9,
    }
    async with sqlite_db.Session() as session:
        finished = _workflow_run_model(
            workflow_run_id="wr_attr_finished",
            queued_at=_ATTR_QUEUED_AT,
            status=WorkflowRunStatus.timed_out.value,
        )
        finished.failure_attribution = genuine
        finished.finished_at = _ATTR_QUEUED_AT
        session.add(finished)
        await session.commit()

    no_op = await sqlite_db.workflow_runs.finish_preexisting_timed_out_workflow_run(
        "wr_attr_finished", failure_reason="timed out", failure_category=proxy_category
    )
    assert no_op is None
    assert (await _read_failure_attribution(sqlite_db, "wr_attr_finished"))["primary_infra_component"] == "browser"


@pytest.mark.asyncio
async def test_late_update_workflow_run_cannot_replace_existing_attribution(sqlite_db: AgentDB) -> None:
    proxy_doc = {
        "schema_version": 1,
        "classifier_version": 1,
        "failure_category": "PROXY_ERROR",
        "primary_infra_component": "proxy",
        "evidence_source": "exception_type",
        "heuristic_confidence": 0.9,
    }
    async with sqlite_db.Session() as session:
        run = _workflow_run_model(
            workflow_run_id="wr_attr_late",
            queued_at=_ATTR_QUEUED_AT,
            status=WorkflowRunStatus.failed.value,
        )
        run.failure_attribution = proxy_doc
        session.add(run)
        await session.commit()

    browser_category = classify_from_failure_reason(None, exception_name="TargetClosedError", fallback_to_unknown=True)
    await sqlite_db.workflow_runs.update_workflow_run(
        "wr_attr_late",
        status=WorkflowRunStatus.failed,
        failure_category=browser_category,
    )

    doc = await _read_failure_attribution(sqlite_db, "wr_attr_late")
    assert doc["primary_infra_component"] == "proxy"


def test_failure_attribution_absent_from_customer_facing_serializers() -> None:
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun as LegacyWorkflowRun
    from skyvern.schemas.runs import BaseRunResponse, BlockRunResponse, TaskRunResponse, WorkflowRunResponse

    for model in (LegacyWorkflowRun, BaseRunResponse, TaskRunResponse, WorkflowRunResponse, BlockRunResponse):
        assert "failure_attribution" not in model.model_fields, model.__name__


async def _seed_failed_run_with_attribution(sqlite_db: AgentDB, workflow_run_id: str) -> None:
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id=workflow_run_id,
                queued_at=_ATTR_QUEUED_AT,
                status=WorkflowRunStatus.created.value,
            )
        )
        await session.commit()
    proxy_category = classify_from_failure_reason("No proxy available for this run", fallback_to_unknown=True)
    await sqlite_db.workflow_runs.update_workflow_run(
        workflow_run_id,
        status=WorkflowRunStatus.failed,
        failure_reason="No proxy available for this run",
        failure_category=proxy_category,
    )


@pytest.mark.asyncio
async def test_retry_reopen_clears_stale_attribution(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_reopen"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"

    async with sqlite_db.Session() as session:
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=workflow_run_id,
                attempt_number=1,
                organization_id="org_test",
                status=WorkflowRunStatus.failed.value,
                retry_decision="retry",
                next_attempt_at=_ATTR_QUEUED_AT,
            )
        )
        await session.commit()

    preparation = await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=None,
    )
    assert preparation.status == "inserted"

    async with sqlite_db.Session() as session:
        reopened = await session.get(WorkflowRunModel, workflow_run_id)
        assert reopened.status == WorkflowRunStatus.queued.value
        assert reopened.failure_attribution is None
    # SQL grain, not just ORM None: the reopen must store real SQL NULL, not JSON 'null'.
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)

    # A completed retry leaves it NULL; a differently-failed retry writes the new document.
    await sqlite_db.workflow_runs.update_workflow_run(workflow_run_id, status=WorkflowRunStatus.completed)
    assert await _read_failure_attribution(sqlite_db, workflow_run_id) is None
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)


@pytest.mark.asyncio
async def test_retry_reopen_then_new_failure_writes_new_document(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_reopen_refail"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    async with sqlite_db.Session() as session:
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=workflow_run_id,
                attempt_number=1,
                organization_id="org_test",
                status=WorkflowRunStatus.failed.value,
                retry_decision="retry",
                next_attempt_at=_ATTR_QUEUED_AT,
            )
        )
        await session.commit()
    await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=None,
    )

    browser_category = classify_from_failure_reason(None, exception_name="TargetClosedError", fallback_to_unknown=True)
    await sqlite_db.workflow_runs.update_workflow_run(
        workflow_run_id,
        status=WorkflowRunStatus.failed,
        failure_category=browser_category,
    )
    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc["primary_infra_component"] == "browser"


@pytest.mark.asyncio
async def test_fail_prepared_workflow_run_persists_unattributed_document(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_prepared"
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id=workflow_run_id,
                queued_at=_ATTR_QUEUED_AT,
                status=WorkflowRunStatus.queued.value,
            )
        )
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=workflow_run_id,
                attempt_number=1,
                organization_id="org_test",
                status="queued",
            )
        )
        await session.commit()

    claimed = await sqlite_db.workflow_run_attempts.fail_prepared_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        attempt_number=1,
        failure_reason="prepared attempt never started",
    )
    assert claimed is True
    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc is not None
    assert doc["primary_infra_component"] == "unattributed"

    # Idempotent: a second call finds the row already terminal and does not change attribution.
    again = await sqlite_db.workflow_run_attempts.fail_prepared_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        attempt_number=1,
        failure_reason="prepared attempt never started",
    )
    assert again is False
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "unattributed"


@pytest.mark.asyncio
async def test_fail_prepared_does_not_clobber_existing_attribution(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_prepared_existing"
    proxy_doc = {
        "schema_version": 1,
        "classifier_version": 1,
        "failure_category": "PROXY_ERROR",
        "primary_infra_component": "proxy",
        "evidence_source": "exception_type",
        "heuristic_confidence": 0.9,
    }
    async with sqlite_db.Session() as session:
        run = _workflow_run_model(
            workflow_run_id=workflow_run_id,
            queued_at=_ATTR_QUEUED_AT,
            status=WorkflowRunStatus.queued.value,
        )
        run.failure_attribution = proxy_doc
        session.add(run)
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=workflow_run_id,
                attempt_number=1,
                organization_id="org_test",
                status="queued",
            )
        )
        await session.commit()

    await sqlite_db.workflow_run_attempts.fail_prepared_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        attempt_number=1,
        failure_reason="prepared attempt never started",
    )
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"


@pytest.mark.asyncio
async def test_bulk_timeout_persists_provisional_attribution_then_repair_strengthens(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_bulk_timeout"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"

    # Bulk stuck-run cleanup persists a bounded provisional (unattributed) attribution atomically
    # — never SQL NULL — so a stuck-run timeout is never left permanently unattributable if the
    # per-run repair does not run. failure_category is cleared so the repair can fill it.
    await sqlite_db.workflow_runs.bulk_update_workflow_runs(
        [workflow_run_id],
        status=WorkflowRunStatus.timed_out,
    )
    async with sqlite_db.Session() as session:
        marked = await session.get(WorkflowRunModel, workflow_run_id)
        assert marked.status == WorkflowRunStatus.timed_out.value
        assert marked.failure_attribution["primary_infra_component"] == "unattributed"
        assert marked.failure_category is None
        assert marked.finished_at is None
    assert not await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)

    # The repair runs only on the unfinished (provisional) row, so it safely strengthens the
    # provisional unattributed to the typed timeout attribution and fills the category.
    result = await sqlite_db.workflow_runs.finish_preexisting_timed_out_workflow_run(
        workflow_run_id,
        failure_reason="activity timeout",
        failure_category=classify_from_failure_reason("activity timeout", fallback_to_unknown=True),
    )
    assert result is not None
    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc["primary_infra_component"] == "worker"
    async with sqlite_db.Session() as session:
        repaired = await session.get(WorkflowRunModel, workflow_run_id)
        assert repaired.failure_category[0]["category"] == "INFRASTRUCTURE_ERROR"

    # Idempotent: the row is now finished, so a repeat repair is a no-op and cannot weaken it.
    repeat = await sqlite_db.workflow_runs.finish_preexisting_timed_out_workflow_run(
        workflow_run_id,
        failure_reason="activity timeout",
        failure_category=None,
    )
    assert repeat is None
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "worker"


@pytest.mark.asyncio
async def test_reset_clear_wipes_reason_category_and_attribution_when_created(sqlite_db: AgentDB) -> None:
    workflow_run_id = "wr_attr_reset"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"
    # reset_workflow_run first transitions the row to `created`, then calls this clear.
    await sqlite_db.workflow_runs.update_workflow_run(workflow_run_id, status=WorkflowRunStatus.created)

    await sqlite_db.workflow_runs.clear_workflow_run_failure_reason(workflow_run_id, "org_test")

    async with sqlite_db.Session() as session:
        cleared = await session.get(WorkflowRunModel, workflow_run_id)
        assert cleared.failure_reason is None
        # failure_category is cleared too, so a later terminal transition on the reset run does
        # not derive attribution from the stale category.
        assert cleared.failure_category is None
        assert cleared.failure_attribution is None
    # SQL grain: a reset row must be SQL NULL so it never enters the Redshift projection.
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)


@pytest.mark.asyncio
async def test_update_to_created_clears_failure_state_and_closes_reset_window(sqlite_db: AgentDB) -> None:
    # Reopening/resetting a run to `created` clears reason/category/attribution in the same update,
    # so no stale document lingers on the non-terminal row. A later failure then derives fresh
    # attribution (COALESCE onto SQL NULL) instead of retaining the previous attempt's document.
    workflow_run_id = "wr_attr_created_clear"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"

    await sqlite_db.workflow_runs.update_workflow_run(workflow_run_id, status=WorkflowRunStatus.created)
    async with sqlite_db.Session() as session:
        reopened = await session.get(WorkflowRunModel, workflow_run_id)
        assert reopened.status == WorkflowRunStatus.created.value
        assert reopened.failure_reason is None
        assert reopened.failure_category is None
        assert reopened.failure_attribution is None
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)

    await sqlite_db.workflow_runs.update_workflow_run(
        workflow_run_id,
        status=WorkflowRunStatus.failed,
        failure_category=classify_from_failure_reason(
            None, exception_name="TargetClosedError", fallback_to_unknown=True
        ),
    )
    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc["primary_infra_component"] == "browser"


@pytest.mark.asyncio
async def test_reset_clear_is_noop_when_a_finalizer_already_took_the_row(sqlite_db: AgentDB) -> None:
    # A finalizer that raced in (created -> failed with fresh attribution) between the reset's
    # status write and this clear must not be clobbered: the clear only fires on a `created` row.
    workflow_run_id = "wr_attr_reset_raced"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    assert (await _read_failure_attribution(sqlite_db, workflow_run_id))["primary_infra_component"] == "proxy"

    # Row is still `failed` (finalizer won the race), not `created`.
    await sqlite_db.workflow_runs.clear_workflow_run_failure_reason(workflow_run_id, "org_test")

    async with sqlite_db.Session() as session:
        row = await session.get(WorkflowRunModel, workflow_run_id)
        assert row.status == WorkflowRunStatus.failed.value
        assert row.failure_attribution["primary_infra_component"] == "proxy"
        assert row.failure_category is not None


@pytest.mark.asyncio
async def test_reopen_then_cas_cancel_writes_document_after_sql_null_clear(sqlite_db: AgentDB) -> None:
    # End-to-end: fail (writes doc) -> reopen (SQL NULL) -> CAS cancel must COALESCE a new
    # document in, which is only possible because the reopen stored SQL NULL, not JSON 'null'.
    workflow_run_id = "wr_attr_reopen_cas"
    await _seed_failed_run_with_attribution(sqlite_db, workflow_run_id)
    async with sqlite_db.Session() as session:
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=workflow_run_id,
                attempt_number=1,
                organization_id="org_test",
                status=WorkflowRunStatus.failed.value,
                retry_decision="retry",
                next_attempt_at=_ATTR_QUEUED_AT,
            )
        )
        await session.commit()

    await sqlite_db.workflow_runs.prepare_next_attempt_atomic(
        workflow_run_id=workflow_run_id,
        organization_id="org_test",
        from_attempt=1,
        expected_status=WorkflowRunStatus.failed,
        browser_session_id=None,
    )
    assert await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)

    canceled = await sqlite_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id=workflow_run_id,
        status=WorkflowRunStatus.canceled,
        failure_reason="canceled after reopen",
    )
    assert canceled is not None
    doc = await _read_failure_attribution(sqlite_db, workflow_run_id)
    assert doc is not None
    assert doc["primary_infra_component"] == "unattributed"
    assert not await _failure_attribution_is_sql_null(sqlite_db, workflow_run_id)


@pytest.mark.asyncio
async def test_batch_create_with_empty_list_returns_empty() -> None:
    """create_workflow_run_parameters with an empty list should short-circuit and return []."""
    session = MagicMock()
    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    result = await repo.create_workflow_run_parameters(
        workflow_run_id="wr_test",
        workflow_parameter_values=[],
    )

    assert result == []
    session.add_all.assert_not_called()


@pytest.mark.asyncio
async def test_batch_create_propagates_sqlalchemy_error_from_flush() -> None:
    """When flush() raises an IntegrityError, it should propagate without being swallowed."""
    db_error = IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
    session = MagicMock()
    session.add_all = MagicMock()
    session.flush = AsyncMock(side_effect=db_error)
    session.commit = AsyncMock()

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    param = _make_workflow_parameter("url")

    with pytest.raises(IntegrityError) as exc_info:
        await repo.create_workflow_run_parameters(
            workflow_run_id="wr_test",
            workflow_parameter_values=[(param, "https://example.com")],
        )

    assert exc_info.value is db_error
    session.add_all.assert_called_once()
    session.flush.assert_awaited_once()
    # commit should NOT be called when flush fails
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_create_propagates_sqlalchemy_error_from_commit() -> None:
    """When commit() raises an IntegrityError, it should propagate without being swallowed."""
    db_error = IntegrityError("INSERT", {}, Exception("FK constraint failed"))
    tracked_models: list = []

    session = MagicMock()
    session.add_all = MagicMock(side_effect=lambda models: tracked_models.extend(models))

    async def _flush() -> None:
        now = datetime.now(tz=timezone.utc)
        for model in tracked_models:
            model.created_at = now

    session.flush = AsyncMock(side_effect=_flush)
    session.commit = AsyncMock(side_effect=db_error)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    param = _make_workflow_parameter("url")

    with pytest.raises(IntegrityError) as exc_info:
        await repo.create_workflow_run_parameters(
            workflow_run_id="wr_test",
            workflow_parameter_values=[(param, "https://example.com")],
        )

    assert exc_info.value is db_error
    session.flush.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_all_runs_v2_search_key_matches_run_id_and_workflow_permanent_id() -> None:
    """Regression test for SKY-8795: searching by run_id (wr_*/tsk_*) or wpid_*
    on the global runs page must match the underlying ID columns, not only
    `searchable_text` (which contains only title + url)."""
    captured_queries: list[Any] = []

    async def _execute(query):
        captured_queries.append(query)
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test", page=10, page_size=100, search_key="wr_abc123")

    assert len(captured_queries) == 2

    # Inspect WHERE clauses specifically — these columns are also in SELECT lists,
    # so substring checks on full SQL would be false positives.
    where_clause = _where_clause_sql(captured_queries[0])
    assert "task_runs.run_id" in where_clause
    # WPID search must match across both task_runs and the joined workflow_runs
    # so legacy rows with task_runs.workflow_permanent_id=NULL still hit.
    assert "coalesce(task_runs.workflow_permanent_id, workflow_runs.workflow_permanent_id)" in where_clause
    # autoescape rewrites '_' to e.g. '/_' so check the distinctive suffix.
    assert "abc123" in where_clause
    assert ".".join(("workflows", "title")) not in where_clause

    fallback_where_clause = _where_clause_sql(captured_queries[1])
    assert "workflow_runs.workflow_run_id" in fallback_where_clause
    assert ".".join(("workflows", "title")) in fallback_where_clause
    assert "workflow_runs.workflow_permanent_id" in fallback_where_clause
    assert "task_runs.run_id = workflow_runs.workflow_run_id" in fallback_where_clause

    for query in captured_queries:
        assert f"LIMIT {MAX_SEARCH_FETCH_LIMIT}" in _query_sql(query)


@pytest.mark.asyncio
async def test_get_all_runs_v2_search_key_matches_parameter_inputs() -> None:
    """Regression test for SKY-11217: Run History search must match agent input values
    (workflow_run_parameters key/description/value + extra_http_headers) on the primary
    task_runs query — not only searchable_text/run_id/wpid. The SKY-7600 unified task_runs
    migration repointed the runs list to the v2 path and dropped parameter-value search for
    runs that have a task_runs row (the common case); the fallback query only covers orphan
    workflow_runs, so param search must live on the primary query too."""
    captured_queries: list[Any] = []

    async def _execute(query):
        captured_queries.append(query)
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test", search_key="Paris")

    primary_where = _where_clause_sql(captured_queries[0])
    # Parameter EXISTS subqueries correlate on the run_id of the primary task_runs row.
    assert "workflow_run_parameters.workflow_run_id = task_runs.run_id" in primary_where
    assert "workflow_run_parameters.value" in primary_where
    assert "workflow_parameters.key" in primary_where
    assert "workflow_parameters.description" in primary_where
    assert "extra_http_headers" in primary_where
    # Param search must not drag workflows.title into the primary query (no implicit FROM workflows).
    assert ".".join(("workflows", "title")) not in primary_where


@pytest.mark.asyncio
async def test_get_all_runs_v2_selects_workflow_deleted_flag() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test")

    rendered = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    assert "AS workflow_deleted" in rendered
    # NOT EXISTS subquery against an active (non-deleted) workflows row.
    assert "NOT (EXISTS" in rendered
    assert "workflows.deleted_at IS NULL" in rendered
    # WPID must coalesce task_runs over workflow_runs so legacy rows where
    # task_runs.workflow_permanent_id is NULL still resolve via the join.
    assert "coalesce(task_runs.workflow_permanent_id, workflow_runs.workflow_permanent_id)" in rendered


@pytest.mark.asyncio
async def test_get_all_runs_v2_status_filter_uses_coalesced_effective_status() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test")

    where_clause = _where_clause_sql(captured["query"]).lower()
    assert "coalesce(workflow_runs.status, task_runs.status) is not null" in where_clause
    assert "task_runs.status is not null" not in where_clause


@pytest.mark.asyncio
async def test_get_all_runs_v2_run_type_filter_applies_task_run_type_predicate() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test", run_type=["task_v1", "task_v2"])

    where_clause = _where_clause_sql(captured["query"])
    assert "task_runs.task_run_type IN ('task_v1', 'task_v2')" in where_clause


@pytest.mark.asyncio
async def test_get_all_runs_v2_run_type_filter_gates_workflow_run_search_fallback() -> None:
    """The search fallback query only ever yields workflow_run rows, so it must be
    skipped when the run_type filter excludes workflow_run and kept when it doesn't."""
    captured_queries: list[Any] = []

    async def _execute(query):
        captured_queries.append(query)
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test", search_key="wr_abc123", run_type=["task_v1"])
    assert len(captured_queries) == 1

    captured_queries.clear()
    await repo.get_all_runs_v2(organization_id="o_test", search_key="wr_abc123", run_type=["task_v1", "workflow_run"])
    assert len(captured_queries) == 2


@pytest.mark.asyncio
async def test_get_all_runs_v2_filters_by_one_workflow_and_excludes_null_wpid_tasks(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_agent_a",
                    workflow_permanent_id="wpid_a",
                    queued_at=created_at,
                    status=WorkflowRunStatus.completed.value,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_agent_b",
                    workflow_permanent_id="wpid_b",
                    queued_at=created_at,
                    status=WorkflowRunStatus.completed.value,
                ),
                _task_run_model(
                    run_id="wr_agent_a",
                    created_at=created_at,
                    workflow_permanent_id="wpid_a",
                ),
                _task_run_model(
                    run_id="wr_agent_b",
                    created_at=created_at,
                    workflow_permanent_id="wpid_b",
                ),
                _task_run_model(
                    run_id="tsk_standalone",
                    created_at=created_at,
                    workflow_permanent_id=None,
                    task_run_type=RunType.task_v1.value,
                ),
            ]
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        workflow_permanent_ids=["wpid_a"],
    )

    assert {row["run_id"] for row in rows} == {"wr_agent_a"}


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_uses_or_semantics(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _task_run_model(run_id="wr_agent_a", created_at=created_at, workflow_permanent_id="wpid_a"),
                _task_run_model(run_id="wr_agent_b", created_at=created_at, workflow_permanent_id="wpid_b"),
                _task_run_model(run_id="wr_agent_c", created_at=created_at, workflow_permanent_id="wpid_c"),
            ]
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        workflow_permanent_ids=["wpid_a", "wpid_b"],
    )

    assert {row["run_id"] for row in rows} == {"wr_agent_a", "wr_agent_b"}


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_composes_with_status(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_agent_a_completed",
                    workflow_permanent_id="wpid_a",
                    queued_at=created_at,
                    status=WorkflowRunStatus.completed.value,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_agent_a_failed",
                    workflow_permanent_id="wpid_a",
                    queued_at=created_at,
                    status=WorkflowRunStatus.failed.value,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_agent_b_completed",
                    workflow_permanent_id="wpid_b",
                    queued_at=created_at,
                    status=WorkflowRunStatus.completed.value,
                ),
                _task_run_model(
                    run_id="wr_agent_a_completed",
                    created_at=created_at,
                    workflow_permanent_id="wpid_a",
                ),
                _task_run_model(
                    run_id="wr_agent_a_failed",
                    created_at=created_at,
                    workflow_permanent_id="wpid_a",
                    status=WorkflowRunStatus.failed.value,
                ),
                _task_run_model(
                    run_id="wr_agent_b_completed",
                    created_at=created_at,
                    workflow_permanent_id="wpid_b",
                ),
            ]
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        status=[WorkflowRunStatus.completed.value],
        workflow_permanent_ids=["wpid_a"],
    )

    assert {row["run_id"] for row in rows} == {"wr_agent_a_completed"}


@pytest.mark.asyncio
async def test_get_all_runs_v2_failure_category_filter_matches_top_category_and_excludes_canceled(
    sqlite_db: AgentDB,
) -> None:
    """SKY-14821: filtering by failure_category matches only the classifier's top category, and
    never returns a canceled run even if it happens to carry a matching category."""
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    matching_category = [{"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "reasoning": "blocked"}]
    other_category = [{"category": "AUTH_FAILURE", "confidence_float": 0.9, "reasoning": "bad login"}]

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_matches",
                    queued_at=created_at,
                    status=WorkflowRunStatus.failed.value,
                    failure_category=matching_category,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_other_category",
                    queued_at=created_at,
                    status=WorkflowRunStatus.failed.value,
                    failure_category=other_category,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_canceled_but_matches",
                    queued_at=created_at,
                    status=WorkflowRunStatus.canceled.value,
                    failure_category=matching_category,
                ),
                _task_run_model(
                    run_id="wr_matches",
                    created_at=created_at,
                    workflow_permanent_id="wpid_test",
                    status=WorkflowRunStatus.failed.value,
                ),
                _task_run_model(
                    run_id="wr_other_category",
                    created_at=created_at,
                    workflow_permanent_id="wpid_test",
                    status=WorkflowRunStatus.failed.value,
                ),
                _task_run_model(
                    run_id="wr_canceled_but_matches",
                    created_at=created_at,
                    workflow_permanent_id="wpid_test",
                    status=WorkflowRunStatus.canceled.value,
                ),
            ]
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        failure_category="ANTI_BOT_DETECTION",
    )

    assert {row["run_id"] for row in rows} == {"wr_matches"}


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_applies_to_search_fallback(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_orphan_target",
                workflow_permanent_id="wpid_target",
                queued_at=created_at,
                status=WorkflowRunStatus.completed.value,
            )
        )
        await session.commit()

    excluded_rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        search_key="orphan_target",
        workflow_permanent_ids=["wpid_other"],
    )
    included_rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        search_key="orphan_target",
        workflow_permanent_ids=["wpid_target"],
    )

    assert "wr_orphan_target" not in {row["run_id"] for row in excluded_rows}
    assert "wr_orphan_target" in {row["run_id"] for row in included_rows}


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_merges_orphan_runs_by_created_at(sqlite_db: AgentDB) -> None:
    newest = datetime(2026, 7, 20, 12, 3, tzinfo=timezone.utc)
    middle = datetime(2026, 7, 20, 12, 2, tzinfo=timezone.utc)
    oldest = datetime(2026, 7, 20, 12, 1, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_backed_newest",
                    workflow_permanent_id="wpid_target",
                    queued_at=newest,
                    status=WorkflowRunStatus.completed.value,
                ),
                _task_run_model(
                    run_id="wr_backed_newest",
                    created_at=newest,
                    workflow_permanent_id="wpid_target",
                ),
                _workflow_run_model(
                    workflow_run_id="wr_orphan_middle",
                    workflow_permanent_id="wpid_target",
                    queued_at=middle,
                    status=WorkflowRunStatus.completed.value,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_backed_oldest",
                    workflow_permanent_id="wpid_target",
                    queued_at=oldest,
                    status=WorkflowRunStatus.completed.value,
                ),
                _task_run_model(
                    run_id="wr_backed_oldest",
                    created_at=oldest,
                    workflow_permanent_id="wpid_target",
                ),
            ]
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        workflow_permanent_ids=["wpid_target"],
    )

    assert [row["run_id"] for row in rows] == ["wr_backed_newest", "wr_orphan_middle", "wr_backed_oldest"]


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_skips_orphan_fallback_for_task_run_type(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_orphan_target",
                workflow_permanent_id="wpid_target",
                queued_at=created_at,
                status=WorkflowRunStatus.completed.value,
            )
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        workflow_permanent_ids=["wpid_target"],
        run_type=[RunType.task_v1.value],
    )

    assert rows == []


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_does_not_search_filter_orphan_fallback(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_unrelated_orphan",
                workflow_permanent_id="wpid_target",
                queued_at=created_at,
                status=WorkflowRunStatus.completed.value,
            )
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        workflow_permanent_ids=["wpid_target"],
    )

    assert [row["run_id"] for row in rows] == ["wr_unrelated_orphan"]
    assert rows[0]["title"] is None


@pytest.mark.asyncio
async def test_get_all_runs_v2_workflow_filter_uses_joined_wpid_for_legacy_task_rows(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_legacy",
                    workflow_permanent_id="wpid_legacy",
                    queued_at=created_at,
                    status=WorkflowRunStatus.completed.value,
                ),
                _task_run_model(
                    run_id="wr_legacy",
                    created_at=created_at,
                    workflow_permanent_id=None,
                ),
            ]
        )
        await session.commit()

    rows = await sqlite_db.workflow_runs.get_all_runs_v2(
        organization_id="org_test",
        workflow_permanent_ids=["wpid_legacy"],
    )

    assert {row["run_id"] for row in rows} == {"wr_legacy"}
    assert rows[0]["workflow_permanent_id"] == "wpid_legacy"


@pytest.mark.asyncio
async def test_get_all_runs_v2_excludes_copilot_session_workflow_runs() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs_v2(organization_id="o_test")

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause
    _assert_not_filtering_copilot_authored_workflows(where_clause)


@pytest.mark.asyncio
async def test_get_all_runs_excludes_copilot_session_workflow_runs() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)
    session.scalars = AsyncMock(return_value=_EmptyExecuteResult())

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_all_runs(organization_id="o_test")

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause
    _assert_not_filtering_copilot_authored_workflows(where_clause)


@pytest.mark.asyncio
async def test_workflow_run_history_queries_exclude_copilot_session_runs() -> None:
    captured_queries: list[Any] = []

    async def _execute(query):
        captured_queries.append(query)
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs(organization_id="o_test")
    await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
        exclude_child_runs=True,
    )

    assert len(captured_queries) == 2
    for query in captured_queries:
        where_clause = _where_clause_sql(query)
        assert "workflow_runs.copilot_session_id IS NULL" in where_clause
        _assert_not_filtering_copilot_authored_workflows(where_clause)
    workflow_runs_for_workflow_clause = _where_clause_sql(captured_queries[1])
    assert "workflow_runs.parent_workflow_run_id IS NULL" in workflow_runs_for_workflow_clause


@pytest.mark.asyncio
async def test_get_workflow_runs_for_workflow_permanent_id_keeps_child_runs_by_default() -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs_for_workflow_permanent_id(
        workflow_permanent_id="wpid_test",
        organization_id="o_test",
    )

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.parent_workflow_run_id IS NULL" not in where_clause
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause


@pytest.mark.asyncio
async def test_get_workflow_runs_for_browser_session_filters_and_excludes(
    sqlite_db: AgentDB, sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def _execute(query):
        captured["query"] = query
        return _EmptyExecuteResult()

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_execute)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_workflow_runs_for_browser_session(
        browser_session_id="pbs_abc123",
        organization_id="o_test",
        page=2,
        page_size=5,
    )

    where_clause = _where_clause_sql(captured["query"])
    assert "workflow_runs.browser_session_id = 'pbs_abc123'" in where_clause
    assert "workflow_runs.organization_id = 'o_test'" in where_clause
    assert "workflow_runs.parent_workflow_run_id IS NULL" in where_clause
    assert "workflow_runs.copilot_session_id IS NULL" in where_clause
    _assert_not_filtering_copilot_authored_workflows(where_clause)

    rendered = str(captured["query"].compile(compile_kwargs={"literal_binds": True}))
    assert "ORDER BY workflow_runs.created_at DESC" in rendered
    assert "LIMIT 5" in rendered
    assert "OFFSET 5" in rendered

    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    runs = [
        _workflow_run_model(
            workflow_run_id=f"wr_session_{i}", queued_at=now + timedelta(seconds=i), browser_session_id="pbs_test"
        )
        for i in range(4)
    ]
    foreign = _workflow_run_model(
        workflow_run_id="wr_foreign", queued_at=now, browser_session_id="pbs_test", organization_id="org_other"
    )
    child = _workflow_run_model(workflow_run_id="wr_child", queued_at=now, browser_session_id="pbs_test")
    child.parent_workflow_run_id = runs[0].workflow_run_id
    copilot = _workflow_run_model(workflow_run_id="wr_copilot", queued_at=now, browser_session_id="pbs_test")
    copilot.copilot_session_id = "cps_test"
    other_session = _workflow_run_model(
        workflow_run_id="wr_other_session", queued_at=now, browser_session_id="pbs_other"
    )
    async with sqlite_db.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_test", workflow_permanent_id="wpid_test", title="test", workflow_definition={}
            )
        )
        session.add_all([*runs, foreign, child, copilot, other_session])
        session.add_all(
            [
                WorkflowRunAttemptModel(
                    workflow_run_id=run.workflow_run_id,
                    organization_id="org_test",
                    attempt_number=number,
                    status="queued" if number == 2 else "failed",
                )
                for run in runs
                for number in (1, 2)
            ]
        )
        await session.commit()
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    statements, listener = _capture_sql(sqlite_engine)
    try:
        page = await workflow_service_module.WorkflowService().get_workflow_runs_for_browser_session(
            browser_session_id="pbs_test", organization_id="org_test", page=2, page_size=2
        )
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", listener)

    assert [run.workflow_run_id for run in page] == ["wr_session_1", "wr_session_0"]
    assert [run.attempt for run in page] == [2, 2]
    assert len([statement for statement in statements if "FROM workflow_run_attempts" in statement]) == 1


@pytest.mark.asyncio
async def test_get_last_queued_workflow_run_can_include_browser_session_rows(sqlite_db: AgentDB) -> None:
    created_at = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_forced_prior",
                workflow_id="wf_test",
                workflow_permanent_id="wpid_test",
                organization_id="org_test",
                browser_session_id="pbs_forced",
                status=WorkflowRunStatus.queued.value,
                sequential_key="cred_a",
                created_at=created_at,
                modified_at=created_at,
                queued_at=created_at,
            )
        )
        await session.commit()

    default_result = await sqlite_db.workflow_runs.get_last_queued_workflow_run(
        "wpid_test",
        "org_test",
        "cred_a",
    )
    included_result = await sqlite_db.workflow_runs.get_last_queued_workflow_run(
        "wpid_test",
        "org_test",
        "cred_a",
        include_browser_session_rows=True,
    )

    assert default_result is None
    assert included_result is not None
    assert included_result.workflow_run_id == "wr_forced_prior"


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_forced_key_lane_includes_session_rows(
    sqlite_db: AgentDB,
) -> None:
    prior_queued_at = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    self_queued_at = datetime(2026, 7, 4, 12, 1, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _persistent_browser_session_model(persistent_browser_session_id="pbs_prior"),
                _persistent_browser_session_model(persistent_browser_session_id="pbs_self"),
                _workflow_run_model(
                    workflow_run_id="wr_prior",
                    browser_session_id="pbs_prior",
                    sequential_key="cred_a",
                    queued_at=prior_queued_at,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_self",
                    browser_session_id="pbs_self",
                    sequential_key="cred_a",
                    queued_at=self_queued_at,
                ),
            ]
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_self")

    assert blocker is not None
    assert blocker.workflow_run_id == "wr_prior"


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_prior_forced_visible_to_later_forced(
    sqlite_db: AgentDB,
) -> None:
    prior_queued_at = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    self_queued_at = datetime(2026, 7, 4, 12, 1, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _persistent_browser_session_model(persistent_browser_session_id="pbs_forced_prior"),
                _persistent_browser_session_model(persistent_browser_session_id="pbs_forced_self"),
                _workflow_run_model(
                    workflow_run_id="wr_forced_prior",
                    browser_session_id="pbs_forced_prior",
                    sequential_key="cred_a",
                    queued_at=prior_queued_at,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_forced_self",
                    browser_session_id="pbs_forced_self",
                    sequential_key="cred_a",
                    queued_at=self_queued_at,
                ),
            ]
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_forced_self")

    assert blocker is not None
    assert blocker.workflow_run_id == "wr_forced_prior"


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_forced_whole_workflow_lane_includes_session_rows(
    sqlite_db: AgentDB,
) -> None:
    prior_queued_at = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    self_queued_at = datetime(2026, 7, 4, 12, 1, tzinfo=timezone.utc)

    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _persistent_browser_session_model(persistent_browser_session_id="pbs_prior"),
                _persistent_browser_session_model(persistent_browser_session_id="pbs_self"),
                _workflow_run_model(
                    workflow_run_id="wr_prior",
                    browser_session_id="pbs_prior",
                    queued_at=prior_queued_at,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_self",
                    browser_session_id="pbs_self",
                    queued_at=self_queued_at,
                ),
            ]
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_self")

    assert blocker is not None
    assert blocker.workflow_run_id == "wr_prior"


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_scans_earlier_active_same_key() -> None:
    """SKY-10799: the sequential gate scans ALL earlier-queued same-key runs still in flight
    (queued/running/paused) — not a single depends_on edge — so it holds under a forest-shaped
    graph or a canceled predecessor. Earlier = (queued_at, id) strictly before self; queued_at
    is stamped under the submit lock, so it is the true queue order even when creation order
    diverges from submission order."""
    fake_run = MagicMock()
    fake_run.workflow_run_id = "wr_self"
    fake_run.organization_id = "o_test"
    fake_run.workflow_permanent_id = "wpid_test"
    fake_run.sequential_credential_id = None
    fake_run.sequential_key = "cred_test-sequential-key"
    fake_run.browser_session_id = None
    fake_run.browser_address = None
    fake_run.created_at = datetime(2026, 6, 8, 18, 53, 48, tzinfo=timezone.utc)
    fake_run.queued_at = datetime(2026, 6, 8, 18, 53, 50, tzinfo=timezone.utc)

    calls: list[Any] = []

    async def _scalars(query: Any) -> Any:
        calls.append(query)
        # 1st query loads the run itself; 2nd is the gate scan we assert on.
        return _Result(fake_run) if len(calls) == 1 else _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    blocker = await repo.get_blocking_sequential_workflow_run("wr_self")
    assert blocker is None

    gate_query = calls[1]
    where_clause = _where_clause_sql(gate_query)
    rendered = str(gate_query.compile(compile_kwargs={"literal_binds": True}))

    # scoped to the same wpid + key, ignoring browser-session runs
    assert "workflow_runs.workflow_permanent_id = 'wpid_test'" in where_clause
    assert "workflow_runs.sequential_key = 'cred_test-sequential-key'" in where_clause
    assert "workflow_runs.browser_session_id IS NULL" in where_clause
    assert "workflow_runs.organization_id = 'o_test'" in where_clause
    # only genuinely in-flight runs block; `created` and terminal statuses do not
    assert "'queued'" in where_clause and "'running'" in where_clause and "'paused'" in where_clause
    assert "'created'" not in where_clause and "'completed'" not in where_clause
    # earlier-queued by (queued_at, id), never created_at, and FIFO order so the
    # earliest blocker surfaces; unqueued rows can't block
    assert "workflow_runs.queued_at IS NOT NULL" in where_clause
    assert "workflow_runs.queued_at <" in where_clause
    assert "workflow_runs.created_at" not in where_clause
    assert "workflow_runs.workflow_run_id < 'wr_self'" in where_clause
    assert "ORDER BY workflow_runs.queued_at ASC" in rendered


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_prefers_browser_session_lane() -> None:
    """The gate's lane resolution must mirror enqueue priority (browser_session_id >
    browser_address > sequential_key): a non-debug run carrying both a browser session and a
    sequential_key chains on the session lane at enqueue, so the gate must scan that
    same lane or it can miss its actual blocker."""
    fake_run = MagicMock()
    fake_run.workflow_run_id = "wr_self"
    fake_run.organization_id = "o_test"
    fake_run.workflow_permanent_id = "wpid_test"
    fake_run.sequential_credential_id = None
    fake_run.sequential_key = "cred_test-sequential-key"
    fake_run.browser_session_id = "pbs_test"
    fake_run.debug_session_id = None
    fake_run.browser_address = None
    fake_run.created_at = datetime(2026, 6, 8, 18, 53, 48, tzinfo=timezone.utc)
    fake_run.queued_at = datetime(2026, 6, 8, 18, 53, 50, tzinfo=timezone.utc)

    calls: list[Any] = []
    persistent_browser_session = MagicMock()
    persistent_browser_session.runnable_type = "user_browser_session"

    async def _scalars(query: Any) -> Any:
        calls.append(query)
        if len(calls) == 1:
            return _Result(fake_run)
        if len(calls) == 2:
            return _Result(persistent_browser_session)
        return _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_blocking_sequential_workflow_run("wr_self")

    where_clause = _where_clause_sql(calls[2])
    assert "workflow_runs.browser_session_id = 'pbs_test'" in where_clause
    assert "sequential_key" not in where_clause


@pytest.mark.asyncio
async def test_get_blocking_sequential_workflow_run_debug_session_uses_key_lane() -> None:
    """A debug-session run carries a browser_session_id but enqueue keeps it out of the
    browser-session lane (is_browser_session_workflow requires not debug_session_id). The gate
    must do the same, or the debug run scans only its own session and misses an earlier same-key
    run — the SKY-10799 regression."""
    fake_run = MagicMock()
    fake_run.workflow_run_id = "wr_self"
    fake_run.organization_id = "o_test"
    fake_run.workflow_permanent_id = "wpid_test"
    fake_run.sequential_credential_id = None
    fake_run.sequential_key = "cred_test-sequential-key"
    fake_run.browser_session_id = "pbs_test"
    fake_run.debug_session_id = "dbg_test"
    fake_run.browser_address = None
    fake_run.created_at = datetime(2026, 6, 8, 18, 53, 48, tzinfo=timezone.utc)
    fake_run.queued_at = datetime(2026, 6, 8, 18, 53, 50, tzinfo=timezone.utc)

    calls: list[Any] = []

    async def _scalars(query: Any) -> Any:
        calls.append(query)
        return _Result(fake_run) if len(calls) == 1 else _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)

    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_blocking_sequential_workflow_run("wr_self")

    where_clause = _where_clause_sql(calls[1])
    assert "workflow_runs.sequential_key = 'cred_test-sequential-key'" in where_clause
    assert "workflow_runs.browser_session_id IS NULL" in where_clause
    assert "workflow_runs.browser_session_id = 'pbs_test'" not in where_clause


@pytest.mark.asyncio
async def test_get_last_queued_workflow_run_admits_credential_composed_session_row(sqlite_db: AgentDB) -> None:
    # A credential-composed predecessor carries a browser_session_id but is a K-lane occupant by the
    # PR's gate contract, so the legacy K lookup must admit it. A plain (non-credential) session+K row
    # stays excluded even though it is later-modified — legacy session-lane priority is preserved.
    early = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_cred_session",
                    browser_session_id="pbs_cred",
                    sequential_key="K",
                    sequential_credential_id="cred_a",
                    queued_at=early,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_plain_session",
                    browser_session_id="pbs_plain",
                    sequential_key="K",
                    queued_at=late,
                ),
            ]
        )
        await session.commit()

    result = await sqlite_db.workflow_runs.get_last_queued_workflow_run("wpid_test", "org_test", "K")

    assert result is not None
    assert result.workflow_run_id == "wr_cred_session"


@pytest.mark.asyncio
async def test_composed_lane_predecessor_queries_limit_each_index_candidate() -> None:
    captured: list[Any] = []

    async def _scalars(query: Any) -> Any:
        captured.append(query)
        return _Result(None)

    session = MagicMock()
    session.scalars = AsyncMock(side_effect=_scalars)
    repo = WorkflowRunsRepository(session_factory=lambda: _SessionContext(session), debug_enabled=False)

    await repo.get_last_queued_workflow_run("wpid_test", "o_test", "K")
    await repo.get_last_running_workflow_run("wpid_test", "o_test", "K")

    assert len(captured) == 4
    for query in captured:
        rendered = str(query.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 1" in rendered


@pytest.mark.asyncio
async def test_get_last_queued_workflow_run_can_keep_legacy_batch_predecessor(sqlite_db: AgentDB) -> None:
    early = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        legacy = _workflow_run_model(
            workflow_run_id="wr_batch_legacy",
            sequential_key="K",
            queued_at=early,
        )
        credential = _workflow_run_model(
            workflow_run_id="wr_credential_newer",
            browser_session_id="pbs_cred",
            sequential_key="K",
            sequential_credential_id="cred_a",
            queued_at=late,
        )
        legacy.modified_at = early
        credential.modified_at = late
        session.add_all([legacy, credential])
        await session.commit()

    cross_lane = await sqlite_db.workflow_runs.get_last_queued_workflow_run("wpid_test", "org_test", "K")
    legacy_only = await sqlite_db.workflow_runs.get_last_queued_workflow_run(
        "wpid_test",
        "org_test",
        "K",
        include_credential_composed_rows=False,
    )

    assert cross_lane is not None
    assert cross_lane.workflow_run_id == "wr_credential_newer"
    assert legacy_only is not None
    assert legacy_only.workflow_run_id == "wr_batch_legacy"


@pytest.mark.asyncio
async def test_get_last_running_workflow_run_admits_credential_composed_session_row(sqlite_db: AgentDB) -> None:
    early = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        cred = _workflow_run_model(
            workflow_run_id="wr_cred_session",
            browser_session_id="pbs_cred",
            sequential_key="K",
            sequential_credential_id="cred_a",
            status=WorkflowRunStatus.running.value,
            queued_at=early,
        )
        cred.started_at = early
        plain = _workflow_run_model(
            workflow_run_id="wr_plain_session",
            browser_session_id="pbs_plain",
            sequential_key="K",
            status=WorkflowRunStatus.running.value,
            queued_at=late,
        )
        plain.started_at = late
        session.add_all([cred, plain])
        await session.commit()

    result = await sqlite_db.workflow_runs.get_last_running_workflow_run("wpid_test", "org_test", "K")

    assert result is not None
    assert result.workflow_run_id == "wr_cred_session"


@pytest.mark.asyncio
@pytest.mark.parametrize("pred_status", [WorkflowRunStatus.queued.value, WorkflowRunStatus.running.value])
async def test_gate_noncredential_key_blocks_on_credential_composed_session_predecessor(
    sqlite_db: AgentDB, pred_status: str
) -> None:
    # A later non-credential K run must see and wait on an earlier credential+session+K occupant,
    # for both queued and running predecessors — the gate's non-credential K branch admits it.
    early = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_pred",
                    browser_session_id="pbs_pred",
                    sequential_key="K",
                    sequential_credential_id="cred_a",
                    status=pred_status,
                    queued_at=early,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_self",
                    sequential_key="K",
                    queued_at=late,
                ),
            ]
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_self")

    assert blocker is not None
    assert blocker.workflow_run_id == "wr_pred"


@pytest.mark.asyncio
async def test_gate_noncredential_whole_workflow_blocks_on_credential_composed_session_predecessor(
    sqlite_db: AgentDB,
) -> None:
    # A later non-credential whole-workflow run must see and wait on an earlier credential+session
    # occupant of the same wpid — the gate's non-credential whole-workflow branch admits it.
    early = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_pred",
                    browser_session_id="pbs_pred",
                    sequential_credential_id="cred_a",
                    queued_at=early,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_self",
                    queued_at=late,
                ),
            ]
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_self")

    assert blocker is not None
    assert blocker.workflow_run_id == "wr_pred"


@pytest.mark.asyncio
async def test_gate_noncredential_key_ignores_plain_session_predecessor(sqlite_db: AgentDB) -> None:
    # Legacy-priority regression pin (stays green before and after): a plain non-credential session+K
    # row must remain invisible to a later non-credential K run — the OR admits only credential rows.
    early = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _workflow_run_model(
                    workflow_run_id="wr_plain_session",
                    browser_session_id="pbs_plain",
                    sequential_key="K",
                    queued_at=early,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_self",
                    sequential_key="K",
                    queued_at=late,
                ),
            ]
        )
        await session.commit()

    blocker = await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_self")

    assert blocker is None


def _capture_sql(engine: AsyncEngine) -> tuple[list[str], Any]:
    """Collect every SQL statement emitted on the engine so a test can assert query shape."""
    statements: list[str] = []

    def _on_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:  # noqa: ANN401
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
    return statements, _on_execute


# The de-indexing OR the legacy partial index (ix_workflow_runs_sequential_key_lookup, partial on
# `browser_session_id IS NULL`) can no longer serve. A regex, not a substring, so column-order or
# alias changes in the compiled SQL cannot silently let the OR slip back in.
_DEINDEXING_OR = re.compile(
    r"browser_session_id IS NULL\s+OR\s+\S*sequential_credential_id IS NOT NULL",
    re.IGNORECASE,
)


@pytest.mark.asyncio
async def test_get_last_queued_workflow_run_keeps_legacy_lane_index_eligible(
    sqlite_db: AgentDB, sqlite_engine: AsyncEngine
) -> None:
    # The non-session lookup must scan the legacy lane with a standalone `browser_session_id IS
    # NULL` (index-eligible via ix_workflow_runs_sequential_key_lookup), never ORed with
    # `sequential_credential_id IS NOT NULL` — that OR de-indexes the high-write lookup.
    statements, listener = _capture_sql(sqlite_engine)
    try:
        await sqlite_db.workflow_runs.get_last_queued_workflow_run("wpid_test", "org_test", "K")
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", listener)

    lookups = [s for s in statements if "FROM workflow_runs" in s and "browser_session_id IS NULL" in s]
    assert lookups, "expected a legacy-lane lookup that filters browser_session_id IS NULL"
    for statement in statements:
        assert not _DEINDEXING_OR.search(statement), f"legacy lane de-indexed by widened OR: {statement}"


@pytest.mark.asyncio
async def test_get_last_running_workflow_run_keeps_legacy_lane_index_eligible(
    sqlite_db: AgentDB, sqlite_engine: AsyncEngine
) -> None:
    statements, listener = _capture_sql(sqlite_engine)
    try:
        await sqlite_db.workflow_runs.get_last_running_workflow_run("wpid_test", "org_test", "K")
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", listener)

    lookups = [s for s in statements if "FROM workflow_runs" in s and "browser_session_id IS NULL" in s]
    assert lookups, "expected a legacy-lane lookup that filters browser_session_id IS NULL"
    for statement in statements:
        assert not _DEINDEXING_OR.search(statement), f"legacy lane de-indexed by widened OR: {statement}"


@pytest.mark.asyncio
async def test_gate_noncredential_key_lane_keeps_index_eligible_shape(
    sqlite_db: AgentDB, sqlite_engine: AsyncEngine
) -> None:
    # The gate's non-credential sequential_key branch admits credential-composed predecessors, but must
    # do so without ORing the credential predicate into the keyed lane scan (which de-indexes it).
    async with sqlite_db.Session() as session:
        session.add(
            _workflow_run_model(
                workflow_run_id="wr_self",
                sequential_key="K",
                queued_at=datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    statements, listener = _capture_sql(sqlite_engine)
    try:
        await sqlite_db.workflow_runs.get_blocking_sequential_workflow_run("wr_self")
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", listener)

    for statement in statements:
        assert not _DEINDEXING_OR.search(statement), f"gate keyed lane de-indexed by widened OR: {statement}"


@pytest.mark.asyncio
async def test_wake_candidates_share_credential_lane_by_gate_ticket(sqlite_db: AgentDB) -> None:
    # Excluded peers are all same-credential so any predicate regression (org scope, queued-only
    # status, queued-after ordering) would surface as an extra or misordered candidate.
    early_at = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)
    done_at = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    next_at = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
    third_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    fourth_at = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _credential_run("wr_fan_done", done_at, "cred_f", status=WorkflowRunStatus.completed.value),
                _credential_run("wr_fan_before", early_at, "cred_f"),
                _credential_run("wr_fan_other_org", next_at, "cred_f", organization_id="org_other"),
                _credential_run("wr_fan_running", next_at, "cred_f", status=WorkflowRunStatus.running.value),
                _credential_run("wr_fan_next", next_at, "cred_f"),
                _credential_run("wr_fan_third", third_at, "cred_f"),
                _credential_run("wr_fan_fourth", fourth_at, "cred_f"),
            ]
        )
        await session.commit()

    waiters = await sqlite_db.workflow_runs.get_queued_runs_sharing_sequential_lanes("wr_fan_done")

    assert [w.workflow_run_id for w in waiters] == ["wr_fan_next", "wr_fan_third"]


@pytest.mark.asyncio
async def test_wake_candidates_cover_run_sequentially_whole_workflow(sqlite_db: AgentDB) -> None:
    # A different-credential waiter in a run_sequentially workflow has no depends_on edge and no
    # shared credential/key, so only the whole-workflow lane can wake it; a plain session row
    # fails the lane's admission condition.
    done_at = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    next_at = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add(
            WorkflowModel(
                workflow_id="wf_seq_wake",
                workflow_permanent_id="wpid_ws_wake",
                title="seq",
                workflow_definition={},
                run_sequentially=True,
            )
        )
        session.add_all(
            [
                _credential_run(
                    "wr_wsw_done",
                    done_at,
                    "cred_p",
                    workflow_permanent_id="wpid_ws_wake",
                    workflow_id="wf_seq_wake",
                    status=WorkflowRunStatus.completed.value,
                ),
                _credential_run(
                    "wr_wsw_next", next_at, "cred_q", workflow_permanent_id="wpid_ws_wake", workflow_id="wf_seq_wake"
                ),
                _workflow_run_model(
                    workflow_run_id="wr_wsw_plain_session",
                    queued_at=next_at,
                    workflow_permanent_id="wpid_ws_wake",
                    workflow_id="wf_seq_wake",
                    browser_session_id="pbs_wsw_plain",
                ),
            ]
        )
        await session.commit()

    waiters = await sqlite_db.workflow_runs.get_queued_runs_sharing_sequential_lanes("wr_wsw_done")

    assert [w.workflow_run_id for w in waiters] == ["wr_wsw_next"]


@pytest.mark.asyncio
async def test_wake_candidates_merge_lanes_and_keep_the_earliest(sqlite_db: AgentDB) -> None:
    # The key-lane waiter is queued earlier than the credential-lane waiter, so the cross-lane
    # merge must return it first; the later credential waiter survives because the merge never
    # truncates across lanes (only the per-lane limit applies).
    done_at = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    key_at = datetime(2026, 8, 4, 10, 30, tzinfo=timezone.utc)
    cred_at = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
    late_at = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _credential_run(
                    "wr_merge_done",
                    done_at,
                    "cred_m",
                    workflow_permanent_id="wpid_merge",
                    sequential_key="KM",
                    status=WorkflowRunStatus.completed.value,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_merge_key",
                    queued_at=key_at,
                    workflow_permanent_id="wpid_merge",
                    sequential_key="KM",
                ),
                _credential_run("wr_merge_cred", cred_at, "cred_m", workflow_permanent_id="wpid_other_merge"),
                _credential_run("wr_merge_late", late_at, "cred_m", workflow_permanent_id="wpid_other_merge"),
            ]
        )
        await session.commit()

    waiters = await sqlite_db.workflow_runs.get_queued_runs_sharing_sequential_lanes("wr_merge_done")

    assert [w.workflow_run_id for w in waiters] == ["wr_merge_key", "wr_merge_cred", "wr_merge_late"]


@pytest.mark.asyncio
async def test_wake_candidates_signal_every_lane_head(sqlite_db: AgentDB) -> None:
    # A predecessor in three lanes (credential, browser session, workflow+key) with three distinct
    # lane heads: a global cap of 2 would drop the latest-queued lane's head, which has no
    # depends_on edge and would sleep until the fallback poll instead of starting.
    done_at = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    key_at = datetime(2026, 8, 4, 10, 30, tzinfo=timezone.utc)
    cred_at = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
    sess_at = datetime(2026, 8, 4, 11, 30, tzinfo=timezone.utc)
    async with sqlite_db.Session() as session:
        session.add_all(
            [
                _credential_run(
                    "wr_tri_done",
                    done_at,
                    "cred_t",
                    browser_session_id="pbs_shared",
                    workflow_permanent_id="wpid_tri",
                    sequential_key="KT",
                    status=WorkflowRunStatus.completed.value,
                ),
                _workflow_run_model(
                    workflow_run_id="wr_tri_key",
                    queued_at=key_at,
                    workflow_permanent_id="wpid_tri",
                    sequential_key="KT",
                ),
                _credential_run("wr_tri_cred", cred_at, "cred_t", workflow_permanent_id="wpid_other_tri"),
                _workflow_run_model(
                    workflow_run_id="wr_tri_sess",
                    queued_at=sess_at,
                    browser_session_id="pbs_shared",
                    workflow_permanent_id="wpid_other_tri2",
                ),
            ]
        )
        await session.commit()

    waiters = await sqlite_db.workflow_runs.get_queued_runs_sharing_sequential_lanes("wr_tri_done")

    assert [w.workflow_run_id for w in waiters] == ["wr_tri_key", "wr_tri_cred", "wr_tri_sess"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "stale",
        "young",
        "boundary",
        "running_run",
        "terminal_run",
        "unstarted_run",
        "queued_attempt",
        "unstarted_attempt",
        "decided_attempt",
        "wrong_organization",
        "wrong_attempt",
        "mismatched_organization",
        "missing_run",
    ],
)
async def test_stale_dispatch_claim_release_is_conditional_and_reclaimable(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    monkeypatch.setattr(attempts_repository_module, "naive_utc_now", lambda: now)
    cutoff = now - timedelta(seconds=max(600, LEASE_TAKEOVER_SECONDS))
    old = cutoff - timedelta(seconds=1)
    run = _workflow_run_model(workflow_run_id="wr_dispatch", queued_at=old)
    run.started_at = old
    run.modified_at = old
    attempt = WorkflowRunAttemptModel(
        workflow_run_id=run.workflow_run_id,
        organization_id=run.organization_id,
        attempt_number=2,
        status="running",
        started_at=old,
        modified_at=old,
    )
    if scenario in {"young", "boundary"}:
        attempt.modified_at = cutoff + timedelta(seconds=1 if scenario == "young" else 0)
    elif scenario == "running_run":
        run.status = "running"
    elif scenario == "terminal_run":
        run.status = "failed"
    elif scenario == "unstarted_run":
        run.started_at = None
    elif scenario == "queued_attempt":
        attempt.status = "queued"
    elif scenario == "unstarted_attempt":
        attempt.started_at = None
    elif scenario == "decided_attempt":
        attempt.retry_decision = "final"
    elif scenario == "mismatched_organization":
        attempt.organization_id = "org_other"
    async with sqlite_db.Session() as session:
        if scenario != "missing_run":
            session.add(run)
        session.add(attempt)
        await session.commit()

    async def snapshot() -> tuple[object, ...]:
        async with sqlite_db.Session() as session:
            current_run = await session.get(WorkflowRunModel, "wr_dispatch")
            current_attempt = await session.get(WorkflowRunAttemptModel, ("wr_dispatch", 2))
            assert current_attempt is not None
            return (
                (current_run.status, current_run.started_at, current_run.modified_at) if current_run else None,
                current_attempt.status,
                current_attempt.started_at,
                current_attempt.modified_at,
                current_attempt.retry_decision,
            )

    before = await snapshot()
    candidates = await sqlite_db.workflow_run_attempts.list_stale_dispatch_claims(cutoff.replace(tzinfo=UTC))
    assert bool(candidates) == (scenario in {"stale", "wrong_organization", "wrong_attempt"})
    released = await sqlite_db.workflow_run_attempts.release_stale_dispatch_claim(
        "wr_dispatch",
        "org_other" if scenario == "wrong_organization" else "org_test",
        3 if scenario == "wrong_attempt" else 2,
        stale_before=cutoff.replace(tzinfo=UTC),
    )
    assert released is (scenario == "stale")
    if not released:
        assert await snapshot() == before
        return
    assert await snapshot() == (("queued", None, now), "queued", None, now, None)
    assert not await sqlite_db.workflow_run_attempts.release_stale_dispatch_claim(
        "wr_dispatch", "org_test", 2, stale_before=cutoff
    )
    stamp = await sqlite_db.workflow_run_attempts.claim_prepared_attempt_execution("wr_dispatch", "org_test", 2)
    assert stamp == now
    assert await snapshot() == (("queued", now, now), "running", now, now, None)
    assert await sqlite_db.workflow_run_attempts.claim_prepared_attempt_execution("wr_dispatch", "org_test", 2) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", [WorkflowRunStatus.canceled.value, WorkflowRunStatus.queued.value])
async def test_recovery_selects_never_started_attempts_of_terminal_runs(sqlite_db: AgentDB, run_status: str) -> None:
    # A cancel that lands before mark_attempt_started leaves a created row with no started_at; the
    # terminal run still owes its cleanup and webhook, so recovery must see the row.
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1)
    run = _workflow_run_model(workflow_run_id="wr_never_started", queued_at=old, status=run_status)
    attempt = WorkflowRunAttemptModel(
        workflow_run_id=run.workflow_run_id,
        organization_id=run.organization_id,
        attempt_number=1,
        status="created",
        modified_at=old,
    )
    async with sqlite_db.Session() as session:
        session.add_all([run, attempt])
        await session.commit()

    candidates = await sqlite_db.workflow_run_attempts.list_attempts_needing_recovery(
        datetime.now(UTC) - timedelta(seconds=LEASE_TAKEOVER_SECONDS)
    )

    expected = ["wr_never_started"] if run_status == WorkflowRunStatus.canceled.value else []
    assert [candidate.workflow_run_id for candidate in candidates] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("started_after_selection", [False, True])
async def test_prepared_recovery_failure_is_conditional_on_unstarted_queued_run(
    sqlite_db: AgentDB, started_after_selection: bool
) -> None:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1)
    run = _workflow_run_model(workflow_run_id="wr_prepared_recovery", queued_at=old)
    attempt = WorkflowRunAttemptModel(
        workflow_run_id=run.workflow_run_id,
        organization_id=run.organization_id,
        attempt_number=2,
        status="queued",
        modified_at=old,
    )
    task_run = _task_run_model(
        run_id=run.workflow_run_id,
        created_at=old.replace(tzinfo=UTC),
        workflow_permanent_id="wpid_test",
        status=WorkflowRunStatus.queued.value,
    )
    async with sqlite_db.Session() as session:
        session.add_all([run, attempt, task_run])
        await session.commit()
    candidates = await sqlite_db.workflow_run_attempts.list_attempts_needing_recovery(
        datetime.now(UTC) - timedelta(seconds=LEASE_TAKEOVER_SECONDS)
    )
    assert [candidate.workflow_run_id for candidate in candidates] == ["wr_prepared_recovery"]
    if started_after_selection:
        async with sqlite_db.Session() as session:
            current = await session.get(WorkflowRunModel, "wr_prepared_recovery")
            assert current is not None
            # Keep queued to prove started_at independently fences the failure UPDATE.
            current.started_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
    claimed = await sqlite_db.workflow_run_attempts.fail_prepared_workflow_run(
        "wr_prepared_recovery", "org_test", 2, "execution absent"
    )
    assert claimed is not started_after_selection
    assert not await sqlite_db.workflow_run_attempts.fail_prepared_workflow_run(
        "wr_prepared_recovery", "org_test", 2, "execution absent"
    )
    async with sqlite_db.Session() as session:
        current = await session.get(WorkflowRunModel, "wr_prepared_recovery")
        assert current is not None
        assert current.status == ("queued" if started_after_selection else "failed")
        mirrored = await session.get(TaskRunModel, "tr_wr_prepared_recovery")
        assert mirrored is not None
        # The runs list reads finished_at from this mirror; the abandon must write it in the same transaction.
        assert (mirrored.status, mirrored.finished_at is not None) == (
            ("queued", False) if started_after_selection else ("failed", True)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("labels_only", [False, True])
async def test_oss_run_with_unpersisted_block_outputs_abandons_retry(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch, labels_only: bool
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    run_model = _workflow_run_model(workflow_run_id="wr_scoped_inputs", queued_at=now, status="failed")
    async with sqlite_db.Session() as session:
        session.add(run_model)
        await session.commit()
    await sqlite_db.workflow_run_attempts.create_attempt("wr_scoped_inputs", "org_test", 1, "queued")
    run = await sqlite_db.workflow_runs.get_workflow_run("wr_scoped_inputs", organization_id="org_test")
    assert run is not None
    workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(
            retry_policy=WorkflowRetryPolicy.model_validate({"retry_on": [{"status": "failed"}]})
        )
    )
    svc = workflow_service_module.WorkflowService()
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", svc)
    monkeypatch.setattr(svc, "get_workflow", AsyncMock(return_value=workflow))
    monkeypatch.setattr(svc, "get_workflow_by_workflow_run_id", AsyncMock(return_value=workflow))
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_block_scoped_workflow_run", AsyncMock(return_value=False))
    assert await is_retry_eligible_run(run, "org_test")
    execute = AsyncMock(return_value=run)
    monkeypatch.setattr(svc, "execute_workflow", execute)
    release = AsyncMock(return_value="released")
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", release)
    prepare = AsyncMock()
    monkeypatch.setattr(workflow_service_module, "prepare_next_attempt_result", prepare)

    await svc.execute_workflow_with_retries(
        workflow_run_id=run.workflow_run_id,
        api_key=None,
        organization=SimpleNamespace(organization_id="org_test"),
        block_labels=["selected"] if labels_only else None,
        block_outputs=None if labels_only else {"caller_output": {"value": "original"}},
    )

    execute.assert_awaited_once()
    assert execute.await_args.kwargs["block_labels"] == (["selected"] if labels_only else None)
    assert execute.await_args.kwargs["block_outputs"] == (
        None if labels_only else {"caller_output": {"value": "original"}}
    )
    prepare.assert_not_awaited()
    attempts = await sqlite_db.workflow_run_attempts.get_attempts(run.workflow_run_id)
    assert len(attempts) == 1
    assert attempts[0].retry_decision == "abandoned"
    assert attempts[0].decision_reason == "unrecoverable_block_outputs"
    assert attempts[0].finished_at is not None
    release.assert_awaited_once()
    assert release.await_args.args[1].retry is False


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["running", "unknown", "absent", "terminal", "error"])
@pytest.mark.parametrize("prepared_during_check", [False, True])
async def test_stale_retry_recovery_checks_ownership_and_decision_cas(
    sqlite_db: AgentDB, monkeypatch: pytest.MonkeyPatch, ownership: str, prepared_during_check: bool
) -> None:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1)
    run = _workflow_run_model(workflow_run_id="wr_stale_owned", queued_at=old, status="failed")
    attempt = WorkflowRunAttemptModel(
        workflow_run_id="wr_stale_owned",
        organization_id=run.organization_id,
        attempt_number=1,
        status="failed",
        retry_decision="retry",
        next_attempt_at=old,
        finished_at=old,
    )
    async with sqlite_db.Session() as session:
        session.add_all([run, attempt])
        await session.commit()
    monkeypatch.setattr(app, "DATABASE", sqlite_db)
    svc = workflow_service_module.WorkflowService()
    monkeypatch.setattr(app, "WORKFLOW_SERVICE", svc)
    release = AsyncMock()
    monkeypatch.setattr(svc, "_run_terminal_side_effects_with_retries", release)
    monkeypatch.setattr(workflow_service_module, "_get_recovery_api_key", AsyncMock(return_value=None))

    async def execution_status(_run: Any) -> str:
        if prepared_during_check:
            async with sqlite_db.Session() as session:
                current = await session.get(WorkflowRunAttemptModel, ("wr_stale_owned", 1))
                current.next_attempt_prepared_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
        if ownership == "error":
            raise RuntimeError("description unavailable")
        return ownership

    describe = AsyncMock(side_effect=execution_status)
    monkeypatch.setattr(app.AGENT_FUNCTION, "get_workflow_run_execution_status", describe)
    await workflow_service_module.recover_pending_workflow_attempts(release_only=True)
    current = (await sqlite_db.workflow_run_attempts.get_attempts("wr_stale_owned"))[0]
    won = ownership in {"absent", "terminal"} and not prepared_during_check
    assert current.retry_decision == ("abandoned" if won else "retry")
    assert release.await_count == int(won)
    describe.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_progress_fences_stale_writers_and_separates_release_kinds(sqlite_db: AgentDB) -> None:
    repo = sqlite_db.workflow_run_attempts
    await repo.create_attempt("wr_progress", "org_test", 1, "failed")
    async with sqlite_db.Session() as session:
        row = await session.get(WorkflowRunAttemptModel, ("wr_progress", 1))
        row.retry_decision = "retry"
        await session.commit()
    first_token = await repo.claim_attempt_side_effects("wr_progress", 1, kind="interim")
    assert first_token is not None
    interim: TerminalSideEffectCheckpoint = {
        "completed_effects": ["interim workflow webhook"],
        "final_hook_invoked": False,
    }
    assert await repo.save_side_effect_progress(
        "wr_progress", 1, kind="interim", expected_claim_at=first_token, progress=interim
    )
    second_token = await repo.claim_attempt_side_effects(
        "wr_progress",
        1,
        kind="interim",
        expected_claim_at=first_token,
        stale_before=first_token + timedelta(seconds=1),
    )
    assert second_token is not None and second_token != first_token
    assert not await repo.save_side_effect_progress(
        "wr_progress",
        1,
        kind="interim",
        expected_claim_at=first_token,
        progress={"completed_effects": [], "final_hook_invoked": False},
    )
    assert await repo.revoke_or_abandon_attempt("wr_progress", 1, "revoked", "cancel") is not None
    final_token = await repo.claim_attempt_side_effects("wr_progress", 1, kind="final")
    assert final_token is not None
    final: TerminalSideEffectCheckpoint = {"completed_effects": ["workflow final hook"], "final_hook_invoked": True}
    assert await repo.save_side_effect_progress(
        "wr_progress", 1, kind="final", expected_claim_at=final_token, progress=final
    )
    assert not await repo.save_side_effect_progress(
        "wr_progress", 1, kind="interim", expected_claim_at=second_token, progress=interim
    )
    row = (await repo.get_attempts("wr_progress"))[0]
    assert row.interim_side_effects_progress == interim
    assert row.final_side_effects_progress == final


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated_count, unrelated_parent_links", [(0, False), (1000, False), (1000, True)])
async def test_attempt_artifact_ancestry_ignores_unrelated_history(
    sqlite_db: AgentDB, sqlite_engine: AsyncEngine, unrelated_count: int, unrelated_parent_links: bool
) -> None:
    start = datetime(2026, 7, 22, 12, tzinfo=UTC)
    successor = start + timedelta(minutes=10)
    root = "wr_ancestry"
    runs = {
        name: _workflow_run_model(workflow_run_id=name, queued_at=start)
        for name in (root, "wr_block_child", "wr_parent_child", "wr_grandchild", "wr_next_child")
    }
    runs["wr_parent_child"].parent_workflow_run_id = root
    runs["wr_grandchild"].parent_workflow_run_id = "wr_block_child"
    runs["wr_next_child"].parent_workflow_run_id = root
    async with sqlite_db.Session() as session:
        session.add(OrganizationModel(organization_id="org_test", organization_name="Test Organization"))
        await session.flush()
        session.add_all(list(runs.values()))
        for name, number in [("wr_block_child", 1), ("wr_next_child", 2)]:
            session.add(
                WorkflowRunBlockModel(
                    workflow_run_block_id=f"spawn_{name}",
                    workflow_run_id=root,
                    block_workflow_run_id=name,
                    organization_id="org_test",
                    block_type="task_v2",
                    status="completed",
                    attempt_number=number,
                )
            )
        await session.flush()
        for name in runs:
            session.add(
                TaskModel(
                    task_id=f"task_{name}",
                    workflow_run_id=name,
                    organization_id="org_test",
                    status="completed",
                    url="https://example.com",
                    attempt_number=7,
                )
            )
        await session.flush()
        for name in list(runs)[1:]:
            session.add(
                ArtifactModel(
                    artifact_id=f"artifact_{name}",
                    organization_id="org_test",
                    run_id=root,
                    workflow_run_id=name,
                    task_id=f"task_{name}",
                    artifact_type=ArtifactType.DOWNLOAD,
                    uri=f"https://example.com/{name}",
                    created_at=start if name == "wr_parent_child" else successor + timedelta(minutes=1),
                )
            )
        for name, when in [("in_window", start), ("outside_window", successor)]:
            session.add(
                ArtifactModel(
                    artifact_id=name,
                    organization_id="org_test",
                    run_id=root,
                    artifact_type=ArtifactType.DOWNLOAD,
                    uri=f"https://example.com/{name}",
                    created_at=when,
                )
            )
        for i in range(unrelated_count):
            unrelated_id = f"wr_unrelated_{i}"
            unrelated_run = _workflow_run_model(workflow_run_id=unrelated_id, queued_at=start)
            unrelated_run.parent_workflow_run_id = f"wr_unrelated_{i - 1}" if i and unrelated_parent_links else None
            session.add(unrelated_run)
            session.add(
                WorkflowRunBlockModel(
                    workflow_run_block_id=f"unrelated_block_{i}",
                    workflow_run_id=f"wr_unrelated_{i - 1}" if i else unrelated_id,
                    block_workflow_run_id=unrelated_id if i else None,
                    organization_id="org_test",
                    block_type="task_v2",
                    status="completed",
                    attempt_number=2,
                )
            )
        await session.commit()

    async with sqlite_engine.begin() as connection:
        await connection.exec_driver_sql("ANALYZE")
    statements, listener = _capture_sql(sqlite_engine)
    plans: list[str] = []

    def explain(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        if statement.startswith("WITH RECURSIVE"):
            plan_cursor = conn.connection.cursor()
            try:
                plan_cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
                plans.extend(row[3] for row in plan_cursor.fetchall())
            finally:
                plan_cursor.close()

    event.listen(sqlite_engine.sync_engine, "before_cursor_execute", explain)
    try:
        first = await sqlite_db.workflow_runs.get_artifacts_for_attempt(root, "org_test", 1, start, successor)
        second = await sqlite_db.workflow_runs.get_artifacts_for_attempt(root, "org_test", 2, successor, None)
    finally:
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", listener)
        event.remove(sqlite_engine.sync_engine, "before_cursor_execute", explain)

    assert {artifact.artifact_id for artifact in first} == {
        "artifact_wr_block_child",
        "artifact_wr_parent_child",
        "artifact_wr_grandchild",
        "in_window",
    }
    assert {artifact.artifact_id for artifact in second} == {"artifact_wr_next_child", "outside_window"}
    assert len(statements) == 2
    if unrelated_count:
        assert any("parent_workflow_run_id=?" in plan for plan in plans), plans
        block_plans = [plan for plan in plans if "attempt_spawning_block" in plan]
        assert block_plans and all("organization_id=? AND workflow_run_id=?" in plan for plan in block_plans), plans
        assert not any(plan.startswith("SCAN attempt_child") for plan in plans), plans


@pytest.mark.asyncio
async def test_recovery_paginates_stale_undecided_terminal_runs(sqlite_db: AgentDB) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(seconds=LEASE_TAKEOVER_SECONDS)
    old = cutoff - timedelta(seconds=1)
    terminal_statuses = ["completed", "failed", "terminated", "canceled", "timed_out"]
    async with sqlite_db.Session() as session:
        for index, status in enumerate(terminal_statuses + ["running", "failed", "failed"]):
            run_id = f"wr_undecided_{index}"
            session.add(_workflow_run_model(workflow_run_id=run_id, queued_at=old, status=status))
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id=run_id,
                    organization_id="org_test",
                    attempt_number=1,
                    status="running",
                    started_at=None if index == 7 else old,
                    modified_at=now if index == 6 else old,
                )
            )
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id="wr_undecided_0",
                organization_id="org_test",
                attempt_number=2,
                status="running",
                started_at=old,
                modified_at=old,
            )
        )
        session.add(_workflow_run_model(workflow_run_id="wr_retry_decided", queued_at=old, status="failed"))
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id="wr_retry_decided",
                organization_id="org_test",
                attempt_number=1,
                status="failed",
                retry_decision="retry",
                next_attempt_at=old - timedelta(seconds=1),
                modified_at=now,
            )
        )
        await session.commit()

    recovered: list[tuple[str, int]] = []
    cursor = None
    for _ in range(5):
        page = await sqlite_db.workflow_run_attempts.list_attempts_needing_recovery(cutoff, cursor=cursor, limit=2)
        if not page:
            break
        recovered.extend((attempt.workflow_run_id, attempt.attempt_number) for attempt in page)
        last = page[-1]
        cursor = (last.next_attempt_at or last.modified_at, last.workflow_run_id, last.attempt_number)
    # Index 7 never started but its run is terminal, so recovery still owes it the terminal effects.
    assert recovered == [
        ("wr_retry_decided", 1),
        ("wr_undecided_0", 1),
        ("wr_undecided_0", 2),
        *((f"wr_undecided_{index}", 1) for index in range(1, 5)),
        ("wr_undecided_7", 1),
    ]
