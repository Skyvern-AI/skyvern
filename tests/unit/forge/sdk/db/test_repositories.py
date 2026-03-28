"""Tests for all 14 OSS repository instantiations + dependency injection."""

from unittest.mock import MagicMock, patch


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


def test_otp_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.otp import OTPRepository

    mock_session = MagicMock()
    repo = OTPRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "get_otp_codes")
    assert hasattr(repo, "create_otp_code")


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


def test_browser_sessions_repository_instantiation():
    from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository

    mock_session = MagicMock()
    repo = BrowserSessionsRepository(session_factory=mock_session, debug_enabled=False)
    assert repo.Session is mock_session
    assert hasattr(repo, "create_browser_profile")
    assert hasattr(repo, "get_browser_profile")


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
    from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
    from skyvern.forge.sdk.db.repositories.tasks import TasksRepository

    with patch("skyvern.forge.sdk.db.agent_db.create_async_engine"):
        from skyvern.forge.sdk.db.agent_db import AgentDB

        db = AgentDB("postgresql+asyncpg://test", debug_enabled=True)
        assert isinstance(db.tasks, TasksRepository)
        assert isinstance(db.credentials, CredentialRepository)
        assert hasattr(db, "get_task")  # backward compat delegate
        assert hasattr(db, "create_workflow")


def test_agent_db_delegates_route_to_repositories():
    """Verify delegate methods actually forward to the correct repository."""
    from unittest.mock import AsyncMock
    from unittest.mock import patch as mock_patch

    with mock_patch("skyvern.forge.sdk.db.agent_db.create_async_engine"):
        from skyvern.forge.sdk.db.agent_db import AgentDB

        db = AgentDB("postgresql+asyncpg://test", debug_enabled=False)

    # Patch a method on each major repository and verify the delegate calls it
    delegates_to_check = [
        ("get_task", "tasks"),
        ("create_workflow", "workflows"),
        ("create_artifact", "artifacts"),
        ("get_organization", "organizations"),
        ("get_credential", "credentials"),
        ("create_workflow_run", "workflow_runs"),
    ]

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        for delegate_name, repo_attr in delegates_to_check:
            repo = getattr(db, repo_attr)
            mock_method = AsyncMock(return_value="sentinel")
            original = getattr(repo, delegate_name)
            setattr(repo, delegate_name, mock_method)
            try:
                result = loop.run_until_complete(getattr(db, delegate_name)("arg1", key="val"))
                mock_method.assert_called_once_with("arg1", key="val")
                assert result == "sentinel", f"Delegate {delegate_name} did not return repository result"
            finally:
                setattr(repo, delegate_name, original)
    finally:
        loop.close()
