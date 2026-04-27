"""Tests for skyvern setup hermes command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyvern.cli.setup_commands import (
    _load_yaml_config,
    _save_yaml_config,
    setup_app,
)

runner = CliRunner()


@pytest.fixture()
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake ~/.hermes with global config + 2 profiles."""
    home = tmp_path / ".hermes"
    home.mkdir()
    _save_yaml_config(home / "config.yaml", {"model": {"default": "gpt-4"}})

    for name in ("profile-a", "profile-b"):
        p = home / "profiles" / name
        p.mkdir(parents=True)
        _save_yaml_config(p / "config.yaml", {"mcp_servers": {"exa": {"url": "https://exa.ai"}}})

    monkeypatch.setattr("skyvern.cli.setup_commands.Path.home", lambda: tmp_path)
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key-1234567890")
    monkeypatch.setenv("SKYVERN_BASE_URL", "https://api.skyvern.com")
    return home


def test_setup_hermes_updates_global_and_profiles(hermes_home: Path) -> None:
    """Remote mode updates global + all profile configs."""
    result = runner.invoke(setup_app, ["hermes", "--yes"])
    assert result.exit_code == 0, result.output

    for config_path in [
        hermes_home / "config.yaml",
        hermes_home / "profiles" / "profile-a" / "config.yaml",
        hermes_home / "profiles" / "profile-b" / "config.yaml",
    ]:
        data = _load_yaml_config(config_path)
        assert data is not None
        assert "skyvern" in data["mcp_servers"]
        assert data["mcp_servers"]["skyvern"]["url"] == "https://api.skyvern.com/mcp/"
        assert data["mcp_servers"]["skyvern"]["headers"]["x-api-key"] == "test-key-1234567890"

    # Existing exa entry preserved in profiles
    profile_a = _load_yaml_config(hermes_home / "profiles" / "profile-a" / "config.yaml")
    assert profile_a["mcp_servers"]["exa"]["url"] == "https://exa.ai"


def test_setup_hermes_skips_malformed_profile(hermes_home: Path) -> None:
    """Bad YAML in one profile is skipped, others still updated."""
    bad_profile = hermes_home / "profiles" / "profile-a" / "config.yaml"
    bad_profile.write_text("{{{{invalid yaml", encoding="utf-8")

    result = runner.invoke(setup_app, ["hermes", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Skipping" in result.output

    # profile-b still updated
    data = _load_yaml_config(hermes_home / "profiles" / "profile-b" / "config.yaml")
    assert data is not None
    assert "skyvern" in data["mcp_servers"]


def test_setup_hermes_case_insensitive_key(hermes_home: Path) -> None:
    """Existing 'Skyvern' key (capitalized) is reused, not duplicated."""
    config_path = hermes_home / "config.yaml"
    data = _load_yaml_config(config_path)
    data["mcp_servers"] = {"Skyvern": {"url": "https://old.example.com"}}
    _save_yaml_config(config_path, data)

    result = runner.invoke(setup_app, ["hermes", "--yes"])
    assert result.exit_code == 0, result.output

    updated = _load_yaml_config(config_path)
    assert updated is not None
    # Should reuse 'Skyvern' key, not create a new 'skyvern'
    assert "Skyvern" in updated["mcp_servers"]
    assert updated["mcp_servers"]["Skyvern"]["url"] == "https://api.skyvern.com/mcp/"
    # No duplicate lowercase key
    keys = [k for k in updated["mcp_servers"] if k.lower() == "skyvern"]
    assert len(keys) == 1


def test_setup_hermes_local_fails_without_base_url(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode exits with error when SKYVERN_BASE_URL is missing."""
    monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key-1234567890")
    # Prevent dotenv from providing a base URL
    monkeypatch.setattr("skyvern.cli.setup_commands._get_local_env_credentials", lambda: ("test-key", ""))

    result = runner.invoke(setup_app, ["hermes", "--local", "--yes"])
    assert result.exit_code == 1
    assert "SKYVERN_BASE_URL" in result.output


def test_setup_hermes_dry_run_masks_secrets(hermes_home: Path) -> None:
    """Dry-run output does not contain raw API keys."""
    result = runner.invoke(setup_app, ["hermes", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "test-key-1234567890" not in result.output
    # Masked key should appear
    assert "****" in result.output


def test_setup_hermes_idempotent_no_backup(hermes_home: Path) -> None:
    """Running setup twice with same config produces no backup on second run."""
    # First run
    result1 = runner.invoke(setup_app, ["hermes", "--yes"])
    assert result1.exit_code == 0

    # Count backups
    backups_before = list(hermes_home.rglob("*.bak"))

    # Second run with same key/url — should be a no-op (exit 0, no error)
    result2 = runner.invoke(setup_app, ["hermes", "--yes"])
    assert result2.exit_code == 0, result2.output
    assert "up to date" in result2.output
    backups_after = list(hermes_home.rglob("*.bak"))

    # No new backups created on second run
    assert len(backups_after) == len(backups_before)
