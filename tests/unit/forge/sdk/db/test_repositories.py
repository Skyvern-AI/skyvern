"""Tests for all OSS repository instantiations + dependency injection."""

import importlib
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import libcst as cst
import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from structlog.testing import capture_logs

from skyvern.core.script_generations.generate_script import _action_to_stmt
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType, WorkflowRunStatus
from skyvern.forge.sdk.db.id import generate_artifact_id
from skyvern.forge.sdk.db.models import (
    ActionModel,
    OrganizationAuthTokenModel,
    OrganizationModel,
    PersistentBrowserSessionModel,
    ScriptBlockModel,
    ScriptModel,
    TaskModel,
    TaskRunModel,
    WorkflowRunBlockModel,
    WorkflowScriptModel,
)
from skyvern.forge.sdk.db.repositories.organizations import OrganizationsRepository
from skyvern.forge.sdk.db.repositories.scripts import ScriptsRepository
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.encrypt.base import EncryptMethod
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.totp_codes import OTPType, RawTOTPCode, TOTPCode
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager
from skyvern.forge.sdk.workflow.reached_download_target import GENERATED_FILE_ARTIFACT_IDS_KEY
from skyvern.schemas.runs import ProxyLocation, RunType
from skyvern.schemas.workflows import BlockType
from skyvern.utils import run_data_scrub
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER, REDACTION_FAILED_STUB
from skyvern.webeye.actions.actions import InputTextAction
from tests.unit._sql_recording import recorded_statements
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session

_REPOSITORY_METHODS: dict[str, tuple[str, ...]] = {
    "skyvern.forge.sdk.db.repositories.credentials.CredentialRepository": (
        "create_credential",
        "get_credential",
        "get_credentials",
        "update_credential",
        "delete_credential",
        "create_organization_bitwarden_collection",
        "get_organization_bitwarden_collection",
    ),
    "skyvern.forge.sdk.db.repositories.credential_folders.CredentialFoldersRepository": (
        "create_credential_folder",
        "get_credential_folder",
        "get_credential_folders",
        "update_credential_folder",
        "soft_delete_credential_folder",
        "get_credential_folder_credential_count",
        "get_credential_folder_credential_counts_batch",
        "set_credential_folder",
    ),
    "skyvern.forge.sdk.db.repositories.otp.OTPRepository": (
        "get_otp_codes",
        "create_otp_code",
    ),
    "skyvern.forge.sdk.db.repositories.debug.DebugRepository": (
        "get_debug_session",
        "create_debug_session",
        "create_block_run",
    ),
    "skyvern.forge.sdk.db.repositories.organizations.OrganizationsRepository": (
        "get_organization",
        "create_organization",
        "create_org_auth_token",
        "validate_org_auth_token",
    ),
    "skyvern.forge.sdk.db.repositories.schedules.SchedulesRepository": (
        "create_workflow_schedule",
        "get_workflow_schedules",
    ),
    "skyvern.forge.sdk.db.repositories.scripts.ScriptsRepository": (
        "create_script",
        "get_scripts",
        "soft_delete_workflow_script_if_matches",
        "restore_workflow_script_if_matches",
    ),
    "skyvern.forge.sdk.db.repositories.self_heal.SelfHealRepository": (
        "create_heal_episode",
        "get_heal_episodes",
        "create_heal_proposal",
        "get_heal_proposals",
        "update_heal_proposal_status",
    ),
    "skyvern.forge.sdk.db.repositories.workflow_parameters.WorkflowParametersRepository": (
        "get_workflow_parameter",
        "create_workflow_parameter",
    ),
    "skyvern.forge.sdk.db.repositories.tasks.TasksRepository": (
        "create_task",
        "get_task",
        "create_step",
    ),
    "skyvern.forge.sdk.db.repositories.workflows.WorkflowsRepository": (
        "get_workflow",
        "create_workflow",
        "get_workflow_by_permanent_id",
        "update_workflow_dispatch_state_if_latest_with_previous",
        "restore_workflow_script_dispatch_if_matches",
    ),
    "skyvern.forge.sdk.db.repositories.browser_sessions.BrowserSessionsRepository": (
        "create_browser_profile",
        "get_browser_profile",
        "update_browser_profile",
        "delete_browser_profile",
    ),
}


@pytest.mark.parametrize("repository", list(_REPOSITORY_METHODS))
def test_repository_instantiation(repository: str) -> None:
    module_name, class_name = repository.rsplit(".", 1)
    repository_class = getattr(importlib.import_module(module_name), class_name)

    mock_session = MagicMock()
    repo = repository_class(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    for method in _REPOSITORY_METHODS[repository]:
        assert hasattr(repo, method)


@pytest.mark.asyncio
async def test_otp_repository_can_include_unscoped_workflow_run_rows_in_sql():
    from skyvern.forge.sdk.db.repositories.otp import OTPRepository

    class CapturingSession:
        query = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def scalars(self, query):
            self.query = query
            return SimpleNamespace(all=lambda: [])

    session = CapturingSession()
    repo = OTPRepository(session_factory=lambda: session, debug_enabled=False)

    await repo.get_otp_codes(
        organization_id="o_test",
        totp_identifier="otp@example.test",
        workflow_run_id="wr_test",
        include_unscoped_workflow_run=True,
    )

    sql = str(session.query)
    assert "totp_codes.workflow_run_id = :workflow_run_id_1" in sql
    assert "totp_codes.workflow_run_id IS NULL" in sql
    assert " OR " in sql
    assert "totp_codes.parse_status = :parse_status_1" in sql
    await repo.get_raw_otp_codes(
        organization_id="o_test",
        totp_identifier="otp@example.test",
        workflow_run_id="wr_test",
        include_unscoped_workflow_run=True,
        created_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    sql = str(session.query)
    assert "totp_codes.parse_status = :parse_status_1" in sql
    assert "totp_codes.workflow_run_id IS NULL" in sql
    assert "totp_codes.created_at >=" in sql


@pytest.mark.asyncio
async def test_otp_repository_stores_blank_run_scoping_ids_as_null():
    from skyvern.forge.sdk.db.repositories.otp import OTPRepository
    from skyvern.forge.sdk.schemas.totp_codes import OTPType

    class CapturingWriteSession:
        added = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, obj):
            self.added = obj

        async def commit(self):
            return None

        async def flush(self):
            self.added.totp_code_id = "otp_test"
            self.added.created_at = self.added.modified_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    session = CapturingWriteSession()
    repo = OTPRepository(session_factory=lambda: session, debug_enabled=False)

    await repo.create_otp_code(
        organization_id="o_test",
        totp_identifier="otp@example.test",
        content="123456",
        code="123456",
        otp_type=OTPType.TOTP,
        task_id="",
        workflow_id="",
        workflow_run_id="",
    )

    assert session.added.workflow_run_id is None
    assert session.added.workflow_id is None
    assert session.added.task_id is None


@pytest.mark.asyncio
async def test_otp_repository_creates_raw_row_without_fabricated_code():
    from skyvern.forge.sdk.db.repositories.otp import OTPRepository

    class CapturingWriteSession:
        added = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, obj):
            self.added = obj

        async def commit(self):
            return None

        async def flush(self):
            self.added.totp_code_id = "otp_raw"
            self.added.created_at = self.added.modified_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    session = CapturingWriteSession()
    repo = OTPRepository(session_factory=lambda: session, debug_enabled=False)
    result = await repo.create_raw_otp_code(
        organization_id="o_test",
        totp_identifier="otp@example.test",
        content="unparsed content",
        workflow_run_id="",
    )

    assert result.totp_code_id == "otp_raw"
    assert session.added.code is None
    assert session.added.otp_type is None
    assert session.added.parse_status == "raw"
    assert session.added.workflow_run_id is None


