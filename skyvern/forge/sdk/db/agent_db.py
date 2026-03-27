import asyncio
from typing import Any

import structlog
from sqlalchemy import (
    event,
    pool,
)
from sqlalchemy.exc import (
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.config import settings
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.mixins.artifacts import ArtifactsMixin
from skyvern.forge.sdk.db.mixins.browser_sessions import BrowserSessionsMixin
from skyvern.forge.sdk.db.mixins.credentials import CredentialsMixin
from skyvern.forge.sdk.db.mixins.debug import DebugMixin
from skyvern.forge.sdk.db.mixins.folders import FoldersMixin
from skyvern.forge.sdk.db.mixins.observer import ObserverMixin
from skyvern.forge.sdk.db.mixins.organizations import OrganizationsMixin
from skyvern.forge.sdk.db.mixins.otp import OTPMixin
from skyvern.forge.sdk.db.mixins.schedules import ScheduleLimitExceededError, SchedulesMixin
from skyvern.forge.sdk.db.mixins.scripts import ScriptsMixin
from skyvern.forge.sdk.db.mixins.tasks import TasksMixin
from skyvern.forge.sdk.db.mixins.workflow_parameters import WorkflowParametersMixin
from skyvern.forge.sdk.db.mixins.workflow_runs import WorkflowRunsMixin
from skyvern.forge.sdk.db.mixins.workflows import WorkflowsMixin
from skyvern.forge.sdk.db.utils import (
    _custom_json_serializer,
)

LOG = structlog.get_logger()


def _build_engine(database_string: str) -> AsyncEngine:
    """
    Build a SQLAlchemy async engine.

    Supports both PostgreSQL and SQLite (via aiosqlite) dialects.

    PostgreSQL behaviour:
      When DISABLE_CONNECTION_POOL=True (NullPool): enforce statement_timeout
      and allow prepared statements.
      When DISABLE_CONNECTION_POOL=False (QueuePool): disable prepared statements
      and do not set statement_timeout - set at role level in the database,
      since the transaction pooler does not maintain session-level settings.

    SQLite behaviour:
      For :memory: databases, uses StaticPool to keep the single connection alive.
      For file-backed databases, enables WAL mode for concurrent read support.
      Always enables foreign key enforcement via PRAGMA.
    """
    if database_string.startswith("sqlite"):
        is_memory = ":memory:" in database_string
        engine_kwargs: dict[str, Any] = {
            "json_serializer": _custom_json_serializer,
        }
        if is_memory:
            engine_kwargs["poolclass"] = pool.StaticPool
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_async_engine(database_string, **engine_kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if not is_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    # PostgreSQL path (unchanged)
    connect_args: dict[str, Any] = {}
    if settings.DISABLE_CONNECTION_POOL:
        if "postgresql+psycopg" in database_string:
            connect_args["options"] = f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
        if "postgresql+asyncpg" in database_string:
            connect_args["server_settings"] = {"statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)}
        return create_async_engine(
            database_string,
            json_serializer=_custom_json_serializer,
            connect_args=connect_args,
            poolclass=pool.NullPool,
        )

    else:
        if "postgresql+psycopg" in database_string:
            connect_args["prepare_threshold"] = None
        if "postgresql+asyncpg" in database_string:
            connect_args["statement_cache_size"] = 0
        return create_async_engine(
            database_string,
            json_serializer=_custom_json_serializer,
            connect_args=connect_args,
            pool_pre_ping=True,
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_POOL_MAX_OVERFLOW,
        )


__all__ = ["AgentDB", "ScheduleLimitExceededError"]


class AgentDB(
    TasksMixin,
    WorkflowsMixin,
    WorkflowRunsMixin,
    WorkflowParametersMixin,
    SchedulesMixin,
    ArtifactsMixin,
    BrowserSessionsMixin,
    ScriptsMixin,
    OTPMixin,
    CredentialsMixin,
    FoldersMixin,
    OrganizationsMixin,
    ObserverMixin,
    DebugMixin,
    BaseAlchemyDB,
):
    def __init__(self, database_string: str, debug_enabled: bool = False, db_engine: AsyncEngine | None = None) -> None:
        super().__init__(db_engine or _build_engine(database_string))
        self.debug_enabled = debug_enabled
        # Global lock for SQLite schedule serialization. Unlike Postgres advisory locks
        # (which are scoped per org:workflow via hashtext(key)), this serializes ALL
        # schedule creates across all workflows. Acceptable for single-user embedded mode.
        self._sqlite_schedule_lock: asyncio.Lock | None = (
            asyncio.Lock() if self.engine.dialect.name == "sqlite" else None
        )

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        error_msg = str(error).lower()
        return "server closed the connection" in error_msg
