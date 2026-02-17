from __future__ import annotations

import logging

import skyvern.cli.commands as cli_commands


def test_configure_cli_logging_is_idempotent(monkeypatch) -> None:
    setup_calls: list[str] = []
    monkeypatch.setattr(cli_commands, "_setup_logger", lambda: setup_calls.append("called"))
    monkeypatch.setattr(cli_commands, "_cli_logging_configured", False)

    logger_names = ("skyvern", "httpx", "litellm", "playwright", "httpcore")
    previous_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        cli_commands.configure_cli_logging()
        assert setup_calls == ["called"]
        for name in logger_names:
            assert logging.getLogger(name).level == logging.WARNING

        cli_commands.configure_cli_logging()
        assert setup_calls == ["called"]
    finally:
        for name, level in previous_levels.items():
            logging.getLogger(name).setLevel(level)


def test_cli_callback_configures_logging(monkeypatch) -> None:
    called = False

    def _fake_configure() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli_commands, "configure_cli_logging", _fake_configure)
    cli_commands.cli_callback()
    assert called
