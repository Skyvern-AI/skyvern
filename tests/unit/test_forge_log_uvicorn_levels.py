"""Regression tests for the uvicorn.error log-level suppression.

Both "connection open" and the "WebSocket /v1/stream/..." [accepted] access
line are emitted via logging.getLogger("uvicorn.error") — uvicorn explicitly
passes its own logger into websockets' ServerProtocol (see
uvicorn/protocols/websockets/websockets_sansio_impl.py). Suppressing them
requires two cooperating pieces:

1. setup_logger() must setLevel(WARNING) on uvicorn.error and CRITICAL on
   uvicorn.access.
2. Every uvicorn.run() entry point must NOT pass log_level=, otherwise
   uvicorn.Config.configure_logging() calls setLevel(log_level) on
   uvicorn.error/access/asgi and undoes (1).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
from collections.abc import Iterator
from types import ModuleType

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.forge_log import setup_logger

_OTLP_EXPORTER_LOGGER = "opentelemetry.exporter.otlp.proto.grpc.exporter"

_TOUCHED_LOGGERS = (
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
    "websockets",
    "websockets.server",
    "websockets.client",
    "websockets.legacy",
    "websockets.legacy.server",
    _OTLP_EXPORTER_LOGGER,
)


@pytest.fixture
def _restore_logger_levels() -> Iterator[None]:
    saved = {name: logging.getLogger(name).level for name in _TOUCHED_LOGGERS}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_setup_logger_silences_uvicorn_and_websockets(_restore_logger_levels: None) -> None:
    setup_logger()
    assert logging.getLogger("uvicorn.error").level == logging.WARNING
    assert logging.getLogger("uvicorn.access").level == logging.CRITICAL
    assert logging.getLogger("websockets").level == logging.WARNING
    assert logging.getLogger("websockets.server").level == logging.WARNING
    assert logging.getLogger("websockets.client").level == logging.WARNING
    assert logging.getLogger("websockets.legacy").level == logging.WARNING
    assert logging.getLogger("websockets.legacy.server").level == logging.WARNING


def test_setup_logger_defaults_otlp_exporter_logger_to_warning(_restore_logger_levels: None) -> None:
    setup_logger()
    assert logging.getLogger(_OTLP_EXPORTER_LOGGER).level == logging.WARNING


def test_setup_logger_suppresses_otlp_exporter_logs_at_critical(
    _restore_logger_levels: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "OTEL_EXPORTER_LOG_LEVEL", "CRITICAL")

    setup_logger()

    exporter_logger = logging.getLogger(_OTLP_EXPORTER_LOGGER)
    assert exporter_logger.level == logging.CRITICAL
    # The exporter emits its failure spam at WARNING ("Transient error ... retrying")
    # and ERROR ("Failed to export ..."); both records must be dropped so they never
    # reach stdout/log ingestion. A mere severity relabel would still emit them.
    assert exporter_logger.isEnabledFor(logging.WARNING) is False
    assert exporter_logger.isEnabledFor(logging.ERROR) is False


def test_setup_logger_falls_back_to_warning_for_unknown_exporter_level(
    _restore_logger_levels: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "OTEL_EXPORTER_LOG_LEVEL", "NOT_A_LEVEL")

    setup_logger()

    assert logging.getLogger(_OTLP_EXPORTER_LOGGER).level == logging.WARNING


@pytest.mark.parametrize(
    "module_path",
    [
        "skyvern.forge.__main__",
        "skyvern.cli.run_commands",
    ],
)
def test_no_log_level_kwarg_on_uvicorn_run(module_path: str) -> None:
    module: ModuleType = importlib.import_module(module_path)
    tree = ast.parse(inspect.getsource(module))

    found_call = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "uvicorn"
        ):
            continue
        found_call = True
        for kw in node.keywords:
            assert kw.arg != "log_level", (
                f"{module_path} must not pass log_level= to uvicorn.run(); it triggers "
                "Config.configure_logging() to setLevel(log_level) on "
                "uvicorn.error/access/asgi and re-leaks WebSocket INFO spam."
            )

    assert found_call, f"expected a uvicorn.run() call in {module_path}"
