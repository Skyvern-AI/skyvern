"""Tests for the guided quickstart flow in `skyvern setup`.

Covers the key behavioral contracts:
- Detection exception resilience
- API key priority (flag > env > signup) and --yes failure
- Guided flow: writes config, respects --dry-run, subcommands still work
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.exceptions import Exit
from typer.testing import CliRunner

from skyvern.cli.setup_commands import _acquire_api_key, _detect_installed_tools, setup_app

# Shared monkeypatch helpers ------------------------------------------------

_FAKE_ENV = ("test-key", "https://api.skyvern.com")
_NO_ENV = ("", "https://api.skyvern.com")


def _patch_detection(monkeypatch: pytest.MonkeyPatch, *, claude_code: bool = False, cursor: bool = False) -> None:
    monkeypatch.setattr("skyvern.cli.setup_commands._is_claude_code_installed", lambda: claude_code)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_cursor_installed", lambda: cursor)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_windsurf_installed", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_claude_desktop_installed", lambda: False)


def _patch_env(monkeypatch: pytest.MonkeyPatch, env: tuple[str, str] = _FAKE_ENV) -> None:
    monkeypatch.setattr("skyvern.cli.setup_commands._get_env_credentials", lambda: env)
    monkeypatch.setattr("skyvern.cli.setup_commands.capture_setup_event", lambda *a, **kw: None)


# Detection ------------------------------------------------------------------


def test_detect_handles_exception_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a detection function raises, the tool lands in not_detected (not a crash)."""
    monkeypatch.setattr(
        "skyvern.cli.setup_commands._is_claude_code_installed", lambda: (_ for _ in ()).throw(RuntimeError)
    )
    monkeypatch.setattr("skyvern.cli.setup_commands._is_cursor_installed", lambda: True)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_windsurf_installed", lambda: False)
    monkeypatch.setattr("skyvern.cli.setup_commands._is_claude_desktop_installed", lambda: False)

    detected, not_detected = _detect_installed_tools()
    assert {t.name for t in detected} == {"Cursor"}
    assert "Claude Code" in {t.name for t in not_detected}


# API key acquisition --------------------------------------------------------


def test_acquire_api_key_flag_beats_env() -> None:
    assert _acquire_api_key("flag-key", yes=False) == "flag-key"


def test_acquire_api_key_yes_without_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch, _NO_ENV)
    with pytest.raises(Exit):
        _acquire_api_key(None, yes=True)


# Guided flow (CliRunner integration) ---------------------------------------


def test_guided_writes_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: env key present, one tool detected, config file written."""
    config = tmp_path / ".claude.json"
    _patch_env(monkeypatch)
    _patch_detection(monkeypatch, claude_code=True)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_code_global_config_path", lambda: config)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(setup_app, ["--yes"])
    assert result.exit_code == 0
    data = json.loads(config.read_text())
    assert data["mcpServers"]["skyvern"]["headers"]["x-api-key"] == "test-key"
    assert "Setup complete" in result.output


def test_guided_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / ".claude.json"
    _patch_env(monkeypatch)
    _patch_detection(monkeypatch, claude_code=True)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_code_global_config_path", lambda: config)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(setup_app, ["--dry-run", "--yes"])
    assert result.exit_code == 0
    assert not config.exists()
    assert "Dry run" in result.output


def test_guided_no_tools_shows_manual_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch)
    _patch_detection(monkeypatch)

    result = CliRunner().invoke(setup_app, ["--yes"])
    assert result.exit_code == 0
    assert "No supported AI tools detected" in result.output


def test_subcommand_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing `setup claude-code` must not be broken by the callback."""
    config = tmp_path / ".claude.json"
    _patch_env(monkeypatch)
    monkeypatch.setattr("skyvern.cli.setup_commands._claude_code_global_config_path", lambda: config)
    # Prevent _install_skills from writing into the repo's .claude/skills/ during CI
    monkeypatch.setattr("skyvern.cli.setup_commands._install_skills", lambda *a, **kw: None)

    result = CliRunner().invoke(setup_app, ["claude-code", "--global", "--yes"])
    assert result.exit_code == 0
    data = json.loads(config.read_text())
    assert data["mcpServers"]["skyvern"]["headers"]["x-api-key"] == "test-key"


def test_claude_code_local_auto_uses_project_config_and_installs_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    bundled_skill = tmp_path / "bundled" / "qa"
    bundled_skill.mkdir(parents=True)
    (bundled_skill / "SKILL.md").write_text("# qa\n", encoding="utf-8")

    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("SKYVERN_API_KEY", "local-key")
    monkeypatch.setenv("SKYVERN_BASE_URL", "http://localhost:8000")
    monkeypatch.setattr("skyvern.cli.setup_commands.get_skill_dirs", lambda: [bundled_skill])

    result = CliRunner().invoke(setup_app, ["claude-code", "--local", "--yes"])
    assert result.exit_code == 0

    data = json.loads((project_dir / ".mcp.json").read_text())
    assert data["mcpServers"]["skyvern"]["command"] == sys.executable
    assert data["mcpServers"]["skyvern"]["args"] == ["-m", "skyvern", "run", "mcp"]
    assert data["mcpServers"]["skyvern"]["env"] == {
        "SKYVERN_BASE_URL": "http://localhost:8000",
        "SKYVERN_API_KEY": "local-key",
    }
    assert (project_dir / ".claude" / "skills" / "qa" / "SKILL.md").exists()
