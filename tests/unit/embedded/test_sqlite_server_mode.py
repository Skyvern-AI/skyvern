"""Tests for SQLite-first server mode.

Covers:
- _default_database_string() returns SQLite path under ~/.skyvern/
- Settings.is_sqlite() detection
- SQLite bootstrap in api_app lifespan (tables, org, token, idempotency)
- Settings snapshot/restore on bootstrap failure
- ForgeApp restoration on bootstrap failure
"""

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from skyvern.config import _default_database_string


def test_default_database_string_is_sqlite() -> None:
    """_default_database_string returns a SQLite URL pointing at ~/.skyvern/data.db."""
    result = _default_database_string()
    assert result.startswith("sqlite+aiosqlite:///")
    assert ".skyvern/data.db" in result


def test_default_database_string_is_pure(tmp_path: Path) -> None:
    """_default_database_string is a pure string computation — no side effects."""
    fake_home = tmp_path / "fakehome"
    with patch("skyvern.config.Path.home", return_value=fake_home):
        result = _default_database_string()
    assert not (fake_home / ".skyvern").exists(), "factory must not create directories"
    assert ".skyvern/data.db" in result


def test_ensure_sqlite_dir_creates_directory(tmp_path: Path) -> None:
    """_ensure_sqlite_dir creates the parent directory for file-backed SQLite."""
    from skyvern.config import _ensure_sqlite_dir

    db_path = tmp_path / "subdir" / "data.db"
    _ensure_sqlite_dir(f"sqlite+aiosqlite:///{db_path}")
    assert (tmp_path / "subdir").is_dir()


def test_ensure_sqlite_dir_noop_for_memory() -> None:
    """_ensure_sqlite_dir is a no-op for in-memory SQLite."""
    from skyvern.config import _ensure_sqlite_dir

    _ensure_sqlite_dir("sqlite+aiosqlite:///:memory:")  # should not raise


def test_ensure_sqlite_dir_noop_for_postgres() -> None:
    """_ensure_sqlite_dir is a no-op for Postgres URLs."""
    from skyvern.config import _ensure_sqlite_dir

    _ensure_sqlite_dir("postgresql+psycopg://localhost/test")  # should not raise


def test_is_sqlite_true_for_sqlite_string() -> None:
    """is_sqlite() returns True when DATABASE_STRING starts with 'sqlite'."""
    from skyvern.config import Settings

    s = Settings(DATABASE_STRING="sqlite+aiosqlite:///test.db")
    assert s.is_sqlite() is True


def test_is_sqlite_false_for_postgres_string() -> None:
    """is_sqlite() returns False for PostgreSQL strings."""
    from skyvern.config import Settings

    s = Settings(DATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern")
    assert s.is_sqlite() is False


@pytest_asyncio.fixture
async def sqlite_bootstrap_db():  # type: ignore[no-untyped-def]  # pytest fixture
    """Swap in a disposable SQLite AgentDB for bootstrap tests."""
    from skyvern.forge import app as forge_app
    from skyvern.forge.sdk.db.agent_db import AgentDB

    db = AgentDB("sqlite+aiosqlite:///:memory:")
    original_db = forge_app.DATABASE
    forge_app.DATABASE = db  # type: ignore[assignment]
    try:
        yield db
    finally:
        forge_app.DATABASE = original_db  # type: ignore[assignment]
        await db.engine.dispose()


@pytest.fixture
def patched_env_writes():  # type: ignore[no-untyped-def]  # pytest fixture
    """Mock env-file writes so bootstrap tests do not touch the repo .env."""
    with patch("skyvern.forge.sdk.services.local_org_auth_token_service._write_env") as write_env:
        yield write_env


@pytest.mark.asyncio
async def test_sqlite_bootstrap_creates_tables_and_org(sqlite_bootstrap_db, patched_env_writes) -> None:
    """_bootstrap_sqlite creates tables, org, and API key in a SQLite DB."""
    from skyvern.forge.api_app import _bootstrap_sqlite
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.forge.sdk.db.models import Base
    from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN

    async with sqlite_bootstrap_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _bootstrap_sqlite()

    org = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    assert org is not None
    assert org.organization_name == "Skyvern-local"
    token = await sqlite_bootstrap_db.organizations.get_valid_org_auth_token(
        org.organization_id, OrganizationAuthTokenType.api
    )
    assert token is not None


@pytest.mark.asyncio
async def test_sqlite_bootstrap_is_idempotent(sqlite_bootstrap_db, patched_env_writes) -> None:
    """Calling _bootstrap_sqlite twice does not create duplicate orgs."""
    from skyvern.forge.api_app import _bootstrap_sqlite
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.forge.sdk.db.models import Base
    from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN

    async with sqlite_bootstrap_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _bootstrap_sqlite()
    org1 = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)

    # Second call should detect existing org and skip
    await _bootstrap_sqlite()
    org2 = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)

    assert org1 is not None
    assert org2 is not None
    assert org1.organization_id == org2.organization_id
    token = await sqlite_bootstrap_db.organizations.get_valid_org_auth_token(
        org1.organization_id, OrganizationAuthTokenType.api
    )
    assert token is not None


