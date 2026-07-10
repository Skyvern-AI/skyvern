import asyncio
import shlex
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import (
    event,
    pool,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import (
    SQLAlchemyError,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from skyvern.config import settings
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.exceptions import ScheduleLimitExceededError  # noqa: F401
from skyvern.forge.sdk.db.repositories.artifacts import ArtifactsRepository
from skyvern.forge.sdk.db.repositories.browser_sessions import BrowserSessionsRepository
from skyvern.forge.sdk.db.repositories.credential_folders import CredentialFoldersRepository
from skyvern.forge.sdk.db.repositories.credentials import CredentialRepository
from skyvern.forge.sdk.db.repositories.debug import DebugRepository
from skyvern.forge.sdk.db.repositories.folders import FoldersRepository
from skyvern.forge.sdk.db.repositories.google_oauth import GoogleOAuthRepository
from skyvern.forge.sdk.db.repositories.observer import ObserverRepository
from skyvern.forge.sdk.db.repositories.organizations import OrganizationsRepository
from skyvern.forge.sdk.db.repositories.otp import OTPRepository
from skyvern.forge.sdk.db.repositories.schedules import SchedulesRepository
from skyvern.forge.sdk.db.repositories.scripts import ScriptsRepository
from skyvern.forge.sdk.db.repositories.self_heal import SelfHealRepository
from skyvern.forge.sdk.db.repositories.tags import TagsRepository
from skyvern.forge.sdk.db.repositories.tasks import TasksRepository
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.repositories.workflow_run_credential_selections import (
    WorkflowRunCredentialSelectionsRepository,
)
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.db.utils import (
    _custom_json_serializer,
)

LOG = structlog.get_logger()

# Transaction-pooler compatibility is intentionally scoped to standard :6543 deployments.
TRANSACTION_POOLER_PORT = 6543
_ENFORCING_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})


def _query_values(query: Mapping[str, "str | tuple[str, ...]"], key: str) -> tuple[str, ...]:
    value = query.get(key)
    if value is None:
        return ()
    values = value if isinstance(value, tuple) else (value,)
    return tuple(part for item in values for part in item.split(",") if part)


def _parse_port(port: str) -> int | None:
    try:
        return int(port)
    except ValueError:
        return None


def _query_host_port(host: str) -> int | None:
    if host.startswith("["):
        _, separator, port = host.rpartition("]:")
        if not separator:
            return None
        return _parse_port(port)

    _, separator, port = host.rpartition(":")
    if not separator:
        return None
    return _parse_port(port)


def _query_targets_transaction_pooler(query: Mapping[str, "str | tuple[str, ...]"]) -> bool:
    # Startup args cannot vary by fallback target, so use pooler-safe args if any
    # query-level fallback host may connect through the transaction pooler.
    host_ports = (_query_host_port(host) for host in _query_values(query, "host"))
    query_ports = (_parse_port(port) for port in _query_values(query, "port"))
    return TRANSACTION_POOLER_PORT in host_ports or TRANSACTION_POOLER_PORT in query_ports


def _query_pins_every_host_port(query: Mapping[str, "str | tuple[str, ...]"]) -> bool:
    # A query host without its own port inherits the authority port, so the authority
    # port only stops being a target when every query host names a port itself.
    hosts = _query_values(query, "host")
    if not hosts:
        return False
    if _query_values(query, "port"):
        return True
    return all(_query_host_port(host) is not None for host in hosts)


def _is_transaction_pooler(database_string: str) -> bool:
    try:
        database_url = make_url(database_string)
    except Exception:
        return False
    if _query_targets_transaction_pooler(database_url.query):
        return True
    if _query_pins_every_host_port(database_url.query):
        return False
    return database_url.port == TRANSACTION_POOLER_PORT


def _query_last_value(query: Mapping[str, "str | tuple[str, ...]"], key: str) -> str | None:
    value = query.get(key)
    if isinstance(value, tuple):
        value = value[-1] if value else None
    return value


def _has_enforcing_ssl(query: Mapping[str, "str | tuple[str, ...]"], key: str) -> bool:
    value = _query_last_value(query, key)
    return value in _ENFORCING_SSL_MODES