@pytest.mark.asyncio
async def test_organizations_repository_persists_and_clears_default_llm_keys(sqlite_engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(OrganizationModel(organization_id="o_defaults", organization_name="Defaults Org"))
        await session.commit()

    repo = OrganizationsRepository(session_factory=session_factory, debug_enabled=False)
    updated = await repo.update_organization(
        "o_defaults",
        default_llm_key="CUSTOM_LLM_oat_primary",
        default_secondary_llm_key="CUSTOM_LLM_oat_secondary",
    )

    assert updated.default_llm_key == "CUSTOM_LLM_oat_primary"
    assert updated.default_secondary_llm_key == "CUSTOM_LLM_oat_secondary"
    async with session_factory() as session:
        stored = await session.get(OrganizationModel, "o_defaults")
        assert stored is not None
        assert stored.default_llm_key == "CUSTOM_LLM_oat_primary"
        assert stored.default_secondary_llm_key == "CUSTOM_LLM_oat_secondary"

    cleared = await repo.update_organization(
        "o_defaults",
        clear_default_llm_key=True,
        clear_default_secondary_llm_key=True,
    )

    assert cleared.default_llm_key is None
    assert cleared.default_secondary_llm_key is None
    async with session_factory() as session:
        stored = await session.get(OrganizationModel, "o_defaults")
        assert stored is not None
        assert stored.default_llm_key is None
        assert stored.default_secondary_llm_key is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("registered_encryption")
async def test_validate_org_auth_token_matches_by_decrypting_each_row(sqlite_engine: AsyncEngine) -> None:
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    # "key-on-cbc" as the retired CBC writer stored it for the registered_encryption deployment (public salt/IV).
    legacy_ciphertext = "Y2DD0pw4mlwHHSyFjNP6GA=="
    async with session_factory() as session:
        session.add(OrganizationModel(organization_id="o_keys", organization_name="Keys Org"))
        session.add(
            OrganizationAuthTokenModel(
                organization_id="o_keys",
                token_type=OrganizationAuthTokenType.api,
                token="",
                encrypted_token=legacy_ciphertext,
                encrypted_method=EncryptMethod.AES.value,
            )
        )
        session.add(
            OrganizationAuthTokenModel(
                organization_id="o_keys",
                token_type=OrganizationAuthTokenType.api,
                token="revoked-plaintext-key",
                encrypted_token="",
                encrypted_method="",
                valid=False,
            )
        )
        await session.commit()
    repo = OrganizationsRepository(session_factory=session_factory, debug_enabled=False)
    gcm_key = await repo.create_org_auth_token(
        "o_keys", OrganizationAuthTokenType.api, "key-on-gcm", encrypted_method=EncryptMethod.AES_GCM
    )

    for plaintext in ("key-on-cbc", "key-on-gcm"):
        found = await repo.validate_org_auth_token("o_keys", OrganizationAuthTokenType.api, plaintext)
        assert found is not None and found.token == plaintext
    assert (await repo.validate_org_auth_token("o_keys", OrganizationAuthTokenType.api, "key-on-gcm")).id == gcm_key.id
    assert await repo.validate_org_auth_token("o_keys", OrganizationAuthTokenType.api, "unknown-key") is None
    assert await repo.validate_org_auth_token("o_keys", OrganizationAuthTokenType.api, "revoked-plaintext-key") is None
    revoked = await repo.validate_org_auth_token(
        "o_keys", OrganizationAuthTokenType.api, "revoked-plaintext-key", valid=None
    )
    assert revoked is not None and revoked.valid is False


@pytest.mark.asyncio
async def test_get_cached_block_groups_by_labels_filters_in_sql(sqlite_engine: AsyncEngine):
    """SKY-15102: workflow save timed out because cache invalidation loaded every cached
    script for the workflow (tens of thousands, in the reported incident) before filtering
    by label in Python. ``get_cached_block_groups_by_labels`` pushes that filter into SQL
    instead. This exercises the real query against a real database and pins every exclusion
    it must apply: wrong label, missing run_signature, soft-deleted workflow_script,
    soft-deleted latest script version, an older (non-latest) version, wrong workflow, and
    wrong organization.
    """
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repo = ScriptsRepository(session_factory=factory, debug_enabled=False)
    org = "o_1"
    wpid = "wpid_1"

    def workflow_script(
        suffix: str,
        *,
        script_id: str | None = None,
        workflow_permanent_id: str = wpid,
        organization_id: str = org,
        status: str = "published",
        **kwargs,
    ):
        return WorkflowScriptModel(
            workflow_script_id=f"ws_{suffix}",
            script_id=script_id or f"s_{suffix}",
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
            cache_key="default",
            cache_key_value=f"default-{suffix}",
            status=status,
            **kwargs,
        )

    def script(
        suffix: str,
        *,
        script_id: str | None = None,
        revision_id: str | None = None,
        version: int = 1,
        organization_id: str = org,
        **kwargs,
    ):
        return ScriptModel(
            script_revision_id=revision_id or f"r_{suffix}",
            script_id=script_id or f"s_{suffix}",
            organization_id=organization_id,
            version=version,
            **kwargs,
        )

    def block(revision_id: str, label: str, *, script_id: str, run_signature: str | None, organization_id: str = org):
        return ScriptBlockModel(
            organization_id=organization_id,
            script_id=script_id,
            script_revision_id=revision_id,
            script_block_label=label,
            run_signature=run_signature,
        )

    deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)

    async with factory() as session:
        session.add_all(
            [
                # a: happy path -> returned
                workflow_script("a"),
                script("a"),
                block("r_a", "target_label", script_id="s_a", run_signature="sig_a"),
                # b: two versions of the same script; only the latest (v2) counts
                workflow_script("b", status="pending"),
                script("b", revision_id="r_b_v1", version=1),
                block("r_b_v1", "target_label", script_id="s_b", run_signature="sig_b_old"),
                script("b", revision_id="r_b_v2", version=2),
                block("r_b_v2", "target_label", script_id="s_b", run_signature="sig_b_new"),
                # c: matching label but no run_signature -> excluded
                workflow_script("c"),
                script("c"),
                block("r_c", "target_label", script_id="s_c", run_signature=None),
                # d: matching run_signature but wrong label -> excluded
                workflow_script("d"),
                script("d"),
                block("r_d", "other_label", script_id="s_d", run_signature="sig_d"),
                # e: soft-deleted workflow_script -> excluded
                workflow_script("e", deleted_at=deleted_at),
                script("e"),
                block("r_e", "target_label", script_id="s_e", run_signature="sig_e"),
                # f: soft-deleted latest script version -> excluded
                workflow_script("f"),
                script("f", deleted_at=deleted_at),
                block("r_f", "target_label", script_id="s_f", run_signature="sig_f"),
                # g: different workflow_permanent_id -> excluded
                workflow_script("g", workflow_permanent_id="wpid_other"),
                script("g"),
                block("r_g", "target_label", script_id="s_g", run_signature="sig_g"),
                # h: different organization -> excluded
                workflow_script("h", organization_id="o_other"),
                script("h", organization_id="o_other"),
                block("r_h", "target_label", script_id="s_h", run_signature="sig_h", organization_id="o_other"),
                # i: empty-string run_signature -> excluded (matches the old truthy check)
                workflow_script("i"),
                script("i"),
                block("r_i", "target_label", script_id="s_i", run_signature=""),
            ]
        )
        await session.commit()

    rows = await repo.get_cached_block_groups_by_labels(
        organization_id=org,
        workflow_permanent_id=wpid,
        block_labels=["target_label"],
    )

    returned = {(ws.workflow_script_id, sc.script_revision_id, blk.run_signature) for ws, sc, blk in rows}
    assert returned == {
        ("ws_a", "r_a", "sig_a"),
        ("ws_b", "r_b_v2", "sig_b_new"),
    }