@pytest.mark.asyncio
async def test_sqlite_bootstrap_from_empty_db(sqlite_bootstrap_db, patched_env_writes) -> None:
    """_bootstrap_sqlite creates tables AND org from a completely empty DB.

    Unlike test_sqlite_bootstrap_creates_tables_and_org which pre-creates
    tables, this test starts from scratch to cover the full first-start path.
    """
    from skyvern.forge.api_app import _bootstrap_sqlite
    from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN

    # NO create_all — bootstrap must handle it

    await _bootstrap_sqlite()

    org = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    assert org is not None
    assert org.organization_name == "Skyvern-local"


@pytest.mark.asyncio
async def test_sqlite_bootstrap_syncs_existing_env_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sqlite_bootstrap_db,
    patched_env_writes,
) -> None:
    """An existing SKYVERN_API_KEY must become a valid token in a fresh SQLite DB."""
    from skyvern.forge.api_app import _bootstrap_sqlite
    from skyvern.forge.sdk.core.security import create_access_token
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN
    from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key

    monkeypatch.chdir(tmp_path)
    expected_org_id = "o_existing_local"
    existing_api_key = create_access_token(expected_org_id)
    monkeypatch.setenv("SKYVERN_API_KEY", existing_api_key)
    # Bootstrap reads settings.SKYVERN_API_KEY (the pydantic singleton), not os.environ directly
    monkeypatch.setattr("skyvern.config.settings.SKYVERN_API_KEY", existing_api_key)

    await _bootstrap_sqlite()

    org = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    assert org is not None
    assert org.organization_id == expected_org_id
    token = await sqlite_bootstrap_db.organizations.get_valid_org_auth_token(
        org.organization_id, OrganizationAuthTokenType.api
    )
    assert token is not None
    assert token.token == existing_api_key
    validation = await resolve_org_from_api_key(existing_api_key, sqlite_bootstrap_db)
    assert validation.organization.organization_id == expected_org_id
    patched_env_writes.assert_not_called()


@pytest.mark.asyncio
async def test_sqlite_bootstrap_repairs_existing_org_without_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sqlite_bootstrap_db,
    patched_env_writes,
) -> None:
    """Bootstrap should self-heal an existing local org that has no API token."""
    from skyvern.forge.api_app import _bootstrap_sqlite
    from skyvern.forge.sdk.core.security import create_access_token
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.forge.sdk.db.models import Base
    from skyvern.forge.sdk.services.local_org_auth_token_service import (
        SKYVERN_LOCAL_DOMAIN,
        ensure_local_org_with_id,
    )
    from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key

    monkeypatch.chdir(tmp_path)
    expected_org_id = "o_existing_local"
    existing_api_key = create_access_token(expected_org_id)
    monkeypatch.setenv("SKYVERN_API_KEY", existing_api_key)
    monkeypatch.setattr("skyvern.config.settings.SKYVERN_API_KEY", existing_api_key)

    async with sqlite_bootstrap_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org = await ensure_local_org_with_id(expected_org_id)
    assert org.organization_id
    assert (
        await sqlite_bootstrap_db.organizations.get_valid_org_auth_token(
            org.organization_id, OrganizationAuthTokenType.api
        )
        is None
    )

    await _bootstrap_sqlite()

    repaired_org = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    assert repaired_org is not None
    token = await sqlite_bootstrap_db.organizations.get_valid_org_auth_token(
        repaired_org.organization_id, OrganizationAuthTokenType.api
    )
    assert token is not None
    assert token.token == existing_api_key
    validation = await resolve_org_from_api_key(existing_api_key, sqlite_bootstrap_db)
    assert validation.organization.organization_id == expected_org_id
    patched_env_writes.assert_not_called()


@pytest.mark.asyncio
async def test_sqlite_bootstrap_regenerates_invalid_existing_env_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sqlite_bootstrap_db,
    patched_env_writes,
) -> None:
    """Bootstrap must replace an unusable env key with a valid local JWT."""
    from skyvern.forge.api_app import _bootstrap_sqlite
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.forge.sdk.services.local_org_auth_token_service import SKYVERN_LOCAL_DOMAIN
    from skyvern.forge.sdk.services.org_auth_service import resolve_org_from_api_key

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SKYVERN_API_KEY", "existing-test-key")

    await _bootstrap_sqlite()

    org = await sqlite_bootstrap_db.organizations.get_organization_by_domain(SKYVERN_LOCAL_DOMAIN)
    assert org is not None
    token = await sqlite_bootstrap_db.organizations.get_valid_org_auth_token(
        org.organization_id, OrganizationAuthTokenType.api
    )
    assert token is not None
    assert token.token != "existing-test-key"
    validation = await resolve_org_from_api_key(token.token, sqlite_bootstrap_db)
    assert validation.organization.organization_id == org.organization_id


