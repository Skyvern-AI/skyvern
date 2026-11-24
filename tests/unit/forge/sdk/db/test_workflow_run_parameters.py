"""Tests for WorkflowRunsRepository.get_workflow_run_parameters against in-memory SQLite.

The method resolves each run parameter's definition. It used to issue one query per
parameter (sequential N+1); it now batch-fetches all definitions in a single query.
The load-bearing invariant is that a run referencing a soft-deleted parameter definition
still resolves — batch resolution must match on id only, without a deleted_at filter.
"""

from __future__ import annotations

import importlib.util
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.exceptions import OTPRequestAlreadyBoundError
from skyvern.forge.sdk.db.models import (
    OTPRequestAcquisitionModel,
    OTPRequestBindingModel,
    TaskModel,
    TOTPCodeModel,
    WorkflowRunModel,
)
from skyvern.forge.sdk.db.repositories.otp import _strict_identifier
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition, WorkflowRunStatus


def _wp(key: str, workflow_id: str, default_value: Any = None) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=f"wp_{key}",
        workflow_parameter_type=WorkflowParameterType.STRING,
        key=key,
        description=None,
        workflow_id=workflow_id,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


async def _seed(agent_db: AgentDB) -> dict[str, str]:
    org = await agent_db.organizations.create_organization(organization_name="Test Org", domain="wrp.test")
    workflow = await agent_db.workflows.create_workflow(
        title="Test Workflow",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org.organization_id,
    )
    return {"organization_id": org.organization_id, "workflow_id": workflow.workflow_id}


async def _reconcile(agent_db: AgentDB, ids: dict[str, str], params: list[WorkflowParameter]) -> None:
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
        workflow_definition=WorkflowDefinition(parameters=params, blocks=[]),
    )


@pytest.mark.asyncio
async def test_resolves_all_params_including_soft_deleted(agent_db: AgentDB) -> None:
    ids = await _seed(agent_db)
    workflow_id = ids["workflow_id"]

    await _reconcile(agent_db, ids, [_wp("live", workflow_id, "a"), _wp("gone", workflow_id, "b")])
    persisted = {p.key: p for p in await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)}

    # Soft-delete "gone" — its definition row survives and must still resolve by id.
    await _reconcile(agent_db, ids, [_wp("live", workflow_id, "a")])

    workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=workflow_id,
        organization_id=ids["organization_id"],
    )
    await agent_db.workflow_runs.create_workflow_run_parameters(
        workflow_run_id=workflow_run.workflow_run_id,
        workflow_parameter_values=[(persisted["live"], "live_val"), (persisted["gone"], "gone_val")],
    )

    results = await agent_db.workflow_runs.get_workflow_run_parameters(workflow_run.workflow_run_id)

    by_key = {wp.key: (wp, wrp) for wp, wrp in results}
    assert set(by_key) == {"live", "gone"}
    assert by_key["live"][1].value == "live_val"
    assert by_key["gone"][1].value == "gone_val"
    empty_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=workflow_id,
        organization_id=ids["organization_id"],
    )
    assert await agent_db.workflow_runs.get_workflow_run_parameters(empty_run.workflow_run_id) == []


