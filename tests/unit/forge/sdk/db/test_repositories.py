"""Tests for all OSS repository instantiations + dependency injection."""

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from skyvern.forge.sdk.db.models import OrganizationModel, TaskModel
from skyvern.forge.sdk.db.repositories.organizations import OrganizationsRepository
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session


def test_credential_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository

    mock_session = MagicMock()
    repo = CredentialRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_credential")
    assert hasattr(repo, "get_credential")
    assert hasattr(repo, "get_credentials")
    assert hasattr(repo, "update_credential")
    assert hasattr(repo, "delete_credential")
    assert hasattr(repo, "create_organization_bitwarden_collection")
    assert hasattr(repo, "get_organization_bitwarden_collection")


def test_credential_folders_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.credential_folders import CredentialFoldersRepository

    mock_session = MagicMock()
    repo = CredentialFoldersRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_credential_folder")
    assert hasattr(repo, "get_credential_folder")
    assert hasattr(repo, "get_credential_folders")
    assert hasattr(repo, "update_credential_folder")
    assert hasattr(repo, "soft_delete_credential_folder")
    assert hasattr(repo, "get_credential_folder_credential_count")
    assert hasattr(repo, "get_credential_folder_credential_counts_batch")
    assert hasattr(repo, "set_credential_folder")


def test_otp_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.otp import OTPRepository

    mock_session = MagicMock()
    repo = OTPRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "get_otp_codes")
    assert hasattr(repo, "create_otp_code")


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

        async def refresh(self, obj):
            obj.totp_code_id = "otp_test"
            obj.created_at = obj.modified_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

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

        async def refresh(self, obj):
            obj.totp_code_id = "otp_raw"
            obj.created_at = obj.modified_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

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


def test_debug_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.debug import DebugRepository

    mock_session = MagicMock()
    repo = DebugRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "get_debug_session")
    assert hasattr(repo, "create_debug_session")
    assert hasattr(repo, "create_block_run")


def test_organizations_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.organizations import OrganizationsRepository

    mock_session = MagicMock()
    repo = OrganizationsRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "get_organization")
    assert hasattr(repo, "create_organization")
    assert hasattr(repo, "create_org_auth_token")
    assert hasattr(repo, "validate_org_auth_token")


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


def test_schedules_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.schedules import SchedulesRepository

    mock_session = MagicMock()
    repo = SchedulesRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_workflow_schedule")
    assert hasattr(repo, "get_workflow_schedules")


def test_scripts_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.scripts import ScriptsRepository

    mock_session = MagicMock()
    repo = ScriptsRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_script")
    assert hasattr(repo, "get_scripts")
    assert hasattr(repo, "soft_delete_workflow_script_if_matches")
    assert hasattr(repo, "restore_workflow_script_if_matches")


def test_self_heal_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.self_heal import SelfHealRepository

    mock_session = MagicMock()
    repo = SelfHealRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_heal_episode")
    assert hasattr(repo, "get_heal_episodes")
    assert hasattr(repo, "create_heal_proposal")
    assert hasattr(repo, "get_heal_proposals")
    assert hasattr(repo, "update_heal_proposal_status")


def test_workflow_parameters_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository

    mock_session = MagicMock()
    repo = WorkflowParametersRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "get_workflow_parameter")
    assert hasattr(repo, "create_workflow_parameter")


def test_tasks_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.tasks import TasksRepository

    mock_session = MagicMock()
    repo = TasksRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_task")
    assert hasattr(repo, "get_task")
    assert hasattr(repo, "create_step")


def test_workflows_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository

    mock_session = MagicMock()
    repo = WorkflowsRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "get_workflow")
    assert hasattr(repo, "create_workflow")
    assert hasattr(repo, "get_workflow_by_permanent_id")
    assert hasattr(repo, "update_workflow_dispatch_state_if_latest_with_previous")
    assert hasattr(repo, "restore_workflow_script_dispatch_if_matches")


def test_browser_sessions_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository

    mock_session = MagicMock()
    repo = BrowserSessionsRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_browser_profile")
    assert hasattr(repo, "get_browser_profile")
    assert hasattr(repo, "update_browser_profile")
    assert hasattr(repo, "delete_browser_profile")


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
async def test_task_finish_claim_is_exactly_once_across_racing_finalizers(sqlite_engine: AsyncEngine):
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
    started = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _seed(task_id: str) -> None:
        async with factory() as session:
            session.add(
                TaskModel(
                    task_id=task_id,
                    organization_id="o_race",
                    status=TaskStatus.running.value,
                    url="https://example.test/",
                    started_at=started,
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

    for background_sync in list(repo._background_tasks):
        await background_sync


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

    _, rerun_claim = await repo.update_task_and_claim_finish(
        "tsk_rerun", status=TaskStatus.completed, organization_id="o_rerun"
    )
    assert rerun_claim is True

    for background_sync in list(repo._background_tasks):
        await background_sync
