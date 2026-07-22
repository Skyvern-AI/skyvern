"""Unit tests for the minimal streaming-worker runtime.

The all-in-one container runs ``run_streaming.py`` alongside the main server.
That worker only reads run/task rows and writes screenshot files, so it must be
bootstrapped with a *minimal* app graph (``app.DATABASE`` + ``app.STORAGE``)
rather than the full ``create_forge_app()`` object graph (browser manager, LLM
clients/handlers, persistent-sessions manager, credential vaults, agent, ...).
"""

from threading import Lock
from types import SimpleNamespace

import pytest

from skyvern.forge import forge_app_initializer

# Heavyweight components that ``create_forge_app()`` constructs but the screenshot
# streaming worker never touches. If any of these show up on the streaming-worker
# app instance, the minimal-runtime guarantee has regressed.
HEAVY_COMPONENTS = [
    "REPLICA_DATABASE",
    "CACHE",
    "ARTIFACT_MANAGER",
    "BROWSER_MANAGER",
    "LLM_API_HANDLER",
    "OPENAI_CLIENT",
    "ANTHROPIC_CLIENT",
    "SECONDARY_LLM_API_HANDLER",
    "WORKFLOW_CONTEXT_MANAGER",
    "WORKFLOW_SERVICE",
    "AGENT_FUNCTION",
    "PERSISTENT_SESSIONS_MANAGER",
    "BROWSER_SESSION_RECORDING_SERVICE",
    "BITWARDEN_CREDENTIAL_VAULT_SERVICE",
    "agent",
]


def _stub_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the initializer from reconfiguring process-wide logging during tests.
    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_CONFIGURED", True)
    monkeypatch.setattr(forge_app_initializer, "_SERVER_LOGGING_LOCK", Lock())


def _spy_agent_db(monkeypatch: pytest.MonkeyPatch) -> tuple[object, list[tuple[str, bool]]]:
    calls: list[tuple[str, bool]] = []
    fake_db = SimpleNamespace(engine=SimpleNamespace())

    def _fake_agent_db(database_string: str, debug_enabled: bool = False) -> object:
        calls.append((database_string, debug_enabled))
        return fake_db

    monkeypatch.setattr(forge_app_initializer, "AgentDB", _fake_agent_db)
    return fake_db, calls


def test_start_streaming_worker_app_does_not_build_full_forge_app(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_logging(monkeypatch)
    fake_db, db_calls = _spy_agent_db(monkeypatch)

    create_forge_app_calls: list[int] = []
    monkeypatch.setattr(forge_app_initializer, "create_forge_app", lambda: create_forge_app_calls.append(1))

    installed: list[object] = []
    monkeypatch.setattr(forge_app_initializer, "set_force_app_instance", installed.append)

    app_instance = forge_app_initializer.start_streaming_worker_app()

    # The whole point: never build the full app graph.
    assert create_forge_app_calls == []
    # It constructs exactly one DB, wired from settings, and installs the app once.
    assert db_calls == [(forge_app_initializer.settings.DATABASE_STRING, forge_app_initializer.settings.DEBUG_MODE)]
    assert app_instance.DATABASE is fake_db
    assert installed == [app_instance]


def test_start_streaming_worker_app_only_sets_minimal_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_logging(monkeypatch)
    _spy_agent_db(monkeypatch)
    monkeypatch.setattr(forge_app_initializer, "set_force_app_instance", lambda inst: None)

    app_instance = forge_app_initializer.start_streaming_worker_app()

    # Minimal dependency surface the worker actually reads.
    assert hasattr(app_instance, "DATABASE")
    assert hasattr(app_instance, "STORAGE")
    assert app_instance.STORAGE is forge_app_initializer.StorageFactory.get_storage()

    # Everything heavy stays unconstructed (ForgeApp declares these as annotations
    # only, so an un-set attribute raises AttributeError -> hasattr is False).
    for component in HEAVY_COMPONENTS:
        assert not hasattr(app_instance, component), f"{component} must not be initialized in the streaming worker"


def test_start_streaming_worker_app_startup_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_logging(monkeypatch)

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(forge_app_initializer, "AgentDB", _boom)

    installed: list[object] = []
    monkeypatch.setattr(forge_app_initializer, "set_force_app_instance", installed.append)

    # A startup failure must surface (process fails to start), never be swallowed
    # into a silent screenshot loop, and must never install a half-built app.
    with pytest.raises(RuntimeError, match="database unreachable"):
        forge_app_initializer.start_streaming_worker_app()
    assert installed == []