@pytest.mark.asyncio
async def test_create_persistent_browser_session_preserves_integrity_error_contract() -> None:
    from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository

    original = SimpleNamespace(diag=SimpleNamespace(constraint_name="uq_pbs_live_workflow_binding"))
    error = IntegrityError("INSERT", {}, original)
    session = MagicMock()
    session.flush = AsyncMock(side_effect=error)
    repo = BrowserSessionsRepository(
        session_factory=lambda: MockAsyncSessionCtx(session),
        debug_enabled=False,
    )

    with pytest.raises(IntegrityError):
        await repo.create_persistent_browser_session(
            organization_id="org_binding_race",
            bound_workflow_permanent_id="wpid_binding_race",
            bound_key="shared-key",
        )


@pytest.mark.asyncio
async def test_mark_prewarm_dispatched_is_a_live_binding_cas(sqlite_engine: AsyncEngine) -> None:
    from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository

    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            PersistentBrowserSessionModel(
                persistent_browser_session_id="pbs_prewarm",
                organization_id="org_prewarm",
                status="created",
                runnable_type="pending",
                bound_workflow_permanent_id="debug-session-prewarm",
                bound_key="user-key",
            )
        )
        await session.commit()

    repo = BrowserSessionsRepository(session_factory=session_factory, debug_enabled=False)
    marked = await repo.mark_prewarm_dispatched(
        session_id="pbs_prewarm",
        organization_id="org_prewarm",
        expected_bound_workflow_permanent_id="debug-session-prewarm",
        expected_bound_key="user-key",
        expected_runnable_type="pending",
        dispatched_runnable_type="dispatched",
    )
    marked_twice = await repo.mark_prewarm_dispatched(
        session_id="pbs_prewarm",
        organization_id="org_prewarm",
        expected_bound_workflow_permanent_id="debug-session-prewarm",
        expected_bound_key="user-key",
        expected_runnable_type="pending",
        dispatched_runnable_type="dispatched",
    )

    assert marked is True
    assert marked_twice is False
    async with session_factory() as session:
        stored = await session.get(PersistentBrowserSessionModel, "pbs_prewarm")
        assert stored is not None
        assert stored.runnable_type == "dispatched"


# ── Cross-dependency repositories ──


def test_workflow_runs_repository_with_dependency():
    from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository

    mock_session = MagicMock()
    mock_param_reader = MagicMock()
    repo = WorkflowRunsRepository(
        session_factory=mock_session,
        debug_enabled=False,
        workflow_parameter_reader=mock_param_reader,
    )
    assert repo.Session is mock_session
    assert repo._workflow_parameter_reader is mock_param_reader
    assert hasattr(repo, "get_workflow_run_parameters")
    assert hasattr(repo, "create_workflow_run")
    assert hasattr(repo, "get_workflow_run")


def test_artifacts_repository_with_dependency():
    from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository

    mock_session = MagicMock()
    mock_run_reader = MagicMock()
    repo = ArtifactsRepository(
        session_factory=mock_session,
        debug_enabled=False,
        run_reader=mock_run_reader,
    )
    assert repo.Session is mock_session
    assert repo._run_reader is mock_run_reader
    assert hasattr(repo, "create_artifact")
    assert hasattr(repo, "get_artifact")


def test_folders_repository_with_dependency():
    from skyvern.forge.sdk.db.repositories.folders import FoldersRepository

    mock_session = MagicMock()
    mock_workflow_reader = MagicMock()
    repo = FoldersRepository(
        session_factory=mock_session,
        debug_enabled=False,
        workflow_reader=mock_workflow_reader,
    )
    assert repo.Session is mock_session
    assert repo._workflow_reader is mock_workflow_reader
    assert hasattr(repo, "create_folder")
    assert hasattr(repo, "update_workflow_folder")


