"""Tests for targeted `skyvern setup` command behaviors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyvern.cli.setup_commands import _build_local_mcp_entry, _cursor_config_path, _windsurf_config_path, setup_app

_FAKE_ENV = ("test-key", "https://api.skyvern.com")


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.cli.setup_commands._get_env_credentials", lambda: _FAKE_ENV)
    monkeypatch.setattr("skyvern.cli.setup_commands.capture_setup_event", lambda *a, **kw: None)


def _patch_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.cli.setup_commands._get_local_env_credentials", lambda: _FAKE_ENV)
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
    _patch_local_env(monkeypatch)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_desktop_config_path", lambda: config)
    monkeypatch.setattr("skyvern.cli.setup_commands._has_node_runtime", lambda: False)

    result = CliRunner().invoke(setup_app, ["claude", "--local", "--yes"])

    assert result.exit_code == 0
    data = json.loads(config.read_text())
    assert data["mcpServers"]["skyvern"]["command"] == sys.executable
    assert data["mcpServers"]["skyvern"]["args"] == ["-m", "skyvern", "run", "mcp"]
    assert data["mcpServers"]["skyvern"]["env"]["SKYVERN_API_KEY"] == "test-key"


def test_local_mcp_entry_appends_non_default_scope_only() -> None:
    default_entry = _build_local_mcp_entry("test-key", "http://localhost:8000")
    scoped_entry = _build_local_mcp_entry("test-key", "http://localhost:8000", scope="operate")

    assert default_entry["args"] == ["-m", "skyvern", "run", "mcp"]
    assert scoped_entry["args"][-4:] == ["run", "mcp", "--scope", "operate"]


def test_setup_claude_code_local_scope_operate_writes_args_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / ".claude.json"
    _patch_local_env(monkeypatch)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_code_global_config_path", lambda: config)
    monkeypatch.setattr("skyvern.cli.setup_commands._install_skills", lambda *a, **kw: None)

    result = CliRunner().invoke(setup_app, ["claude-code", "--global", "--local", "--scope", "operate", "--yes"])

    assert result.exit_code == 0, result.output
    data = json.loads(config.read_text())
    assert data["mcpServers"]["skyvern"]["args"] == ["-m", "skyvern", "run", "mcp", "--scope", "operate"]


def test_setup_rejects_invalid_scope_choice() -> None:
    result = CliRunner().invoke(setup_app, ["claude-code", "--local", "--scope", "invalid", "--yes"])

    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_setup_openclaw_rejects_remote_scope_before_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(**_kwargs: object) -> tuple[str, str]:
        raise AssertionError("credentials must not be resolved before scope validation")

    monkeypatch.setattr("skyvern.cli.setup_commands._resolve_setup_credentials", _fail)

    result = CliRunner().invoke(setup_app, ["openclaw", "--scope", "operate", "--yes"])

    assert result.exit_code == 1
    assert "--scope is only supported with --local" in result.output


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


def test_cursor_config_path_uses_windows_home_on_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    roaming_path = Path("/mnt/c/Users/alice/AppData/Roaming")
    monkeypatch.setattr("skyvern.cli.setup_commands.detect_os", lambda: "wsl")
    monkeypatch.setattr("skyvern.cli.setup_commands.get_windows_appdata_roaming", lambda: roaming_path)

    assert _cursor_config_path() == Path("/mnt/c/Users/alice/.cursor/mcp.json")


def test_windsurf_config_path_uses_windows_home_on_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    roaming_path = Path("/mnt/c/Users/alice/AppData/Roaming")
    monkeypatch.setattr("skyvern.cli.setup_commands.detect_os", lambda: "wsl")
    monkeypatch.setattr("skyvern.cli.setup_commands.get_windows_appdata_roaming", lambda: roaming_path)

    assert _windsurf_config_path() == Path("/mnt/c/Users/alice/.codeium/windsurf/mcp_config.json")
