"""Conftest for embedded mode tests.

This conftest intentionally does NOT import from cloud/.
Embedded tests must work without cloud module side effects.

The _restore_settings fixture is REQUIRED because pytest runs all tests
in a single process. Without it, _apply_sqlite_overrides() changes
DATABASE_STRING to SQLite, and subsequent scenario tests (which expect
Postgres) fail with "no such table: organizations".
"""

import os

import pytest

from skyvern.library.embedded_server_factory import _restore_settings, _snapshot_settings


@pytest.fixture(autouse=True)
def _restore_settings_fixture():  # type: ignore[no-untyped-def]  # pytest fixture
    """Save and restore global settings + forge app around each embedded test.

    Uses the shared snapshot/restore helpers from embedded_server_factory so
    that the key list stays in sync with what _apply_sqlite_overrides mutates.
    """
    from skyvern.forge import app as forge_app_holder

    snapshots = _snapshot_settings()
    prev_app_inst = object.__getattribute__(forge_app_holder, "_inst")  # type: ignore[arg-type]
    prev_api_key = os.environ.get("SKYVERN_API_KEY")

    yield

    _restore_settings(snapshots)
    forge_app_holder.set_app(prev_app_inst)  # type: ignore[attr-defined]
    if prev_api_key is None:
        os.environ.pop("SKYVERN_API_KEY", None)
    else:
        os.environ["SKYVERN_API_KEY"] = prev_api_key


@pytest.fixture(autouse=True)
def _reset_mcp_session_contextvar():  # type: ignore[no-untyped-def]  # pytest fixture
    """Reset the mcp_session ContextVar in pytest's main context before each test.

    pytest-asyncio runs the async browser fixture and the test body in separate
    asyncio tasks. Each task gets its own copy of the parent ContextVars context
    at creation, so ContextVar.set() in the fixture does NOT propagate to the
    test task. The fixture relies on the _global_session module-level fallback
    in get_current_session() to bridge the two, but that fallback only fires
    when _current_session.get() returns None. Any other test that leaves a
    populated SessionState in the pytest main context poisons every later
    async test task and defeats the fallback.
    """
    from skyvern.cli.core import session_manager

    token = session_manager._current_session.set(None)
    try:
        yield
    finally:
        session_manager._current_session.reset(token)
