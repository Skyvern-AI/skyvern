"""Tests for all OSS repository instantiations + dependency injection."""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


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