def test_observer_repository_with_dependency():
    from skyvern.forge.sdk.db.repositories.observer import ObserverRepository

    mock_session = MagicMock()
    mock_task_reader = MagicMock()
    repo = ObserverRepository(
        session_factory=mock_session,
        debug_enabled=False,
        task_reader=mock_task_reader,
    )
    assert repo.Session is mock_session
    assert repo._task_reader is mock_task_reader
    assert hasattr(repo, "create_workflow_run_block")
    assert hasattr(repo, "get_workflow_run_blocks")


# ── AgentDB composition test ──


def test_agent_db_has_typed_repo_attributes():
    """After refactoring, AgentDB should expose typed repository attributes."""
    from skyvern.forge.sdk.db.repositories.credential_folders import CredentialFoldersRepository
    from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
    from skyvern.forge.sdk.db.repositories.self_heal import SelfHealRepository
    from skyvern.forge.sdk.db.repositories.tasks import TasksRepository

    with patch("skyvern.forge.sdk.db.agent_db.create_async_engine"):
        from skyvern.forge.sdk.db.agent_db import AgentDB

        db = AgentDB("postgresql+asyncpg://test", debug_enabled=True)
        assert isinstance(db.tasks, TasksRepository)
        assert isinstance(db.credentials, CredentialRepository)
        assert isinstance(db.credential_folders, CredentialFoldersRepository)
        assert isinstance(db.self_heal, SelfHealRepository)
        # Migrated domains no longer have delegates on AgentDB:
        assert not hasattr(db, "create_workflow")
        assert not hasattr(db, "get_organization")
        assert not hasattr(db, "get_credential")


def test_agent_db_defines_no_delegator_methods():
    """All data access goes through typed repository attributes; AgentDB itself defines no forwarding methods."""
    from skyvern.forge.sdk.db.agent_db import AgentDB

    defined = {name for name, member in vars(AgentDB).items() if inspect.isfunction(member)}
    assert defined == {"__init__", "is_retryable_error"}, (
        f"Unexpected methods on AgentDB: {sorted(defined - {'__init__', 'is_retryable_error'})}. "
        "Add data-access methods to the domain repository and call it via the typed attribute "
        "(e.g. db.tasks.get_task) instead of adding delegators to AgentDB."
    )


async def _create_task_with_status(monkeypatch: pytest.MonkeyPatch, status: str):
    from skyvern.forge.sdk.db.repositories import tasks as tasks_module

    session = make_mock_session(MagicMock())
    monkeypatch.setattr(tasks_module, "convert_to_task", lambda model, *args, **kwargs: model)
    repo = tasks_module.TasksRepository(
        session_factory=lambda: MockAsyncSessionCtx(session),
        debug_enabled=False,
    )

    return await repo.create_task(
        url="https://example.test/",
        title=None,
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        status=status,
    )


@pytest.mark.asyncio
async def test_create_task_running_is_not_created_after_it_started(monkeypatch: pytest.MonkeyPatch):
    """queued_seconds is started_at - created_at, so a task created already-running must not
    stamp started_at ahead of the flush-time created_at default."""
    task = await _create_task_with_status(monkeypatch, TaskStatus.running.value)

    assert task.started_at is not None
    assert task.created_at == task.started_at


@pytest.mark.asyncio
async def test_create_task_leaves_started_at_unset_for_other_statuses(monkeypatch: pytest.MonkeyPatch):
    task = await _create_task_with_status(monkeypatch, TaskStatus.created.value)

    assert task.started_at is None


@pytest.mark.asyncio
async def test_task_finish_claim_is_exactly_once_across_racing_finalizers(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
):
    """Two finalizers landing on one task must produce exactly one finish claim.

    The arbitration is the finished_at NULL->set flip: bulk_update_tasks' status
    CAS performs it atomically with its claim, and update_task_and_claim_finish
    reports whether ITS write performed it. The first interleaving encodes the
    reproduced race where a concurrent-agent-style writer pre-read a non-final
    status, the cron sweep claimed the task, and the agent's write still landed:
    the write lands, but the claim -- and any per-task side effect gated on it --
    stays with the sweep.
    """
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repo = TasksRepository(session_factory=factory, debug_enabled=False)
    from skyvern.forge.sdk.db.repositories import tasks as tasks_repository

    audit_actor = SimpleNamespace()
    record_cancel_audit = AsyncMock()
    monkeypatch.setattr(tasks_repository, "record_run_cancel_audit", record_cancel_audit)
    started = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _seed(task_id: str, finished_at: datetime | None = None) -> None:
        async with factory() as session:
            session.add(
                TaskModel(
                    task_id=task_id,
                    organization_id="o_race",
                    status=TaskStatus.running.value,
                    url="https://example.test/",
                    started_at=started,
                    finished_at=finished_at,
                    errors=[],
                )
            )
            await session.commit()

    # Sweep first: its CAS claim IS the flip, so the racing agent-style write
    # gets claim=False even though its (stale) pre-read saw a non-final status.
    await _seed("tsk_sweep_first")
    swept = await repo.bulk_update_tasks(
        ["tsk_sweep_first"], status=TaskStatus.timed_out, only_if_status_in=[TaskStatus.running]
    )
    _, agent_claimed = await repo.update_task_and_claim_finish(
        "tsk_sweep_first", status=TaskStatus.completed, organization_id="o_race"
    )
    assert swept == ["tsk_sweep_first"]
    assert agent_claimed is False

    # Agent first: the sweep's CAS finds no non-final row and claims nothing.
    await _seed("tsk_agent_first")
    _, agent_claimed = await repo.update_task_and_claim_finish(
        "tsk_agent_first", status=TaskStatus.completed, organization_id="o_race"
    )
    swept = await repo.bulk_update_tasks(
        ["tsk_agent_first"], status=TaskStatus.timed_out, only_if_status_in=[TaskStatus.running]
    )
    assert agent_claimed is True
    assert swept == []

    # Same writer twice (an overlapping activity retry): one flip, one claim.
    await _seed("tsk_retry")
    _, first = await repo.update_task_and_claim_finish(
        "tsk_retry", status=TaskStatus.timed_out, organization_id="o_race"
    )
    _, second = await repo.update_task_and_claim_finish(
        "tsk_retry", status=TaskStatus.timed_out, organization_id="o_race"
    )
    assert (first, second) == (True, False)

    await _seed("tsk_cancel_race")
    await repo.update_task_and_claim_finish("tsk_cancel_race", status=TaskStatus.completed, organization_id="o_race")
    canceled, finish_claimed, status_changed = await repo.update_task_and_claim_finish_for_cancel(
        "tsk_cancel_race", organization_id="o_race", audit_actor=audit_actor
    )
    assert canceled.status == TaskStatus.completed
    assert finish_claimed is False
    assert status_changed is False

    await _seed("tsk_cancel_transition", finished_at=started)
    canceled, finish_claimed, status_changed = await repo.update_task_and_claim_finish_for_cancel(
        "tsk_cancel_transition", organization_id="o_race", audit_actor=audit_actor
    )
    assert canceled.status == TaskStatus.canceled
    assert finish_claimed is False
    assert status_changed is True
    record_cancel_audit.assert_awaited_once_with(audit_actor, "o_race", "task", "tsk_cancel_transition")

    for background_sync in list(repo._background_tasks):
        await background_sync


