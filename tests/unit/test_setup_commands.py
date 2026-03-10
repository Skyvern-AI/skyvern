"""Tests for targeted `skyvern setup` command behaviors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyvern.cli.setup_commands import setup_app

_FAKE_ENV = ("test-key", "https://api.skyvern.com")


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.cli.setup_commands._get_env_credentials", lambda: _FAKE_ENV)
    monkeypatch.setattr("skyvern.cli.setup_commands.capture_setup_event", lambda *a, **kw: None)


def test_setup_claude_remote_without_node_shows_bundle_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "claude_desktop_config.json"
    _patch_env(monkeypatch)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_desktop_config_path", lambda: config)
    monkeypatch.setattr("skyvern.cli.setup_commands._has_node_runtime", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._supports_claude_desktop_bundle", lambda: True)

    result = CliRunner().invoke(setup_app, ["claude", "--yes"])

    assert result.exit_code == 1
    assert "one-click Skyvern bundle" in result.output
    assert ".mcpb" in result.output
    assert not config.exists()


def test_setup_claude_local_without_node_still_writes_stdio_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "claude_desktop_config.json"
    _patch_env(monkeypatch)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_desktop_config_path", lambda: config)
    monkeypatch.setattr("skyvern.cli.setup_commands._has_node_runtime", lambda: False)

    result = CliRunner().invoke(setup_app, ["claude", "--local", "--yes"])

    assert result.exit_code == 0
    data = json.loads(config.read_text())
    assert data["mcpServers"]["skyvern"]["command"] == "skyvern"
    assert data["mcpServers"]["skyvern"]["env"]["SKYVERN_API_KEY"] == "test-key"


def test_guided_setup_skips_claude_desktop_without_node_and_prints_bundle_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "claude_desktop_config.json"
    _patch_env(monkeypatch)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_desktop_config_path", lambda: config)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_claude_code_installed", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_cursor_installed", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_windsurf_installed", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_claude_desktop_installed", lambda: True)
    monkeypatch.setattr("skyvern.cli.setup_commands._has_node_runtime", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._supports_claude_desktop_bundle", lambda: True)

    result = CliRunner().invoke(setup_app, ["--yes"])

    assert result.exit_code == 0
    assert "Skipping Claude Desktop JSON setup" in result.output
    assert "one-click Skyvern bundle" in result.output
    assert ".mcpb" in result.output
    assert not config.exists()
