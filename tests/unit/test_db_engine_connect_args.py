"""Postgres engine connect-arg selection.

The args that a Postgres transaction pooler on :6543 tolerates are determined
by the connection *target*, not by SQLAlchemy's pool class. Regression cover for
the pooler `unsupported startup parameter: options` and `SSL required` failures.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest
from sqlalchemy import pool
from sqlalchemy.engine import make_url

from skyvern.config import settings
from skyvern.forge.sdk.db import agent_db
from skyvern.forge.sdk.db.agent_db import _is_transaction_pooler, _postgres_connect_args

POOLER_PSYCOPG = "postgresql+psycopg://user:pass@aws-0-us-east-1.pooler.example.com:6543/postgres"
DIRECT_PSYCOPG = "postgresql+psycopg://user:pass@db.example.com:5432/postgres"
POOLER_ASYNCPG = "postgresql+asyncpg://user:pass@aws-0-us-east-1.pooler.example.com:6543/postgres"
DIRECT_ASYNCPG = "postgresql+asyncpg://user:pass@db.example.com:5432/postgres"
POOLER_PSYCOPG2 = "postgresql+psycopg2://user:pass@aws-0-us-east-1.pooler.example.com:6543/postgres"
MULTI_HOST_POOLER_PSYCOPG = "postgresql+psycopg://user:pass@/postgres?host=pooler-a:6543&host=pooler-b:6543"
MULTI_HOST_POOLER_ASYNCPG = "postgresql+asyncpg://user:pass@/postgres?host=pooler-a:6543&host=pooler-b:6543"
MULTI_HOST_DIRECT_ASYNCPG = "postgresql+asyncpg://user:pass@/postgres?host=db-a:5432&host=db-b:5432"
COMMA_MULTI_HOST_POOLER_ASYNCPG = "postgresql+asyncpg://user:pass@/postgres?host=pooler-a,pooler-b&port=6543,6543"
COMMA_MULTI_HOST_DIRECT_ASYNCPG = "postgresql+asyncpg://user:pass@/postgres?host=db-a,db-b&port=5432,5432"
AUTHORITY_POOLER_QUERY_DIRECT_PSYCOPG = (
    "postgresql+psycopg://user:pass@ignored.example.com:6543/postgres?host=db-a:5432&host=db-b:5432"
)
AUTHORITY_POOLER_QUERY_DIRECT_ASYNCPG = (
    "postgresql+asyncpg://user:pass@ignored.example.com:6543/postgres?host=db-a:5432&host=db-b:5432"
)
POOLER_PSYCOPG_ASYNC = "postgresql+psycopg_async://user:pass@aws-0-us-east-1.pooler.example.com:6543/postgres"
DIRECT_PSYCOPG_ASYNC = "postgresql+psycopg_async://user:pass@db.example.com:5432/postgres"


@pytest.mark.parametrize(
    "url, expected",
    [
        (POOLER_PSYCOPG, True),
        (POOLER_ASYNCPG, True),
        (MULTI_HOST_POOLER_PSYCOPG, True),
        (MULTI_HOST_POOLER_ASYNCPG, True),
        ("postgresql+asyncpg://user:pass@/postgres?host=pooler-a&host=pooler-b&port=6543", True),
        (COMMA_MULTI_HOST_POOLER_ASYNCPG, True),
        (POOLER_PSYCOPG_ASYNC, True),
        (DIRECT_PSYCOPG, False),
        (DIRECT_ASYNCPG, False),
        (MULTI_HOST_DIRECT_ASYNCPG, False),
        (COMMA_MULTI_HOST_DIRECT_ASYNCPG, False),
        (AUTHORITY_POOLER_QUERY_DIRECT_PSYCOPG, False),
        (AUTHORITY_POOLER_QUERY_DIRECT_ASYNCPG, False),
        ("postgresql+psycopg://user:pass@h:6543/postgres?host=pooler", True),
        ("postgresql+asyncpg://user:pass@h:6543/postgres?host=pooler", True),
        ("postgresql+asyncpg://user:pass@h:6543/postgres?host=db-a:5432&host=pooler-b", True),
        ("postgresql+asyncpg://user:pass@h:5432/postgres?host=pooler", False),
        (DIRECT_PSYCOPG_ASYNC, False),
        ("postgresql+psycopg://user:pass@host/postgres", False),
        ("sqlite+aiosqlite:///:memory:", False),
        ("not a url", False),
    ],
)
def test_is_transaction_pooler(url: str, expected: bool) -> None:
    assert _is_transaction_pooler(url) is expected


def test_pooler_psycopg_never_sends_options() -> None:
    """The pooler rejects the `options` startup parameter."""
    args = _postgres_connect_args(POOLER_PSYCOPG)
    assert "options" not in args
    assert args["prepare_threshold"] is None
    assert args["sslmode"] == "require"


def test_multi_host_pooler_psycopg_never_sends_options() -> None:
    args = _postgres_connect_args(MULTI_HOST_POOLER_PSYCOPG)
    assert "options" not in args
    assert args["prepare_threshold"] is None
    assert args["sslmode"] == "require"


def test_pooler_psycopg_async_never_sends_options() -> None:
    args = _postgres_connect_args(POOLER_PSYCOPG_ASYNC)
    assert "options" not in args
    assert args["prepare_threshold"] is None
    assert args["sslmode"] == "require"


def test_direct_psycopg_sets_statement_timeout() -> None:
    args = _postgres_connect_args(DIRECT_PSYCOPG)
    assert args["options"] == f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
    assert "sslmode" not in args
    assert "prepare_threshold" not in args


def test_query_host_direct_psycopg_sets_statement_timeout_when_authority_port_is_pooler() -> None:
    args = _postgres_connect_args(AUTHORITY_POOLER_QUERY_DIRECT_PSYCOPG)
    assert args["options"] == f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
    assert "sslmode" not in args
    assert "prepare_threshold" not in args


def test_query_host_pooler_psycopg_never_sends_options_when_authority_port_is_pooler() -> None:
    args = _postgres_connect_args("postgresql+psycopg://user:pass@h:6543/postgres?host=pooler")
    assert "options" not in args
    assert args["prepare_threshold"] is None
    assert args["sslmode"] == "require"


def test_direct_psycopg_preserves_existing_options() -> None:
    args = _postgres_connect_args(f"{DIRECT_PSYCOPG}?options=-c%20search_path%3Dtenant")
    assert args["options"] == f"-c search_path=tenant -c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
    assert "sslmode" not in args
    assert "prepare_threshold" not in args


def test_direct_psycopg_async_sets_statement_timeout() -> None:
    args = _postgres_connect_args(DIRECT_PSYCOPG_ASYNC)
    assert args["options"] == f"-c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
    assert "sslmode" not in args
    assert "prepare_threshold" not in args


def test_psycopg2_url_does_not_receive_psycopg_v3_connect_args() -> None:
    args = _postgres_connect_args(POOLER_PSYCOPG2)
    assert args == {}


def test_pooler_asyncpg_disables_prepared_statements_and_requires_ssl() -> None:
    args = _postgres_connect_args(POOLER_ASYNCPG)
    assert "server_settings" not in args
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    name_func = cast(Callable[[], str], args["prepared_statement_name_func"])
    first_name = name_func()
    second_name = name_func()
    assert first_name != second_name
    assert first_name.startswith("__asyncpg_")
    assert first_name.endswith("__")
    assert args["ssl"] == "require"


def test_multi_host_pooler_asyncpg_uses_pooler_safe_args() -> None:
    args = _postgres_connect_args(MULTI_HOST_POOLER_ASYNCPG)
    assert "server_settings" not in args
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    assert "prepared_statement_name_func" in args
    assert args["ssl"] == "require"


def test_comma_multi_host_pooler_asyncpg_uses_pooler_safe_args() -> None:
    args = _postgres_connect_args(COMMA_MULTI_HOST_POOLER_ASYNCPG)
    assert "server_settings" not in args
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    assert "prepared_statement_name_func" in args
    assert args["ssl"] == "require"


def test_direct_asyncpg_sets_statement_timeout() -> None:
    args = _postgres_connect_args(DIRECT_ASYNCPG)
    assert args["server_settings"] == {"statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)}
    assert "ssl" not in args
    assert "prepared_statement_name_func" not in args


def test_query_host_direct_asyncpg_sets_statement_timeout_when_authority_port_is_pooler() -> None:
    args = _postgres_connect_args(AUTHORITY_POOLER_QUERY_DIRECT_ASYNCPG)
    assert args["server_settings"] == {"statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)}
    assert "ssl" not in args
    assert "prepared_statement_name_func" not in args


def test_build_engine_keeps_pool_class_selection_independent_of_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)

    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", True)
    agent_db._build_engine(POOLER_ASYNCPG)
    disabled_kwargs = calls[-1]["kwargs"]

    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)
    agent_db._build_engine(POOLER_ASYNCPG)
    enabled_kwargs = calls[-1]["kwargs"]

    assert disabled_kwargs["poolclass"] is pool.NullPool
    assert "poolclass" not in enabled_kwargs
    assert disabled_kwargs["connect_args"] == enabled_kwargs["connect_args"]
    assert disabled_kwargs["connect_args"]["prepared_statement_name_func"] is agent_db._asyncpg_prepared_statement_name


def test_build_engine_forwards_pool_sizing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)
    monkeypatch.setattr(settings, "DATABASE_POOL_SIZE", 20)
    monkeypatch.setattr(settings, "DATABASE_POOL_MAX_OVERFLOW", 20)
    monkeypatch.setattr(settings, "DATABASE_POOL_TIMEOUT", 10)
    monkeypatch.setattr(settings, "DATABASE_POOL_RECYCLE", 1800)

    agent_db._build_engine(DIRECT_ASYNCPG)

    kwargs = calls[-1]["kwargs"]
    assert kwargs["pool_size"] == 20
    assert kwargs["max_overflow"] == 20
    assert kwargs["pool_timeout"] == 10
    assert kwargs["pool_recycle"] == 1800


def test_build_engine_null_pool_does_not_forward_queue_pool_params(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", True)

    agent_db._build_engine(DIRECT_ASYNCPG)

    kwargs = calls[-1]["kwargs"]
    assert kwargs["poolclass"] is pool.NullPool
    for queue_pool_param in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
        assert queue_pool_param not in kwargs


def test_build_engine_strips_asyncpg_weak_sslmode_when_requiring_pooler_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{POOLER_ASYNCPG}?sslmode=prefer")

    assert "sslmode" not in calls[-1]["database_string"]
    assert calls[-1]["kwargs"]["connect_args"]["ssl"] == "require"


def test_build_engine_converts_asyncpg_enforcing_sslmode_to_ssl_connect_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{POOLER_ASYNCPG}?sslmode=verify-full")

    assert "sslmode" not in calls[-1]["database_string"]
    assert calls[-1]["kwargs"]["connect_args"]["ssl"] == "verify-full"


def test_build_engine_removes_pooler_asyncpg_weak_ssl_and_sslmode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{POOLER_ASYNCPG}?ssl=prefer&sslmode=prefer")

    engine_query = make_url(calls[-1]["database_string"]).query
    assert "ssl" not in engine_query
    assert "sslmode" not in engine_query
    assert calls[-1]["kwargs"]["connect_args"]["ssl"] == "require"


@pytest.mark.parametrize("database_string", [POOLER_ASYNCPG, DIRECT_ASYNCPG])
@pytest.mark.parametrize("strong_mode", ["require", "verify-full"])
def test_build_engine_prefers_enforcing_asyncpg_sslmode_over_weak_ssl(
    monkeypatch: pytest.MonkeyPatch,
    database_string: str,
    strong_mode: str,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{database_string}?ssl=prefer&sslmode={strong_mode}")

    engine_query = make_url(calls[-1]["database_string"]).query
    connect_args = calls[-1]["kwargs"]["connect_args"]
    assert "ssl" not in engine_query
    assert "sslmode" not in engine_query
    assert connect_args["ssl"] == strong_mode
    if database_string == DIRECT_ASYNCPG:
        assert connect_args["server_settings"] == {"statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)}
    else:
        assert "server_settings" not in connect_args
        assert connect_args["statement_cache_size"] == 0


@pytest.mark.parametrize("sslmode", ["prefer", "verify-full"])
def test_build_engine_converts_direct_asyncpg_sslmode_to_ssl_connect_arg(
    monkeypatch: pytest.MonkeyPatch,
    sslmode: str,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{DIRECT_ASYNCPG}?sslmode={sslmode}")

    assert "sslmode" not in make_url(calls[-1]["database_string"]).query
    assert calls[-1]["kwargs"]["connect_args"]["ssl"] == sslmode
    assert calls[-1]["kwargs"]["connect_args"]["server_settings"] == {
        "statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS)
    }


def test_build_engine_strips_pooler_psycopg_options_from_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{POOLER_PSYCOPG}?options=-c%20search_path%3Dtenant")

    assert "options" not in make_url(calls[-1]["database_string"]).query
    assert "options" not in calls[-1]["kwargs"]["connect_args"]
    assert calls[-1]["kwargs"]["connect_args"]["sslmode"] == "require"


def test_build_engine_moves_direct_psycopg_options_to_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{DIRECT_PSYCOPG}?options=-c%20search_path%3Dtenant")

    assert "options" not in make_url(calls[-1]["database_string"]).query
    assert (
        calls[-1]["kwargs"]["connect_args"]["options"]
        == f"-c search_path=tenant -c statement_timeout={settings.DATABASE_STATEMENT_TIMEOUT_MS}"
    )


def test_build_engine_moves_direct_asyncpg_options_to_server_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_create_async_engine(database_string: str, **kwargs: Any) -> object:
        calls.append({"database_string": database_string, "kwargs": kwargs})
        return object()

    monkeypatch.setattr(agent_db, "create_async_engine", fake_create_async_engine)
    monkeypatch.setattr(settings, "DISABLE_CONNECTION_POOL", False)

    agent_db._build_engine(f"{DIRECT_ASYNCPG}?options=-c%20search_path%3Dtenant")

    assert "options" not in make_url(calls[-1]["database_string"]).query
    assert calls[-1]["kwargs"]["connect_args"]["server_settings"] == {
        "search_path": "tenant",
        "statement_timeout": str(settings.DATABASE_STATEMENT_TIMEOUT_MS),
    }


@pytest.mark.parametrize("strong_mode", ["require", "verify-ca", "verify-full"])
def test_pooler_psycopg_preserves_enforcing_sslmode(strong_mode: str) -> None:
    args = _postgres_connect_args(f"{POOLER_PSYCOPG}?sslmode={strong_mode}")
    assert "sslmode" not in args
    assert args["prepare_threshold"] is None


@pytest.mark.parametrize("ssl_query_key", ["ssl", "sslmode"])
@pytest.mark.parametrize("strong_mode", ["require", "verify-ca", "verify-full"])
def test_pooler_asyncpg_preserves_enforcing_ssl_modes(ssl_query_key: str, strong_mode: str) -> None:
    args = _postgres_connect_args(f"{POOLER_ASYNCPG}?{ssl_query_key}={strong_mode}")
    assert "ssl" not in args
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0


@pytest.mark.parametrize("weak_mode", ["disable", "allow", "prefer"])
def test_pooler_psycopg_overrides_non_enforcing_sslmode(weak_mode: str) -> None:
    """A non-enforcing sslmode in the URL must still be forced to require."""
    args = _postgres_connect_args(f"{POOLER_PSYCOPG}?sslmode={weak_mode}")
    assert args["sslmode"] == "require"


@pytest.mark.parametrize("weak_mode", ["disable", "allow", "prefer"])
def test_pooler_asyncpg_overrides_non_enforcing_sslmode(weak_mode: str) -> None:
    args = _postgres_connect_args(f"{POOLER_ASYNCPG}?sslmode={weak_mode}")
    assert args["ssl"] == "require"


@pytest.mark.parametrize("weak_mode", ["disable", "allow", "prefer"])
def test_pooler_asyncpg_overrides_non_enforcing_ssl(weak_mode: str) -> None:
    args = _postgres_connect_args(f"{POOLER_ASYNCPG}?ssl={weak_mode}")
    assert args["ssl"] == "require"


@pytest.mark.parametrize(
    "url, connect_arg_key, log_ssl_key",
    [
        (f"{POOLER_PSYCOPG}?sslmode=prefer", "sslmode", "sslmode"),
        (f"{POOLER_ASYNCPG}?ssl=prefer", "ssl", "ssl"),
        (f"{POOLER_ASYNCPG}?sslmode=prefer", "ssl", "sslmode"),
    ],
)
def test_pooler_logs_when_overriding_non_enforcing_ssl(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    connect_arg_key: str,
    log_ssl_key: str,
) -> None:
    logs: list[dict[str, Any]] = []

    class FakeLogger:
        def debug(self, message: str, **kwargs: Any) -> None:
            logs.append({"message": message, **kwargs})

    monkeypatch.setattr(agent_db, "LOG", FakeLogger())

    args = _postgres_connect_args(url)

    assert args[connect_arg_key] == "require"
    assert logs == [
        {
            "message": "Overriding non-enforcing Postgres pooler SSL mode",
            "ssl_key": log_ssl_key,
            "ssl_mode": "prefer",
            "required_ssl_mode": "require",
        }
    ]
