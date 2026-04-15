"""Smoke tests: agent_db exports remain importable and consistent.

Guards against:
- Phantom late imports being re-introduced for non-existent circular dependencies
- Re-export contracts breaking (ScheduleLimitExceededError used by cloud/routes/)
"""

from skyvern.forge.sdk.db.base_repository import BaseRepository


def test_agent_db_exports_schedule_limit_exceeded_error() -> None:
    """ScheduleLimitExceededError must be importable from agent_db (re-export contract)."""
    from skyvern.forge.sdk.db.agent_db import ScheduleLimitExceededError

    # Verify it's the canonical class from exceptions.py, not a shadow
    from skyvern.forge.sdk.db.exceptions import ScheduleLimitExceededError as Original

    assert ScheduleLimitExceededError is Original


def test_agent_db_exports_agent_db_class() -> None:
    """AgentDB must be importable from agent_db."""
    from skyvern.forge.sdk.db.agent_db import AgentDB

    assert AgentDB is not None


def test_all_repositories_on_agent_db() -> None:
    """All 14 domain repositories must be present as typed attributes on AgentDB.

    After the mixin-to-repository refactor, AgentDB uses composition instead
    of inheritance. This test verifies every domain repository is wired up
    by checking that __init__ assigns BaseRepository instances to each expected name.
    """
    from skyvern.forge.sdk.db.agent_db import AgentDB

    expected_repos = [
        "tasks",
        "workflows",
        "workflow_runs",
        "workflow_params",
        "schedules",
        "artifacts",
        "browser_sessions",
        "scripts",
        "otp",
        "credentials",
        "folders",
        "organizations",
        "observer",
        "debug",
    ]
    # Instantiate with a dummy database string (sqlite in-memory)
    db = AgentDB("sqlite+aiosqlite:///", debug_enabled=False)
    for repo in expected_repos:
        assert hasattr(db, repo), f"Repository '{repo}' missing from AgentDB instance"
        assert isinstance(getattr(db, repo), BaseRepository), (
            f"AgentDB.{repo} should be a BaseRepository, got {type(getattr(db, repo))}"
        )