@pytest.mark.asyncio
async def test_strict_otp_marker_persists_on_root_and_task_owners(
    org_scoped_db: tuple[AgentDB, Any, str],
    postgres_scratch_database: Engine,
) -> None:
    agent_db, db_engine, _ = org_scoped_db
    versions = Path(__file__).resolve().parents[5] / "alembic" / "versions"
    hot_revision = (versions / "2026_11_17_1500-9f6b4c2a8d71_add_strict_otp_claim_fields.py").read_text()
    schema_revisions = list(versions.glob("*_add_strict_otp_claim_schema.py"))
    index_revisions = list(versions.glob("*_add_strict_otp_claim_indexes.py"))
    assert "totp_codes" not in hot_revision
    assert len(schema_revisions) == len(index_revisions) == 1
    schema_revision = schema_revisions[0].read_text()
    index_revision = index_revisions[0].read_text()
    assert 'down_revision = "9f6b4c2a8d71"' in schema_revision
    assert "autocommit_block" in hot_revision
    assert "op.create_index" not in schema_revision
    assert "CREATE INDEX CONCURRENTLY" in index_revision
    assert "op.create_index" not in index_revision
    migration_contract_errors: list[str] = []
    if schema_revision.count('ondelete="CASCADE"') != 3:
        migration_contract_errors.append("strict owner and acquisition foreign keys must cascade")
    if index_revision.count("(public.skyvern_sha256_text_utf8(code))") != 2:
        migration_contract_errors.append("strict code indexes must hash literal UTF-8 text")
    if "sha256(code::bytea)" in index_revision:
        migration_contract_errors.append("strict code indexes must not parse text as bytea")
    assert not migration_contract_errors, "; ".join(migration_contract_errors)
    assert "otp_request_id, totp_identifier, code)" not in index_revision
    assert "otp_request_id, code)" not in index_revision

    scratch_database = postgres_scratch_database
    with scratch_database.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE organizations (organization_id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE workflow_runs (workflow_run_id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE tasks (task_id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE observer_cruises (observer_cruise_id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE totp_codes ("
            "totp_code_id VARCHAR PRIMARY KEY, organization_id VARCHAR NOT NULL, "
            "totp_identifier VARCHAR NOT NULL, code VARCHAR, source VARCHAR, "
            "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    hot_path = versions / "2026_11_17_1500-9f6b4c2a8d71_add_strict_otp_claim_fields.py"
    schema_path = schema_revisions[0]

    def run_migration(path: Path, direction: str = "upgrade") -> None:
        spec = importlib.util.spec_from_file_location(f"strict_otp_{path.stem}_{direction}", path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with scratch_database.connect() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context), context.begin_transaction():
                getattr(migration, direction)()

    run_migration(hot_path)
    blocker = scratch_database.connect()
    blocker_transaction = blocker.begin()
    try:
        blocker.exec_driver_sql("LOCK TABLE organizations IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            migration_future = executor.submit(run_migration, schema_path)
            deadline = time.monotonic() + 3
            migration_is_waiting = False
            while time.monotonic() < deadline:
                with scratch_database.connect() as observer:
                    migration_is_waiting = bool(
                        observer.execute(
                            text(
                                "SELECT EXISTS ("
                                "SELECT 1 FROM pg_stat_activity "
                                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                                "AND wait_event_type = 'Lock' AND query ILIKE '%otp_request_bindings%'"
                                ")"
                            )
                        ).scalar_one()
                    )
                if migration_is_waiting:
                    break
                time.sleep(0.05)
            assert migration_is_waiting, "schema migration never reached the referenced-table lock wait"

            task_select_error: OperationalError | None = None
            try:
                try:
                    with scratch_database.connect() as independent_reader:
                        independent_reader.exec_driver_sql("SET statement_timeout = '2s'")
                        assert independent_reader.exec_driver_sql("SELECT count(*) FROM tasks").scalar_one() == 0
                except OperationalError as exc:
                    task_select_error = exc
                # The blocker outlives the revision's own 5 s lock_timeout, so the wait must end in a lock timeout.
                with pytest.raises(OperationalError, match="lock timeout"):
                    migration_future.result(timeout=10)
            finally:
                blocker_transaction.rollback()
            assert task_select_error is None, "revision held the tasks ALTER lock while waiting on a foreign key"
    finally:
        if blocker_transaction.is_active:
            blocker_transaction.rollback()
        blocker.close()

    run_migration(schema_path)
    run_migration(hot_path)
    assert 'ALTER TABLE "workflow_runs" ADD COLUMN IF NOT EXISTS otp_request_id VARCHAR' in hot_revision
    assert 'ALTER TABLE "workflow_runs" DROP COLUMN IF EXISTS otp_request_id' in hot_revision
    with scratch_database.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'workflow_runs' "
                    "AND column_name = 'otp_request_id'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'otp_request_id' "
                    "AND table_name IN ('tasks', 'observer_cruises', 'totp_codes')"
                )
            ).scalar_one()
            == 3
        )

    with scratch_database.begin() as connection:
        connection.exec_driver_sql("INSERT INTO organizations (organization_id) VALUES ('o_literal')")

    spec = importlib.util.spec_from_file_location("strict_otp_claim_indexes_migration", index_revisions[0])
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with scratch_database.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration.upgrade()

    with scratch_database.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        index_rows = connection.execute(
            text(
                "SELECT c.relname, i.indisvalid, pg_get_indexdef(i.indexrelid) "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname IN ('uq_totp_codes_strict_code', 'uq_totp_codes_strict_webhook_code') "
                "ORDER BY c.relname"
            )
        ).all()
        assert len(index_rows) == 2
        assert all(valid for _, valid, _ in index_rows)
        assert all("skyvern_sha256_text_utf8" in definition for _, _, definition in index_rows)
        assert all("code::bytea" not in definition for _, _, definition in index_rows)
        function_properties = connection.execute(
            text(
                "SELECT p.provolatile, p.proisstrict, p.proparallel, p.prosecdef, p.proconfig "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'public' AND p.proname = 'skyvern_sha256_text_utf8'"
            )
        ).one()
        assert function_properties == ("i", True, "s", False, ["search_path=pg_catalog"])

        literal_codes = [
            r"\xGG",
            r"\x4142",
            "AB",
            "424242",
            r"https://example.test/verify\path?token=\xGG",
        ]
        for index_name, identifier, source in (
            ("uq_totp_codes_strict_code", "literal@example.test", None),
            ("uq_totp_codes_strict_webhook_code", None, "verification_url"),
        ):
            for position, code in enumerate(literal_codes):
                values = {
                    "id": f"{index_name}_{position}",
                    "organization_id": "o_literal",
                    "otp_request_id": "otpr_literal",
                    "totp_identifier": identifier,
                    "code": code,
                    "source": source,
                }
                connection.execute(
                    text(
                        "INSERT INTO totp_codes "
                        "(totp_code_id, organization_id, otp_request_id, totp_identifier, code, source) "
                        "VALUES (:id, :organization_id, :otp_request_id, :totp_identifier, :code, :source)"
                    ),
                    values,
                )
                with pytest.raises(IntegrityError) as duplicate_error:
                    connection.execute(
                        text(
                            "INSERT INTO totp_codes "
                            "(totp_code_id, organization_id, otp_request_id, totp_identifier, code, source) "
                            "VALUES (:id, :organization_id, :otp_request_id, :totp_identifier, :code, :source)"
                        ),
                        {**values, "id": f"{index_name}_{position}_duplicate"},
                    )
                assert duplicate_error.value.orig.diag.constraint_name == index_name

    with scratch_database.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context), context.begin_transaction():
            migration.downgrade()
    with scratch_database.connect() as connection:
        assert connection.execute(
            text(
                "SELECT to_regclass('public.uq_totp_codes_strict_code'), "
                "to_regclass('public.uq_totp_codes_strict_webhook_code'), "
                "to_regprocedure('public.skyvern_sha256_text_utf8(text)')"
            )
        ).one() == (None, None, None)
    run_migration(schema_path, "downgrade")
    run_migration(hot_path, "downgrade")
    run_migration(hot_path, "downgrade")

    ids = await _seed(agent_db)
    workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
    )
    task = await agent_db.tasks.create_task(
        url="https://example.test",
        title="Strict task",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=ids["organization_id"],
        workflow_run_id=workflow_run.workflow_run_id,
    )
    task_v2 = await agent_db.observer.create_task_v2(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=ids["organization_id"],
        prompt="Strict task v2",
        otp_request_id="otpr_test",
    )

    assert workflow_run.otp_request_id == "otpr_test"
    assert task.otp_request_id == "otpr_test"
    assert task_v2.otp_request_id == "otpr_test"
    committed_task_claims: list[str] = []
    task_dispatch_claim = await agent_db.tasks.claim_task_for_temporal_dispatch(
        task.task_id,
        ids["organization_id"],
        on_committed=lambda: committed_task_claims.append(task.task_id),
    )
    duplicate_task_dispatch_claim = await agent_db.tasks.claim_task_for_temporal_dispatch(
        task.task_id,
        ids["organization_id"],
        on_committed=lambda: committed_task_claims.append(task.task_id),
    )
    assert task_dispatch_claim is not None
    assert task_dispatch_claim.status == TaskStatus.queued
    assert task_dispatch_claim.queued_at is not None
    assert duplicate_task_dispatch_claim is None
    assert committed_task_claims == [task.task_id]
    losing_task_cleanup = await agent_db.tasks.fail_task_for_temporal_dispatch_precommit(
        task.task_id,
        ids["organization_id"],
        failure_reason="strict reservation failed before commit",
    )
    stored_task = await agent_db.tasks.get_task(task.task_id, organization_id=ids["organization_id"])
    assert losing_task_cleanup is None
    assert stored_task is not None
    assert stored_task.status == TaskStatus.queued
    assert stored_task.failure_reason is None

    unclaimed_task = await agent_db.tasks.create_task(
        url="https://example.test/unclaimed",
        title="Unclaimed strict task",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=ids["organization_id"],
        workflow_run_id=workflow_run.workflow_run_id,
    )
    winning_task_cleanup = await agent_db.tasks.fail_task_for_temporal_dispatch_precommit(
        unclaimed_task.task_id,
        ids["organization_id"],
        failure_reason="strict reservation failed before commit",
    )
    assert winning_task_cleanup is not None
    assert winning_task_cleanup.status == TaskStatus.failed
    assert winning_task_cleanup.failure_reason == "strict reservation failed before commit"
    dispatch_claim = await agent_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id=workflow_run.workflow_run_id,
        status=WorkflowRunStatus.running,
        only_from=[WorkflowRunStatus.running],
        job_id="o_test-strict-workflow",
        reserve_for_dispatch=True,
        require_job_id_unset=True,
    )
    duplicate_dispatch_claim = await agent_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id=workflow_run.workflow_run_id,
        status=WorkflowRunStatus.running,
        only_from=[WorkflowRunStatus.running],
        job_id="o_test-duplicate-workflow",
        reserve_for_dispatch=True,
        require_job_id_unset=True,
    )
    assert dispatch_claim is not None
    assert dispatch_claim.job_id == "o_test-strict-workflow"
    assert duplicate_dispatch_claim is None
    losing_workflow_cleanup = await agent_db.workflow_runs.update_workflow_run_if_not_final(
        workflow_run_id=workflow_run.workflow_run_id,
        status=WorkflowRunStatus.failed,
        failure_reason="strict reservation failed before commit",
        only_from=[WorkflowRunStatus.running],
        require_job_id_unset=True,
    )
    stored_workflow_run = await agent_db.workflow_runs.get_workflow_run(workflow_run.workflow_run_id)
    assert losing_workflow_cleanup is None
    assert stored_workflow_run is not None
    assert stored_workflow_run.status == WorkflowRunStatus.running
    assert stored_workflow_run.job_id == "o_test-strict-workflow"
    assert stored_workflow_run.failure_reason is None
    async with agent_db.Session() as session:
        binding = (
            await session.scalars(
                select(OTPRequestBindingModel).where(
                    OTPRequestBindingModel.organization_id == ids["organization_id"],
                    OTPRequestBindingModel.otp_request_id == "otpr_test",
                )
            )
        ).one()
    assert binding.owner_type == "workflow_run"
    assert binding.owner_workflow_run_id == workflow_run.workflow_run_id
    assert binding.owner_task_id is None

    with pytest.raises(OTPRequestAlreadyBoundError) as duplicate_workflow_error:
        await agent_db.workflow_runs.create_workflow_run(
            workflow_permanent_id="wpid_test",
            workflow_id=ids["workflow_id"],
            organization_id=ids["organization_id"],
            otp_request_id="otpr_test",
        )
    assert duplicate_workflow_error.value.owner_type == "workflow_run"
    assert duplicate_workflow_error.value.owner_id == workflow_run.workflow_run_id
    async with agent_db.Session() as session:
        strict_workflow_ids = (
            await session.scalars(
                select(WorkflowRunModel.workflow_run_id).where(WorkflowRunModel.otp_request_id == "otpr_test")
            )
        ).all()
    assert strict_workflow_ids == [workflow_run.workflow_run_id]

    task_root = await agent_db.tasks.create_task(
        url="https://example.test",
        title="Strict task root",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=ids["organization_id"],
        otp_request_id="otpr_task_root",
    )
    async with agent_db.Session() as session:
        task_binding = (
            await session.scalars(
                select(OTPRequestBindingModel).where(
                    OTPRequestBindingModel.organization_id == ids["organization_id"],
                    OTPRequestBindingModel.otp_request_id == "otpr_task_root",
                )
            )
        ).one()
    assert task_binding.owner_type == "task"
    assert task_binding.owner_task_id == task_root.task_id
    assert task_binding.owner_workflow_run_id is None

    with pytest.raises(OTPRequestAlreadyBoundError) as duplicate_task_error:
        await agent_db.tasks.create_task(
            url="https://example.test/retry",
            title="Duplicate strict task root",
            navigation_goal=None,
            data_extraction_goal=None,
            navigation_payload=None,
            organization_id=ids["organization_id"],
            otp_request_id="otpr_task_root",
        )
    assert duplicate_task_error.value.owner_type == "task"
    assert duplicate_task_error.value.owner_id == task_root.task_id
    async with agent_db.Session() as session:
        strict_task_ids = (
            await session.scalars(select(TaskModel.task_id).where(TaskModel.otp_request_id == "otpr_task_root"))
        ).all()
    assert strict_task_ids == [task_root.task_id]

    sealed = await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_test",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="workflow_run",
        execution_owner_id=workflow_run.workflow_run_id,
        execution_attempt="1",
        worker_pid=123,
        worker_process_started_at="456.0",
        pod_uid="pod-test",
        container_identity="container-test",
    )
    duplicate = await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_duplicate",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="workflow_run",
        execution_owner_id=workflow_run.workflow_run_id,
        execution_attempt="1",
        worker_pid=123,
        worker_process_started_at="456.0",
    )
    selected = await agent_db.otp.record_strict_browser_selection(
        acquisition_id="otpa_test",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        browser_type="chromium-headful",
        browser_engine="playwright",
    )
    stopped = await agent_db.otp.record_strict_browser_stop(
        acquisition_id="otpa_test",
        organization_id=ids["organization_id"],
        confirmed=True,
    )
    async with agent_db.Session() as session:
        acquisition = await session.get(OTPRequestAcquisitionModel, "otpa_test")
    assert sealed is True and duplicate is False and selected is True and stopped is True
    assert acquisition is not None
    assert (acquisition.browser_type, acquisition.browser_engine) == (
        "chromium-headful",
        "playwright",
    )
    assert acquisition.lifecycle_state == "released"
    assert acquisition.stop_confirmed_at is not None
    assert not await agent_db.otp.record_strict_browser_stop(
        acquisition_id="otpa_test",
        organization_id=ids["organization_id"],
        confirmed=False,
        failure_reason="late cleanup observer",
    )
    async with agent_db.Session() as session:
        acquisition = await session.get(OTPRequestAcquisitionModel, "otpa_test")
    assert acquisition is not None and acquisition.lifecycle_state == "released"
    assert acquisition.stop_confirmed_at is not None and acquisition.failure_reason is None
    assert not await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_wrong_owner",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="task",
        execution_owner_id=task.task_id,
        execution_attempt="2",
        worker_pid=123,
        worker_process_started_at="456.0",
    )
    assert await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_unconfirmed",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="workflow_run",
        execution_owner_id=workflow_run.workflow_run_id,
        execution_attempt="2",
        worker_pid=123,
        worker_process_started_at="456.0",
    )
    assert await agent_db.otp.record_strict_browser_stop(
        acquisition_id="otpa_unconfirmed",
        organization_id=ids["organization_id"],
        confirmed=False,
    )
    assert not await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_blocked_retry",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="workflow_run",
        execution_owner_id=workflow_run.workflow_run_id,
        execution_attempt="3",
        worker_pid=123,
        worker_process_started_at="456.0",
    )
    assert await agent_db.otp.record_strict_browser_stop(
        acquisition_id="otpa_unconfirmed",
        organization_id=ids["organization_id"],
        confirmed=True,
    )
    assert await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_recovered_retry",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="workflow_run",
        execution_owner_id=workflow_run.workflow_run_id,
        execution_attempt="3",
        worker_pid=123,
        worker_process_started_at="456.0",
    )
    child_workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
        parent_workflow_run_id=workflow_run.workflow_run_id,
    )
    assert child_workflow_run.otp_request_id == "otpr_test"
    assert await agent_db.otp.seal_strict_browser_acquisition(
        acquisition_id="otpa_child",
        organization_id=ids["organization_id"],
        otp_request_id="otpr_test",
        execution_owner_type="workflow_run",
        execution_owner_id=child_workflow_run.workflow_run_id,
        execution_attempt="1",
        worker_pid=321,
        worker_process_started_at="654.0",
    )
    async with agent_db.Session() as session:
        session.add(
            TOTPCodeModel(
                totp_code_id="otp_run_scoped",
                organization_id=ids["organization_id"],
                totp_identifier=_strict_identifier("shared@example.test", "otpr_test"),
                content="555555",
                code="555555",
                otp_type=OTPType.TOTP,
                parse_status="strict_parsed",
                otp_request_id="otpr_test",
                workflow_id=ids["workflow_id"],
                workflow_run_id=workflow_run.workflow_run_id,
            )
        )
        await session.commit()
    run_scoped_claim = await agent_db.otp.claim_otp_code(
        organization_id=ids["organization_id"],
        totp_identifier="shared@example.test",
        otp_request_id="otpr_test",
        task_id=task.task_id,
        workflow_id=ids["workflow_id"],
        workflow_run_id=workflow_run.workflow_run_id,
    )
    assert run_scoped_claim is not None and run_scoped_claim.totp_code_id == "otp_run_scoped"

    with pytest.raises(ValueError, match="marker"):
        await agent_db.tasks.create_task(
            url="https://example.test",
            title="Conflicting child",
            navigation_goal=None,
            data_extraction_goal=None,
            navigation_payload=None,
            organization_id=ids["organization_id"],
            workflow_run_id=workflow_run.workflow_run_id,
            otp_request_id="otpr_other",
        )
    other_org = await agent_db.organizations.create_organization(organization_name="Other Org", domain="other-wrp.test")
    with pytest.raises(ValueError, match="organization"):
        await agent_db.tasks.create_task(
            url="https://example.test",
            title="Cross-org child",
            navigation_goal=None,
            data_extraction_goal=None,
            navigation_payload=None,
            organization_id=other_org.organization_id,
            workflow_run_id=workflow_run.workflow_run_id,
        )

    cascade_workflow = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id="wpid_test",
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
        otp_request_id="otpr_cascade_workflow",
    )
    cascade_task = await agent_db.tasks.create_task(
        url="https://example.test/cascade",
        title="Strict cascade task",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=ids["organization_id"],
        otp_request_id="otpr_cascade_task",
    )
    for marker, owner_type, owner_id, acquisition_id in (
        ("otpr_cascade_workflow", "workflow_run", cascade_workflow.workflow_run_id, "otpa_cascade_workflow"),
        ("otpr_cascade_task", "task", cascade_task.task_id, "otpa_cascade_task"),
    ):
        assert await agent_db.otp.seal_strict_browser_acquisition(
            acquisition_id=acquisition_id,
            organization_id=ids["organization_id"],
            otp_request_id=marker,
            execution_owner_type=owner_type,
            execution_owner_id=owner_id,
            execution_attempt="1",
            worker_pid=123,
            worker_process_started_at="456.0",
        )
    if db_engine.dialect.name == "postgresql":
        async with agent_db.Session() as session:
            await session.execute(
                delete(WorkflowRunModel).where(WorkflowRunModel.workflow_run_id == cascade_workflow.workflow_run_id)
            )
            await session.execute(delete(TaskModel).where(TaskModel.task_id == cascade_task.task_id))
            await session.commit()
        async with agent_db.Session() as session:
            for marker, acquisition_id in (
                ("otpr_cascade_workflow", "otpa_cascade_workflow"),
                ("otpr_cascade_task", "otpa_cascade_task"),
            ):
                assert await session.get(OTPRequestBindingModel, (ids["organization_id"], marker)) is None
                assert await session.get(OTPRequestAcquisitionModel, acquisition_id) is None