def _require_pooler_ssl(
    connect_args: dict[str, Any],
    query: Mapping[str, "str | tuple[str, ...]"],
    key: str,
    query_keys: tuple[str, ...] | None = None,
) -> None:
    query_keys = query_keys or (key,)
    if any(_has_enforcing_ssl(query, query_key) for query_key in query_keys):
        return

    connect_args[key] = "require"
    overridden_key = None
    overridden_value = None
    for query_key in query_keys:
        overridden_value = _query_last_value(query, query_key)
        if overridden_value is not None:
            overridden_key = query_key
            break

    if overridden_key is not None and overridden_value is not None:
        LOG.debug(
            "Overriding non-enforcing Postgres pooler SSL mode",
            ssl_key=overridden_key,
            ssl_mode=overridden_value,
            required_ssl_mode="require",
        )


def _postgres_options(query: Mapping[str, "str | tuple[str, ...]"]) -> str | None:
    existing_options = query.get("options")
    if isinstance(existing_options, tuple):
        existing_options = " ".join(existing_options)
    return existing_options


def _direct_psycopg_options(query: Mapping[str, "str | tuple[str, ...]"]) -> str:
    timeout_option = f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
    existing_options = _postgres_options(query)
    if existing_options:
        return f"{existing_options} {timeout_option}"
    return timeout_option


def _postgres_options_server_settings(query: Mapping[str, "str | tuple[str, ...]"]) -> dict[str, str]:
    options = _postgres_options(query)
    if not options:
        return {}

    try:
        option_parts = shlex.split(options)
    except ValueError:
        return {}

    server_settings: dict[str, str] = {}
    index = 0
    while index < len(option_parts):
        option = option_parts[index]
        setting = None
        if option == "-c" and index + 1 < len(option_parts):
            index += 1
            setting = option_parts[index]
        elif option.startswith("-c") and len(option) > 2:
            setting = option[2:]

        if setting and "=" in setting:
            key, value = setting.split("=", 1)
            if key:
                server_settings[key] = value
        index += 1

    return server_settings


def _direct_asyncpg_server_settings(query: Mapping[str, "str | tuple[str, ...]"]) -> dict[str, str]:
    server_settings = _postgres_options_server_settings(query)
    server_settings["statement_timeout"] = str(settings.DATABASE_STATEMENT_TIMEOUT_MS)
    return server_settings


def _asyncpg_prepared_statement_name() -> str:
    return f"__asyncpg_{uuid4()}__"


def _postgres_connect_args(database_string: str) -> dict[str, Any]:
    connect_args: dict[str, Any] = {}
    try:
        database_url = make_url(database_string)
    except Exception:
        database_url = None

    drivername = database_url.drivername if database_url else ""
    is_psycopg = drivername in {"postgresql+psycopg", "postgresql+psycopg_async"}
    is_asyncpg = drivername == "postgresql+asyncpg"

    if not _is_transaction_pooler(database_string):
        if is_psycopg:
            connect_args["options"] = _direct_psycopg_options(database_url.query if database_url else {})
        if is_asyncpg:
            connect_args["server_settings"] = _direct_asyncpg_server_settings(
                database_url.query if database_url else {}
            )
        return connect_args

    existing_query = database_url.query if database_url else {}
    if is_psycopg:
        connect_args["prepare_threshold"] = None
        _require_pooler_ssl(connect_args, existing_query, "sslmode")
    if is_asyncpg:
        # Disable both asyncpg's native prepared-statement cache and SQLAlchemy's
        # own per-connection cache; either one reuses prepared statements across
        # pooled server connections, which the transaction pooler can't honor.
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
        # SQLAlchemy/asyncpg call this zero-arg hook for each prepared statement.
        connect_args["prepared_statement_name_func"] = _asyncpg_prepared_statement_name
        _require_pooler_ssl(connect_args, existing_query, "ssl", query_keys=("ssl", "sslmode"))
    return connect_args


def _normalize_asyncpg_ssl_query(database_string: str, connect_args: dict[str, Any]) -> str:
    try:
        database_url = make_url(database_string)
    except Exception:
        return database_string

    if database_url.drivername != "postgresql+asyncpg":
        return database_string

    query = dict(database_url.query)
    sslmode = _query_last_value(query, "sslmode")
    if sslmode is None and "ssl" not in query:
        return database_string

    if sslmode is not None:
        query.pop("sslmode", None)

    if "ssl" in connect_args:
        query.pop("ssl", None)
    elif sslmode in _ENFORCING_SSL_MODES:
        connect_args["ssl"] = sslmode
        query.pop("ssl", None)
    elif sslmode is not None and "ssl" not in query:
        connect_args["ssl"] = sslmode

    return database_url.set(query=query).render_as_string(hide_password=False)