@pytest.mark.asyncio
async def test_local_allows_env_only_persistent_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Skyvern.local() should honor env-only persistent mode without requiring ./.env."""
    from skyvern import Skyvern

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_STRING", "postgresql+psycopg://skyvern@localhost/skyvern")
    monkeypatch.setenv("SKYVERN_API_KEY", "dummy-key")

    embedded_client = httpx.AsyncClient()
    with patch(
        "skyvern.library.embedded_server_factory.create_embedded_server", return_value=embedded_client
    ) as factory:
        skyvern = Skyvern.local()

    try:
        assert factory.call_args.kwargs["use_in_memory_db"] is False
        assert getattr(skyvern, "_embedded_client") is embedded_client
    finally:
        await skyvern.aclose()


@pytest.mark.asyncio
async def test_local_persistent_mode_accepts_settings_without_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Persistent mode should accept DATABASE_STRING and SKYVERN_API_KEY via settings overrides."""
    from skyvern import Skyvern

    monkeypatch.chdir(tmp_path)

    embedded_client = httpx.AsyncClient()
    overrides = {
        "DATABASE_STRING": "postgresql+psycopg://skyvern@localhost/skyvern",
        "SKYVERN_API_KEY": "dummy-key",
    }
    with patch(
        "skyvern.library.embedded_server_factory.create_embedded_server", return_value=embedded_client
    ) as factory:
        skyvern = Skyvern.local(use_in_memory_db=False, settings=overrides)

    try:
        assert factory.call_args.kwargs["use_in_memory_db"] is False
        assert factory.call_args.kwargs["settings_overrides"] == overrides
        assert getattr(skyvern, "_embedded_client") is embedded_client
    finally:
        await skyvern.aclose()


@pytest.mark.asyncio
async def test_create_embedded_server_uses_settings_api_key_in_persistent_mode() -> None:
    """Persistent embedded bootstrap should read SKYVERN_API_KEY from settings overrides."""
    from skyvern.library.embedded_server_factory import create_embedded_server

    seen_headers: dict[bytes, bytes] = {}

    async def fake_app(scope, receive, send):  # type: ignore[no-untyped-def]
        nonlocal seen_headers
        seen_headers = dict(scope["headers"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"[]"})

    client = create_embedded_server(
        settings_overrides={"SKYVERN_API_KEY": "dummy-key"},
        use_in_memory_db=False,
    )
    try:
        with patch("skyvern.library.embedded_server_factory.create_api_app", return_value=fake_app):
            response = await client.get("/")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert seen_headers[b"x-api-key"] == b"dummy-key"


def test_settings_snapshot_restore_roundtrip() -> None:
    """Snapshot captures values and restore puts them back after mutation."""
    from skyvern.config import settings
    from skyvern.library.embedded_server_factory import _restore_settings, _snapshot_settings

    original_db = settings.DATABASE_STRING
    original_modules = settings.ADDITIONAL_MODULES[:]

    snapshots = _snapshot_settings()

    # Mutate settings
    settings.DATABASE_STRING = "sqlite+aiosqlite:///:memory:"
    settings.ADDITIONAL_MODULES = []

    # Restore
    _restore_settings(snapshots)

    assert settings.DATABASE_STRING == original_db
    assert settings.ADDITIONAL_MODULES == original_modules


def test_settings_snapshot_keys_cover_embedded_mutations() -> None:
    """Snapshot coverage should include both SQLite overrides and bootstrap-time mutations."""
    from skyvern.library.embedded_server_factory import (
        _BOOTSTRAP_RUNTIME_SETTINGS,
        _SETTINGS_SNAPSHOT_KEYS,
        _SQLITE_OVERRIDE_VALUES,
    )

    assert frozenset(_SQLITE_OVERRIDE_VALUES).issubset(_SETTINGS_SNAPSHOT_KEYS)
    assert _BOOTSTRAP_RUNTIME_SETTINGS.issubset(_SETTINGS_SNAPSHOT_KEYS)


@pytest.mark.asyncio
async def test_bootstrap_failure_restores_settings() -> None:
    """If bootstrap fails, settings must be restored to pre-bootstrap values."""
    from skyvern.config import settings

    original_db = settings.DATABASE_STRING

    from skyvern import Skyvern

    # Create a client with a bad setting that will cause validation error
    skyvern = Skyvern.local(
        use_in_memory_db=True,
        settings={"OTEL_ENABLED": True},  # Blocked setting
    )

    with pytest.raises(ValueError, match="Cannot override"):
        await skyvern.get_workflows()

    await skyvern.aclose()

    # Settings should be restored to original values
    assert settings.DATABASE_STRING == original_db


@pytest.mark.asyncio
async def test_bootstrap_failure_restores_forge_app() -> None:
    """If bootstrap fails, the forge app instance must be restored."""
    from skyvern.forge import app as forge_app_holder

    prev_inst = object.__getattribute__(forge_app_holder, "_inst")

    from skyvern import Skyvern

    skyvern = Skyvern.local(
        use_in_memory_db=True,
        settings={"OTEL_ENABLED": True},  # Blocked setting
    )

    with pytest.raises(ValueError, match="Cannot override"):
        await skyvern.get_workflows()

    await skyvern.aclose()

    current_inst = object.__getattribute__(forge_app_holder, "_inst")
    assert current_inst is prev_inst
