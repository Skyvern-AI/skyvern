"""Smoke tests: agent_db exports remain importable and consistent.

Guards against:
- Phantom late imports being re-introduced for non-existent circular dependencies
- Re-export contracts breaking (ScheduleLimitExceededError used by cloud/routes/)
"""


def test_agent_db_exports_schedule_limit_exceeded_error() -> None:
    """ScheduleLimitExceededError must be importable from agent_db (re-export contract)."""
    from skyvern.forge.sdk.db.agent_db import ScheduleLimitExceededError

    # Verify it's the canonical class, not a shadow
    from skyvern.forge.sdk.db.mixins.schedules import ScheduleLimitExceededError as Original

    assert ScheduleLimitExceededError is Original


def test_agent_db_exports_agent_db_class() -> None:
    """AgentDB must be importable from agent_db."""
    from skyvern.forge.sdk.db.agent_db import AgentDB

    assert AgentDB is not None


def test_all_mixins_in_agent_db_mro() -> None:
    """All 14 domain mixins must appear in AgentDB's MRO.

    If someone re-introduces a late import for any mixin, this test
    catches it because the mixin won't be in the class hierarchy.
    """
    from skyvern.forge.sdk.db.agent_db import AgentDB

    mro_names = {cls.__name__ for cls in AgentDB.__mro__}
    expected_mixins = [
        "TasksMixin",
        "WorkflowsMixin",
        "WorkflowRunsMixin",
        "WorkflowParametersMixin",
        "SchedulesMixin",
        "ArtifactsMixin",
        "BrowserSessionsMixin",
        "ScriptsMixin",
        "OTPMixin",
        "CredentialsMixin",
        "FoldersMixin",
        "OrganizationsMixin",
        "ObserverMixin",
        "DebugMixin",
    ]
    for name in expected_mixins:
        assert name in mro_names, f"{name} missing from AgentDB MRO"
