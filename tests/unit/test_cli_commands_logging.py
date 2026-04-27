from __future__ import annotations

import skyvern.cli.commands as cli_commands


def test_configure_cli_logging_is_idempotent(monkeypatch) -> None:
    setup_calls: list[str] = []
    monkeypatch.setattr(cli_commands, "_configure_cli_bootstrap_logging", lambda: setup_calls.append("called"))
    monkeypatch.setattr(cli_commands, "_cli_logging_configured", False)
    cli_commands.configure_cli_logging()
    assert setup_calls == ["called"]

    cli_commands.configure_cli_logging()
    assert setup_calls == ["called"]


def test_cli_callback_configures_logging(monkeypatch) -> None:
    called = False

    def _fake_configure() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli_commands, "configure_cli_logging", _fake_configure)
    cli_commands.cli_callback()
    assert called
