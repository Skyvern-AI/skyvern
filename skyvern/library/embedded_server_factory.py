import copy
import os
import tempfile
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
from httpx import ASGITransport

from skyvern.config import settings
from skyvern.forge.api_app import create_api_app
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.models import LLMConfig, LLMRouterConfig

_EMBEDDED_ACTIVE: bool = False

_BLOCKED_EMBEDDED_SETTINGS = frozenset({"OTEL_ENABLED", "ENABLE_CLEANUP_CRON"})
_SQLITE_OVERRIDE_VALUES: dict[str, Any] = {
    "DATABASE_STRING": "sqlite+aiosqlite:///:memory:",
    "DATABASE_REPLICA_STRING": None,
    "ADDITIONAL_MODULES": [],
    "OTEL_ENABLED": False,
    "ENABLE_CLEANUP_CRON": False,
}
_BOOTSTRAP_RUNTIME_SETTINGS = frozenset({"LLM_KEY", "BROWSER_LOGS_ENABLED", "SKYVERN_API_KEY"})

# Keys mutated by _apply_sqlite_overrides and bootstrap-time request setup.
# SQLite override keys are derived from the actual override mapping so that
# snapshot coverage can't silently drift when new overrides are added.
# OTEL_ENABLED and ENABLE_CLEANUP_CRON overlap with _BLOCKED_EMBEDDED_SETTINGS —
# snapshotting them is a defensive no-op (blocked keys can't be mutated via
# settings_overrides, but _apply_sqlite_overrides sets them directly).
_SETTINGS_SNAPSHOT_KEYS = frozenset(_SQLITE_OVERRIDE_VALUES) | _BLOCKED_EMBEDDED_SETTINGS | _BOOTSTRAP_RUNTIME_SETTINGS


def _snapshot_settings() -> dict[str, dict[str, Any]]:
    """Capture current values of mutable settings keys across all targets."""
    from skyvern.forge.sdk.settings_manager import SettingsManager  # noqa: PLC0415

    snapshots: dict[str, dict[str, Any]] = {}
    targets = {"settings": settings}
    mgr = SettingsManager.get_settings()
    if mgr is not settings:
        targets["mgr"] = mgr

    for label, target in targets.items():
        snap: dict[str, Any] = {}
        for key in _SETTINGS_SNAPSHOT_KEYS:
            if hasattr(target, key):
                val = getattr(target, key)
                snap[key] = copy.deepcopy(val) if isinstance(val, (list, dict)) else val
        snapshots[label] = snap
    return snapshots


def _restore_settings(snapshots: dict[str, dict[str, Any]]) -> None:
    """Restore settings from a snapshot taken by _snapshot_settings."""
    from skyvern.forge.sdk.settings_manager import SettingsManager  # noqa: PLC0415

    targets = {"settings": settings}
    mgr = SettingsManager.get_settings()
    if mgr is not settings:
        targets["mgr"] = mgr

    for label, target in targets.items():
        if label in snapshots:
            for key, val in snapshots[label].items():
                setattr(target, key, val)


