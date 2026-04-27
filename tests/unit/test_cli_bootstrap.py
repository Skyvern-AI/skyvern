from __future__ import annotations

import logging
from types import SimpleNamespace

import skyvern._cli_bootstrap as cli_bootstrap


def test_bootstrap_defaults_to_warning_without_explicit_log_level(monkeypatch) -> None:
    setup_calls: list[str] = []
    fake_settings = SimpleNamespace(LOG_LEVEL="INFO", model_fields_set=set())

    monkeypatch.setattr("skyvern.config.settings", fake_settings)
    monkeypatch.setattr("skyvern.forge.sdk.forge_log.setup_logger", lambda: setup_calls.append("called"))

    logger_names = ("", "skyvern", "httpx", "litellm", "playwright", "httpcore")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        cli_bootstrap.configure_cli_bootstrap_logging()
        assert setup_calls == ["called"]
        assert fake_settings.LOG_LEVEL == "WARNING"
        for name in logger_names:
            assert logging.getLogger(name).level == logging.WARNING
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


def test_bootstrap_honors_explicit_log_level(monkeypatch) -> None:
    setup_calls: list[str] = []
    fake_settings = SimpleNamespace(LOG_LEVEL="DEBUG", model_fields_set={"LOG_LEVEL"})

    monkeypatch.setattr("skyvern.config.settings", fake_settings)
    monkeypatch.setattr("skyvern.forge.sdk.forge_log.setup_logger", lambda: setup_calls.append("called"))

    logger_names = ("", "skyvern", "httpx", "litellm", "playwright", "httpcore")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        cli_bootstrap.configure_cli_bootstrap_logging()
        assert setup_calls == ["called"]
        assert fake_settings.LOG_LEVEL == "DEBUG"
        for name in logger_names:
            assert logging.getLogger(name).level == logging.DEBUG
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)
