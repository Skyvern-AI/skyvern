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