@pytest.mark.asyncio
async def test_task_cancel_claim_persists_requested_fields(sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repo = TasksRepository(session_factory=factory, debug_enabled=False)
    monkeypatch.setattr(repo, "sync_task_run_status", AsyncMock())

    async with factory() as session:
        session.add(
            TaskModel(
                task_id="tsk_cancel_fields",
                organization_id="o_cancel_fields",
                status=TaskStatus.running.value,
                url="https://example.test/",
                errors=[{"error_code": "existing"}],
            )
        )
        await session.commit()

    canceled, finish_claimed, status_changed = await repo.update_task_and_claim_finish_for_cancel(
        "tsk_cancel_fields",
        organization_id="o_cancel_fields",
        failure_reason="The task stopped.",
        errors=[{"error_code": "cancelled"}],
        extracted_information={"result": "partial"},
        webhook_failure_reason="Webhook delivery failed.",
    )

    assert canceled.status == TaskStatus.canceled
    assert canceled.failure_reason == "The task stopped."
    assert canceled.errors == [{"error_code": "existing"}, {"error_code": "cancelled"}]
    assert canceled.extracted_information == {"result": "partial"}
    assert canceled.webhook_failure_reason == "Webhook delivery failed."
    assert finish_claimed is True
    assert status_changed is True
    for background_sync in list(repo._background_tasks):
        await background_sync


@pytest.mark.asyncio
async def test_task_v2_cancel_repository_audits_only_a_winning_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories import observer as observer_repository
    from skyvern.forge.sdk.db.repositories.observer import ObserverRepository
    from skyvern.forge.sdk.schemas.task_v2 import TaskV2Status

    events: list[str] = []
    results = ["tsk_v2_cancel", None]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def execute(self, _: object):
            value = results.pop(0)
            return SimpleNamespace(scalar_one_or_none=lambda: value)

        async def commit(self) -> None:
            events.append("commit")

    actor = SimpleNamespace()

    async def record_audit(*_: object) -> None:
        events.append("audit")

    monkeypatch.setattr(observer_repository, "record_run_cancel_audit", record_audit)
    repo = ObserverRepository(session_factory=Session, debug_enabled=False)

    assert await repo.update_task_v2_status_if_not_final(
        "tsk_v2_cancel", "org_cancel", TaskV2Status.canceled, audit_actor=actor
    )
    assert not await repo.update_task_v2_status_if_not_final(
        "tsk_v2_cancel", "org_cancel", TaskV2Status.canceled, audit_actor=actor
    )
    assert events == ["commit", "audit", "commit"]


@pytest.mark.asyncio
async def test_workflow_cancel_repository_audits_before_log_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge.sdk.db.repositories import workflow_runs as workflow_runs_repository
    from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository

    events: list[str] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def execute(self, _: object):
            return SimpleNamespace(scalar_one_or_none=lambda: object())

        async def commit(self) -> None:
            events.append("commit")

    async def record_audit(*_: object) -> None:
        events.append("audit")

    async def save_logs(_: str) -> None:
        events.append("logs")
        raise RuntimeError("artifact flush failed")

    async def on_transition() -> None:
        events.append("transition")

    monkeypatch.setattr(
        workflow_runs_repository,
        "convert_to_workflow_run",
        lambda _: SimpleNamespace(organization_id="org_cancel"),
    )
    monkeypatch.setattr(workflow_runs_repository, "record_run_cancel_audit", record_audit)
    monkeypatch.setattr(workflow_runs_repository, "save_workflow_run_logs", save_logs)
    repo = WorkflowRunsRepository(session_factory=Session, debug_enabled=False)

    with pytest.raises(RuntimeError, match="artifact flush failed"):
        await repo.update_workflow_run_if_not_final(
            "wr_cancel",
            WorkflowRunStatus.canceled,
            audit_actor=SimpleNamespace(),
            on_transition=on_transition,
        )

    assert events == ["commit", "audit", "transition", "logs"]


@pytest.mark.asyncio
async def test_resetting_a_task_for_rerun_re_arms_its_finish_claim(sqlite_engine: AsyncEngine):
    """A rerun of a finished task must be able to claim its own finish.

    The claim is the finished_at NULL->set flip, so a reset that leaves finished_at
    set hands the rerun a spent claim: its real compute never emits, and a later
    sweep that does claim the row emits the PREVIOUS run's duration.
    """
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repo = TasksRepository(session_factory=factory, debug_enabled=False)

    async with factory() as session:
        session.add(
            TaskModel(
                task_id="tsk_rerun",
                organization_id="o_rerun",
                status=TaskStatus.running.value,
                url="https://example.test/",
                queued_at=datetime.now(timezone.utc).replace(tzinfo=None),
                started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                errors=[],
                task_queue="previous-dispatch-queue",
            )
        )
        await session.commit()

    _, first_claim = await repo.update_task_and_claim_finish(
        "tsk_rerun", status=TaskStatus.completed, organization_id="o_rerun"
    )
    assert first_claim is True

    reset_task = await repo.reset_task_for_rerun(task_id="tsk_rerun", organization_id="o_rerun")

    assert reset_task.status == TaskStatus.created
    assert (reset_task.queued_at, reset_task.started_at, reset_task.finished_at) == (None, None, None)
    async with factory() as session:
        stored = await session.get(TaskModel, "tsk_rerun")
        assert stored is not None and stored.task_queue is None

    _, rerun_claim = await repo.update_task_and_claim_finish(
        "tsk_rerun", status=TaskStatus.completed, organization_id="o_rerun"
    )
    assert rerun_claim is True

    for background_sync in list(repo._background_tasks):
        await background_sync


_OTP_IDENTIFIER = "insert@example.test"
# Aware on purpose: the row stores naive UTC, and the returned value must match what a read gets back.
_OTP_EXPIRY = datetime.now(timezone.utc) + timedelta(hours=1)


async def _read_otp_code(db: AgentDB, organization_id: str, created: TOTPCode) -> TOTPCode:
    codes = await db.otp.get_otp_codes(organization_id=organization_id, totp_identifier=_OTP_IDENTIFIER)
    return next(code for code in codes if code.totp_code_id == created.totp_code_id)


async def _read_raw_otp_code(db: AgentDB, organization_id: str, created: RawTOTPCode) -> RawTOTPCode:
    codes = await db.otp.get_raw_otp_codes(organization_id=organization_id, totp_identifier=_OTP_IDENTIFIER)
    return next(code for code in codes if code.totp_code_id == created.totp_code_id)


async def _create_step(db: AgentDB, organization_id: str) -> Step:
    task = await db.tasks.create_task(
        url="https://example.test/",
        title="Insert",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization_id,
    )
    return await db.tasks.create_step(task.task_id, order=1, retry_index=0, organization_id=organization_id)


# Each entry: (create the row, read the same row back by its id).
_INSERTS: dict[str, tuple[Callable[..., Awaitable[BaseModel]], Callable[..., Awaitable[BaseModel | None]]]] = {
    "artifact": (
        lambda db, org: db.artifacts.create_artifact(
            artifact_id=generate_artifact_id(),
            artifact_type=ArtifactType.SCREENSHOT_LLM,
            uri="s3://bucket/screenshot.png",
            organization_id=org,
            task_id="tsk_insert",
            step_id="stp_insert",
            file_size=3,
        ),
        lambda db, org, row: db.artifacts.get_artifact_by_id(row.artifact_id, org),
    ),
    "otp_code": (
        lambda db, org: db.otp.create_otp_code(
            organization_id=org,
            totp_identifier=_OTP_IDENTIFIER,
            content="Your code is 123456",
            code="123456",
            otp_type=OTPType.TOTP,
            expired_at=_OTP_EXPIRY,
        ),
        _read_otp_code,
    ),
    "raw_otp_code": (
        lambda db, org: db.otp.create_raw_otp_code(
            organization_id=org,
            totp_identifier=_OTP_IDENTIFIER,
            content="Your code is 123456",
            expired_at=_OTP_EXPIRY,
        ),
        _read_raw_otp_code,
    ),
    # Workflow blocks pass earlier block outputs, which hold datetimes, and scraped text can hold NULs.
    "task": (
        lambda db, org: db.tasks.create_task(
            url="https://example.test/",
            title="Insert",
            navigation_goal="goal",
            data_extraction_goal=None,
            navigation_payload={
                "block_output": {"downloaded_files": [{"modified_at": datetime(2026, 9, 30, 12, 0, 0)}]},
                "field": "va\x00lue",
            },
            organization_id=org,
            download_timeout=1.5,
        ),
        lambda db, org, row: db.tasks.get_task(row.task_id, organization_id=org),
    ),
    "step": (
        _create_step,
        lambda db, org, row: db.tasks.get_step(row.step_id, organization_id=org),
    ),
    # The ISP pin is written by a second flush, whose UPDATE also stamps modified_at.
    "browser_session": (
        lambda db, org: db.browser_sessions.create_persistent_browser_session(
            organization_id=org,
            timeout_minutes=10,
            proxy_location=ProxyLocation.RESIDENTIAL_ISP,
        ),
        lambda db, org, row: db.browser_sessions.get_persistent_browser_session(row.persistent_browser_session_id, org),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("insert", list(_INSERTS))
async def test_create_returns_the_stored_row_without_reading_it_back(
    org_scoped_db: tuple[AgentDB, AsyncEngine, str], insert: str
) -> None:
    db, engine, organization_id = org_scoped_db
    create, read_back = _INSERTS[insert]

    with recorded_statements(engine) as statements:
        created = await create(db, organization_id)

    assert [statement for statement in statements if statement.startswith("SELECT")] == []
    stored = await read_back(db, organization_id, created)
    assert stored is not None
    assert created.model_dump() == stored.model_dump()


_SCRUB_RUN_ID = "wr_scrub_rows"
_RUN_SECRET = "s3cr3t-value-9f2"


async def _scrub_task_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    task = await db.tasks.create_task(
        url="https://example.test/",
        title="Scrub",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=org,
        workflow_run_id=_SCRUB_RUN_ID,
    )
    await db.tasks.update_task(
        task.task_id,
        organization_id=org,
        failure_reason=f"Typed {_RUN_SECRET} and failed",
        errors=[{"error_type": "USER_DEFINED_ERROR", "error_code": "LOGIN_FAILED", "reasoning": f"saw {_RUN_SECRET}"}],
        extracted_information={"email": _RUN_SECRET},
        failure_category=_failure_category_with_run_text(),
    )
    stored = await db.tasks.get_task(task.task_id, organization_id=org)
    assert stored is not None
    assert [(error["error_type"], error["error_code"]) for error in stored.errors] == [
        ("USER_DEFINED_ERROR", "LOGIN_FAILED")
    ]
    _assert_failure_category_machine_codes_kept(stored.failure_category)
    return [stored.failure_reason, stored.errors, stored.failure_category], [stored.extracted_information]


async def _scrub_task_row_when_the_lookup_fails(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    def failing_lookup(*_: object, **__: object) -> set[str]:
        raise RuntimeError("lookup failed")

    run_data_scrub.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run = failing_lookup
    task = await db.tasks.create_task(
        url="https://example.test/",
        title="Scrub",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=org,
        workflow_run_id=_SCRUB_RUN_ID,
    )
    await db.tasks.update_task(
        task.task_id,
        organization_id=org,
        failure_reason=f"Typed {_RUN_SECRET} and failed",
        errors=[
            {
                "error_type": "USER_DEFINED_ERROR",
                "error_code": "LOGIN_FAILED",
                "reasoning": f"saw {_RUN_SECRET}",
                "attempt": 2,
            }
        ],
    )
    stored = await db.tasks.get_task(task.task_id, organization_id=org)
    assert stored is not None
    # A JSON column keeps its shape with every string withheld; it never becomes a stub string. The error type and
    # code are machine codes that retry policy and the rerun reset read back, so they stay.
    assert stored.errors == [
        {
            "error_type": "USER_DEFINED_ERROR",
            "error_code": "LOGIN_FAILED",
            "reasoning": REDACTED_SECRET_PLACEHOLDER,
            "attempt": 2,
        }
    ]
    block = await db.observer.create_workflow_run_block(
        workflow_run_id=_SCRUB_RUN_ID, organization_id=org, block_type=BlockType.CODE
    )
    failure_category = [
        {
            "category": "LOGIN_ERROR",
            "confidence_float": 0.9,
            "reasoning": f"saw {_RUN_SECRET}",
            "user_errors": [{"error_code": "LOGIN_FAILED", "reasoning": f"typed {_RUN_SECRET}"}],
        }
    ]
    updated = await db.observer.update_workflow_run_block(
        block.workflow_run_block_id,
        organization_id=org,
        output={
            "failure_category": failure_category,
            GENERATED_FILE_ARTIFACT_IDS_KEY: ["a_generated"],
            "email": _RUN_SECRET,
            _RUN_SECRET: {"status": "done"},
        },
    )
    # The engine copies failure_category onto the run and counts downloads without the generated file ids; a key
    # holding run text is withheld too.
    assert _RUN_SECRET not in json.dumps(updated.output)
    withheld_keys = [key for key in updated.output if key.startswith(json.loads(REDACTION_FAILED_STUB))]
    assert len(withheld_keys) == 1
    assert updated.output[withheld_keys[0]] == {"status": REDACTED_SECRET_PLACEHOLDER}
    assert updated.output["failure_category"] == [
        {
            "category": "LOGIN_ERROR",
            "confidence_float": 0.9,
            "reasoning": REDACTED_SECRET_PLACEHOLDER,
            "user_errors": [{"error_code": "LOGIN_FAILED", "reasoning": REDACTED_SECRET_PLACEHOLDER}],
        }
    ]
    assert updated.output[GENERATED_FILE_ARTIFACT_IDS_KEY] == ["a_generated"]
    return [stored.failure_reason, stored.errors, updated.output], []


async def _scrub_action_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    step = await _create_step(db, org)
    action = InputTextAction(
        organization_id=org,
        workflow_run_id=_SCRUB_RUN_ID,
        task_id=step.task_id,
        step_id=step.step_id,
        step_order=1,
        action_order=0,
        element_id="el_1",
        skyvern_element_data={"xpath": "//input[@id='login']"},
        text=_RUN_SECRET,
        reasoning=f"Type {_RUN_SECRET}",
        intention=f"Enter {_RUN_SECRET}",
        response=f"Typed {_RUN_SECRET}",
    )
    created = await db.workflow_params.create_action(action)
    async with db.Session() as session:
        row = (await session.scalars(select(ActionModel).filter_by(action_id=created.action_id))).one()
    # The API and the timeline read the hydrated action, where a non-null action_json key wins over its column.
    (hydrated,) = await db.tasks.get_task_actions_hydrated(step.task_id, organization_id=org)
    assert isinstance(hydrated, InputTextAction)
    # The intention becomes the AI-fallback prompt of a generated script, so it is stored as typed.
    assert row.intention == hydrated.intention == f"Enter {_RUN_SECRET}"
    # Script generation reads the hydrated action back, so the stored action generates the same code as the typed one.
    stored_code, typed_code = (
        cst.Module(body=[_action_to_stmt({**act.model_dump(), "xpath": act.get_xpath()}, {})]).code
        for act in (hydrated, action)
    )
    assert stored_code == typed_code
    assert f"prompt = 'Enter {_RUN_SECRET}'" in stored_code
    return [row.reasoning, row.response, hydrated.reasoning, hydrated.response], [
        hydrated.text,
        row.action_json["text"],
    ]


def _failure_category_with_run_text() -> list[dict[str, object]]:
    # A block that copies its failure_reason into the entry's reasoning, as the web search block does.
    return [
        {
            "category": "AUTHENTICATION_FAILURE",
            "confidence_float": 0.9,
            "reason_code": "LOGIN_FAILED",
            "reasoning": f"Login as {_RUN_SECRET} failed",
        }
    ]


def _assert_failure_category_machine_codes_kept(stored: object) -> None:
    assert isinstance(stored, list)
    assert {key: stored[0][key] for key in ("category", "confidence_float", "reason_code")} == {
        "category": "AUTHENTICATION_FAILURE",
        "confidence_float": 0.9,
        "reason_code": "LOGIN_FAILED",
    }


async def _scrub_workflow_run_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    await db.workflow_runs.update_workflow_run(
        _SCRUB_RUN_ID,
        status=WorkflowRunStatus.failed,
        failure_reason=f"Login as {_RUN_SECRET} failed",
        failure_category=_failure_category_with_run_text(),
    )
    await db.workflow_run_attempts.create_attempt(_SCRUB_RUN_ID, org, attempt_number=1, status="running")
    await db.workflow_run_attempts.finalize_attempt(
        _SCRUB_RUN_ID,
        1,
        status="failed",
        failure_reason=f"Login as {_RUN_SECRET} failed",
        failure_category=_failure_category_with_run_text(),
        error_codes=["LOGIN_FAILED", _RUN_SECRET],
        finished_at=datetime.now(timezone.utc),
        retry_decision="final",
        decision_reason=None,
        next_attempt_at=None,
    )
    run = await db.workflow_runs.get_workflow_run(_SCRUB_RUN_ID, organization_id=org)
    assert run is not None
    (attempt,) = await db.workflow_run_attempts.get_attempts(_SCRUB_RUN_ID)
    # Machine error codes that retry policy reads back are stored as given.
    assert attempt.error_codes == ["LOGIN_FAILED", _RUN_SECRET]
    _assert_failure_category_machine_codes_kept(run.failure_category)
    _assert_failure_category_machine_codes_kept(attempt.failure_category)
    return [run.failure_reason, attempt.failure_reason, run.failure_category, attempt.failure_category], [
        attempt.error_codes
    ]


async def _scrub_block_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    block = await db.observer.create_workflow_run_block(
        workflow_run_id=_SCRUB_RUN_ID,
        organization_id=org,
        block_type=BlockType.FOR_LOOP,
        current_value=_RUN_SECRET,
    )
    updated = await db.observer.update_workflow_run_block(
        block.workflow_run_block_id,
        organization_id=org,
        output={"rows": [{"email": _RUN_SECRET, "count": 2}]},
        failure_reason=f"Loop over {_RUN_SECRET} failed",
        loop_values=[{"email": _RUN_SECRET}],
        prompt=f"Summarize {_RUN_SECRET}",
    )
    assert updated.output == {"rows": [{"email": REDACTED_SECRET_PLACEHOLDER, "count": 2}]}
    assert updated.loop_values == [{"email": REDACTED_SECRET_PLACEHOLDER}]
    return [block.current_value, updated.output, updated.failure_reason, updated.loop_values, updated.prompt], []


async def _scrub_thought_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    thought = await db.observer.create_thought(
        task_v2_id="oc_scrub",
        workflow_run_id=_SCRUB_RUN_ID,
        organization_id=org,
        user_input=f"Log in as {_RUN_SECRET}",
        thought=f"Use {_RUN_SECRET}",
        output={"answer": _RUN_SECRET},
    )
    updated = await db.observer.update_thought(
        thought.observer_thought_id, organization_id=org, observation=f"Saw {_RUN_SECRET}", answer=_RUN_SECRET
    )
    assert updated.output == {"answer": REDACTED_SECRET_PLACEHOLDER}
    return [updated.user_input, updated.thought, updated.output, updated.observation, updated.answer], []


async def _scrub_fallback_episode_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    episode = await db.scripts.create_fallback_episode(
        organization_id=org,
        workflow_permanent_id="wpid_scrub",
        workflow_run_id=_SCRUB_RUN_ID,
        block_label="login",
        fallback_type="element",
        error_message=f"Selector for {_RUN_SECRET} not found",
    )
    stored = await db.scripts.get_fallback_episode(episode.episode_id, org)
    assert stored is not None
    return [stored.error_message], []


async def _scrub_task_run_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    # The run's context does not exist yet when its task run row is written, so a row write must not warn.
    run_data_scrub._RUNS_WARNED_MISSING_CONTEXT.clear()
    with capture_logs() as logs:
        await db.tasks.create_task_run(
            task_run_type=RunType.workflow_run,
            organization_id=org,
            run_id="wr_without_context",
            title=f"Run for {_RUN_SECRET}",
        )
    assert not [log for log in logs if log["event"].startswith("No live run context")], logs
    await db.tasks.create_task_run(
        task_run_type=RunType.workflow_run, organization_id=org, run_id=_SCRUB_RUN_ID, title=f"Run for {_RUN_SECRET}"
    )
    async with db.Session() as session:
        row = (await session.scalars(select(TaskRunModel).filter_by(run_id=_SCRUB_RUN_ID))).one()
    return [row.searchable_text], [row.title]


async def _scrub_task_generation_row(db: AgentDB, org: str) -> tuple[list[object], list[object]]:
    skyvern_context.set(SkyvernContext(organization_id=org, workflow_run_id=_SCRUB_RUN_ID))
    generation = await db.workflow_params.create_task_generation(
        organization_id=org,
        user_prompt="Log in",
        user_prompt_hash="hash_scrub",
        llm_prompt=f"Log in as {_RUN_SECRET}",
        llm_response=f'{{"payload": "{_RUN_SECRET}"}}',
    )
    return [generation.llm_prompt, generation.llm_response], []


# Each row writes a run's text through its repository method and returns what the readers get back: the values that
# must be scrubbed, and the raw-exception values that later blocks read back as input and must keep the secret.
_RUN_TEXT_WRITES: dict[str, Callable[[AgentDB, str], Awaitable[tuple[list[object], list[object]]]]] = {
    "tasks": _scrub_task_row,
    "tasks_when_the_lookup_fails": _scrub_task_row_when_the_lookup_fails,
    "actions": _scrub_action_row,
    "workflow_runs_and_attempts": _scrub_workflow_run_row,
    "workflow_run_blocks": _scrub_block_row,
    "observer_thoughts": _scrub_thought_row,
    "script_fallback_episodes": _scrub_fallback_episode_row,
    "task_runs": _scrub_task_run_row,
    "task_generations": _scrub_task_generation_row,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("write", list(_RUN_TEXT_WRITES))
async def test_run_text_writes_store_the_runs_secrets_scrubbed(
    agent_db: AgentDB,
    monkeypatch: pytest.MonkeyPatch,
    workflow_context_manager_factory: Callable[..., WorkflowContextManager],
    write: str,
) -> None:
    monkeypatch.setattr(run_data_scrub.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    monkeypatch.setattr(run_data_scrub.settings, "ENABLE_ARTIFACT_VALUE_SCRUB_ALL_RUNS", True)
    # Mask Secrets off: the all-runs tier, the one every run gets.
    monkeypatch.setattr(
        run_data_scrub.app,
        "WORKFLOW_CONTEXT_MANAGER",
        workflow_context_manager_factory(
            workflow_run_id=_SCRUB_RUN_ID, mask_secrets=False, secrets={"password": _RUN_SECRET}
        ),
    )
    organization = await agent_db.organizations.create_organization(organization_name="Scrub organization")
    org = organization.organization_id
    workflow = await agent_db.workflows.create_workflow(
        title="wf", workflow_definition={"parameters": [], "blocks": []}, organization_id=org
    )
    await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id=workflow.workflow_permanent_id,
        workflow_id=workflow.workflow_id,
        organization_id=org,
        workflow_run_id=_SCRUB_RUN_ID,
    )
    skyvern_context.reset()
    try:
        scrubbed, raw = await _RUN_TEXT_WRITES[write](agent_db, org)
    finally:
        skyvern_context.reset()

    for value in scrubbed:
        assert _RUN_SECRET not in json.dumps(value)
        assert REDACTED_SECRET_PLACEHOLDER in json.dumps(value)
    for value in raw:
        assert _RUN_SECRET in json.dumps(value)


@pytest.mark.asyncio
async def test_block_bulk_update_scopes_to_the_given_organization_and_none_matches_every_block(
    agent_db: AgentDB,
) -> None:
    async with agent_db.Session() as session:
        session.add_all(
            WorkflowRunBlockModel(
                workflow_run_block_id=block_id,
                workflow_run_id="wr_shared",
                organization_id=organization_id,
                status="running",
                block_type="task",
            )
            for block_id, organization_id in (("wrb_own", "o_own"), ("wrb_other", "o_other"), ("wrb_null", None))
        )
        await session.commit()

    async def statuses() -> dict[str, str]:
        async with agent_db.Session() as session:
            rows = (await session.execute(select(WorkflowRunBlockModel))).scalars().all()
            return {row.workflow_run_block_id: row.status for row in rows}

    update = agent_db.observer.bulk_update_workflow_run_blocks_by_workflow_run_id
    scoped = await update(
        workflow_run_id="wr_shared", organization_id="o_own", new_status="timed_out", only_if_status_in=["running"]
    )
    assert scoped == 1
    assert await statuses() == {"wrb_own": "timed_out", "wrb_other": "running", "wrb_null": "running"}

    unscoped = await update(workflow_run_id="wr_shared", new_status="timed_out", only_if_status_in=["running"])
    assert unscoped == 2
    assert set((await statuses()).values()) == {"timed_out"}