def _strip_postgres_options_query(database_string: str) -> str:
    try:
        database_url = make_url(database_string)
    except Exception:
        return database_string

    if "options" not in database_url.query:
        return database_string

    is_psycopg = database_url.drivername in {"postgresql+psycopg", "postgresql+psycopg_async"}
    is_asyncpg = database_url.drivername == "postgresql+asyncpg"
    if not _is_transaction_pooler(database_string) and not (is_psycopg or is_asyncpg):
        return database_string

    query = dict(database_url.query)
    query.pop("options", None)
    return database_url.set(query=query).render_as_string(hide_password=False)


def _postgres_engine_config(database_string: str) -> tuple[str, dict[str, Any]]:
    connect_args = _postgres_connect_args(database_string)
    database_string = _strip_postgres_options_query(database_string)
    return _normalize_asyncpg_ssl_query(database_string, connect_args), connect_args


def _build_engine(database_string: str) -> AsyncEngine:
    """
    Build a SQLAlchemy async engine.

    Supports both PostgreSQL and SQLite (via aiosqlite) dialects.

    PostgreSQL behaviour:
      Connect args are chosen by the connection target (see _postgres_connect_args):
      transaction poolers get SSL + no prepared statements + no startup options,
      direct connections get a session statement_timeout. DISABLE_CONNECTION_POOL
      independently selects NullPool (True) vs QueuePool (False).

    SQLite behaviour:
      For :memory: databases, uses StaticPool to keep the single connection alive.
      For file-backed databases, enables WAL mode for concurrent read support.
      Always enables foreign key enforcement via PRAGMA.
    """
    if database_string.startswith("sqlite"):
        from skyvern.config import _ensure_sqlite_dir

        _ensure_sqlite_dir(database_string)
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

    database_string, connect_args = _postgres_engine_config(database_string)
    if settings.DISABLE_CONNECTION_POOL:
        return create_async_engine(
            database_string,
            json_serializer=_custom_json_serializer,
            connect_args=connect_args,
            poolclass=pool.NullPool,
        )

    return create_async_engine(
        database_string,
        json_serializer=_custom_json_serializer,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_POOL_MAX_OVERFLOW,
        pool_timeout=settings.DATABASE_POOL_TIMEOUT,
        pool_recycle=settings.DATABASE_POOL_RECYCLE,
    )


__all__ = ["AgentDB", "ScheduleLimitExceededError"]


class AgentDB(BaseAlchemyDB):
    def __init__(self, database_string: str, debug_enabled: bool = False, db_engine: AsyncEngine | None = None) -> None:
        super().__init__(db_engine or _build_engine(database_string))
        self.debug_enabled = debug_enabled
        # Global lock for SQLite schedule serialization. Unlike Postgres advisory locks
        # (which are scoped per org:workflow via hashtext(key)), this serializes ALL
        # schedule creates across all workflows. Acceptable for single-user embedded mode.
        self._sqlite_schedule_lock: asyncio.Lock | None = (
            asyncio.Lock() if self.engine.dialect.name == "sqlite" else None
        )

        # -- Zero-dependency repositories --
        self.tasks = TasksRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.workflows = WorkflowsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.workflow_params = WorkflowParametersRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.workflow_run_credential_selections = WorkflowRunCredentialSelectionsRepository(
            self.Session, debug_enabled, self.is_retryable_error
        )
        self.credentials = CredentialRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.credential_folders = CredentialFoldersRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.otp = OTPRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.debug = DebugRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.organizations = OrganizationsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.scripts = ScriptsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.self_heal = SelfHealRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.tags = TagsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.browser_sessions = BrowserSessionsRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.google_oauth = GoogleOAuthRepository(self.Session, debug_enabled, self.is_retryable_error)
        self.schedules = SchedulesRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            sqlite_schedule_lock=self._sqlite_schedule_lock,
        )

        # -- Cross-dependency repositories --
        self.workflow_runs = WorkflowRunsRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            workflow_parameter_reader=self.workflow_params,
            dialect_name=self.engine.dialect.name,
        )
        self.artifacts = ArtifactsRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            run_reader=self.workflow_runs,
        )
        self.folders = FoldersRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            workflow_reader=self.workflows,
        )
        self.observer = ObserverRepository(
            self.Session,
            debug_enabled,
            self.is_retryable_error,
            task_reader=self.tasks,
        )

    def is_retryable_error(self, error: SQLAlchemyError) -> bool:
        error_msg = str(error).lower()
        return "server closed the connection" in error_msg
