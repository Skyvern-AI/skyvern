"""Conftest for embedded mode tests.

This conftest intentionally does NOT import from cloud/.
Embedded tests must work without cloud module side effects.

The _restore_settings fixture is REQUIRED because pytest runs all tests
in a single process. Without it, _apply_sqlite_overrides() changes
DATABASE_STRING to SQLite, and subsequent scenario tests (which expect
Postgres) fail with "no such table: organizations".
"""

import copy

import pytest

from skyvern.config import settings


@pytest.fixture(autouse=True)
def _restore_settings():
    """Save and restore global settings around each embedded test.

    Embedded mode mutates the global settings singleton (DATABASE_STRING,
    ADDITIONAL_MODULES, OTEL_ENABLED, etc.). Without restoration, subsequent
    Postgres-based scenario tests in the same pytest process see SQLite
    settings and fail.
    """
    from skyvern.forge.sdk.settings_manager import SettingsManager

    keys_to_save = [
        "DATABASE_STRING",
        "DATABASE_REPLICA_STRING",
        "ADDITIONAL_MODULES",
        "OTEL_ENABLED",
        "LLM_KEY",
        "BROWSER_LOGS_ENABLED",
    ]

    original_values = {}
    for key in keys_to_save:
        val = getattr(settings, key, None)
        original_values[key] = copy.deepcopy(val) if isinstance(val, (list, dict)) else val

    mgr_originals = {}
    mgr_settings = SettingsManager.get_settings()
    if mgr_settings is not settings:
        for key in keys_to_save:
            if hasattr(mgr_settings, key):
                val = getattr(mgr_settings, key)
                mgr_originals[key] = copy.deepcopy(val) if isinstance(val, (list, dict)) else val

    yield

    for key, val in original_values.items():
        setattr(settings, key, val)
    if mgr_settings is not settings:
        for key, val in mgr_originals.items():
            setattr(mgr_settings, key, val)