def create_embedded_server(
    llm_config: LLMRouterConfig | LLMConfig | None = None,
    settings_overrides: dict[str, Any] | None = None,
    use_in_memory_db: bool = False,
) -> httpx.AsyncClient:
    global _EMBEDDED_ACTIVE
    if _EMBEDDED_ACTIVE:
        raise RuntimeError(
            "An embedded Skyvern client is already active in this process. "
            "Call aclose() on the existing client before creating a new one."
        )
    _EMBEDDED_ACTIVE = True

    class EmbeddedServerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport: ASGITransport | None = None
            self._api_key: str | None = None
            self._artifact_dir: str | None = None
            self._llm_key: str | None = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self._transport is None:
                snapshots = _snapshot_settings()
                # Capture the current forge app instance so we can restore it
                # if bootstrap fails partway through (after create_api_app sets
                # a new instance but before bootstrap completes).
                from skyvern.forge import app as forge_app_holder  # noqa: PLC0415

                prev_app_inst = object.__getattribute__(forge_app_holder, "_inst")  # type: ignore[arg-type]
                try:
                    await self._bootstrap(request)
                except Exception as exc:
                    import structlog  # noqa: PLC0415

                    structlog.get_logger().exception(
                        "Embedded bootstrap failed; restoring previous process state",
                        error_type=type(exc).__name__,
                    )
                    # Restore settings so a retry (or subsequent tests in the
                    # same process) sees the original values, not half-applied
                    # SQLite overrides.
                    _restore_settings(snapshots)
                    # Restore the previous forge app instance
                    forge_app_holder.set_app(prev_app_inst)  # type: ignore[attr-defined]
                    # Reset the single-client guard so the user can retry
                    # after fixing the issue (e.g., missing API key).
                    global _EMBEDDED_ACTIVE
                    _EMBEDDED_ACTIVE = False
                    raise

            if self._api_key and "x-api-key" not in request.headers:
                request.headers["x-api-key"] = self._api_key

            assert self._transport is not None
            response = await self._transport.handle_async_request(request)
            return response

        async def _bootstrap(self, request: httpx.Request) -> None:
            """One-time lazy initialization of the embedded server."""
            settings.BROWSER_LOGS_ENABLED = False

            # Register custom LLM BEFORE create_api_app — create_forge_app()
            # reads settings.LLM_KEY to build handlers during startup.
            if llm_config:
                llm_key = f"CUSTOM_LLM_{uuid4().hex[:8]}"
                LLMConfigRegistry.register_config(llm_key, llm_config)
                settings.LLM_KEY = llm_key
                self._llm_key = llm_key

            # Validate and apply custom settings overrides
            if settings_overrides:
                blocked = set(settings_overrides) & _BLOCKED_EMBEDDED_SETTINGS
                if blocked:
                    raise ValueError(
                        f"Cannot override {blocked} in embedded mode — "
                        f"these require FastAPI lifespan which is not available under ASGITransport."
                    )
                for key, value in settings_overrides.items():
                    if hasattr(settings, key):
                        setattr(settings, key, value)
                    else:
                        raise ValueError(f"Invalid setting: {key}")

            if not use_in_memory_db:
                self._api_key = (
                    settings_overrides.get("SKYVERN_API_KEY") if settings_overrides is not None else None
                ) or os.getenv("SKYVERN_API_KEY")
                if not self._api_key:
                    raise ValueError("SKYVERN_API_KEY is not set. Provide api_key or set SKYVERN_API_KEY in .env file.")

            if use_in_memory_db:
                # Set overrides BEFORE create_api_app so that start_forge_app()
                # sees ADDITIONAL_MODULES=[] and skips cloud module loading.
                _apply_sqlite_overrides()

            api_app = create_api_app()

            if use_in_memory_db:
                self._api_key = await self._bootstrap_in_memory_db()

            self._transport = ASGITransport(app=api_app)

        async def _bootstrap_in_memory_db(self) -> str:
            """Create tables, org, and auth token for in-memory SQLite. Returns the API token."""
            from skyvern.forge import app as forge_app  # noqa: PLC0415
            from skyvern.forge.sdk.artifact.storage.factory import StorageFactory  # noqa: PLC0415
            from skyvern.forge.sdk.artifact.storage.local import LocalStorage  # noqa: PLC0415
            from skyvern.forge.sdk.core.security import create_access_token  # noqa: PLC0415
            from skyvern.forge.sdk.db.agent_db import AgentDB  # noqa: PLC0415
            from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType  # noqa: PLC0415
            from skyvern.forge.sdk.db.models import Base  # noqa: PLC0415

            # Only replace the engine if cloud/__init__.py swapped it to Postgres.
            # When _apply_sqlite_overrides() runs before create_api_app(), the engine
            # should already be SQLite. Replacing it unconditionally would create a
            # second :memory: database, discarding any state from app startup.
            if forge_app.DATABASE.engine.dialect.name != "sqlite":
                old_engine = forge_app.DATABASE.engine
                forge_app.DATABASE = AgentDB("sqlite+aiosqlite:///:memory:")
                forge_app.REPLICA_DATABASE = forge_app.DATABASE
                await old_engine.dispose()
            else:
                forge_app.REPLICA_DATABASE = forge_app.DATABASE

            # Rebuild LocalStorage with a temp directory for artifacts.
            # LocalStorage.__init__ captures ARTIFACT_STORAGE_PATH at import time,
            # so changing the setting later has no effect on the existing instance.
            self._artifact_dir = tempfile.mkdtemp(prefix="skyvern-artifacts-")
            StorageFactory.set_storage(LocalStorage(artifact_path=self._artifact_dir))
            forge_app.STORAGE = StorageFactory.get_storage()

            db = forge_app.DATABASE
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            org = await db.organizations.create_organization(organization_name="local")
            token = create_access_token(org.organization_id, expires_delta=timedelta(days=365 * 10))
            await db.organizations.create_org_auth_token(
                organization_id=org.organization_id,
                token_type=OrganizationAuthTokenType.api,
                token=token,
            )
            return token

        async def aclose(self) -> None:
            """Dispose embedded resources: SQLite engine, artifact dir, and transport."""
            global _EMBEDDED_ACTIVE
            try:
                from skyvern.forge import app as forge_app  # noqa: PLC0415

                if self._transport is not None and forge_app.DATABASE and forge_app.DATABASE.engine:
                    await forge_app.DATABASE.engine.dispose()
            except Exception:
                import structlog  # noqa: PLC0415

                structlog.get_logger().warning("Failed to dispose embedded engine", exc_info=True)

            if self._llm_key:
                LLMConfigRegistry.deregister_config(self._llm_key)
                self._llm_key = None

            if self._artifact_dir:
                import shutil  # noqa: PLC0415

                shutil.rmtree(self._artifact_dir, ignore_errors=True)
                self._artifact_dir = None

            self._transport = None
            self._api_key = None
            _EMBEDDED_ACTIVE = False

        def __del__(self) -> None:
            """Fallback: reset single-client guard if aclose() was never called."""
            global _EMBEDDED_ACTIVE
            if _EMBEDDED_ACTIVE:
                _EMBEDDED_ACTIVE = False

    transport = EmbeddedServerTransport()
    return EmbeddedClient(transport=transport, base_url="http://skyvern-embedded")


class EmbeddedClient(httpx.AsyncClient):
    """httpx.AsyncClient subclass that holds a typed reference to the embedded transport."""

    def __init__(self, transport: httpx.AsyncBaseTransport, **kwargs: Any) -> None:  # type: ignore[override]
        super().__init__(transport=transport, **kwargs)
        self.embedded_transport = transport


def _apply_sqlite_overrides() -> None:
    """Apply SQLite-required settings overrides to whichever Settings object is active.

    Uses a dual-target loop because Settings and SettingsManager.get_settings()
    may be different objects when cloud/__init__.py was previously imported
    (e.g., in a mixed test suite where scenario conftest imports cloud).
    """
    from skyvern.forge.sdk.settings_manager import SettingsManager  # noqa: PLC0415

    targets = [settings]
    mgr_settings = SettingsManager.get_settings()
    if mgr_settings is not settings:
        targets.append(mgr_settings)
    for target in targets:
        for key, value in _SQLITE_OVERRIDE_VALUES.items():
            if hasattr(target, key):
                copied = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
                setattr(target, key, copied)
