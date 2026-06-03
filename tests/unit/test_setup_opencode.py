"""Tests for skyvern setup opencode command (fixes OAuth callback timeouts in OpenCode)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyvern.cli.setup_commands import _mask_secrets, setup_app

runner = CliRunner()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def opencode_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / ".config" / "opencode"
    monkeypatch.setattr("skyvern.cli.setup_commands.Path.home", lambda: tmp_path)
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key-1234567890")
    monkeypatch.setenv("SKYVERN_BASE_URL", "https://api.skyvern.com")
    return config_dir


def test_setup_opencode_writes_remote_config_with_oauth_disabled(opencode_home: Path) -> None:
    result = runner.invoke(setup_app, ["opencode", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(opencode_home / "opencode.json")
    entry = config["mcp"]["skyvern"]
    assert entry["type"] == "remote"
    assert entry["url"] == "https://api.skyvern.com/mcp/"
    assert entry["oauth"] is False
    assert entry["headers"]["x-api-key"] == "test-key-1234567890"
    assert "opencode mcp auth" in result.output


def test_setup_opencode_writes_local_stdio_config(opencode_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKYVERN_BASE_URL", "http://localhost:8000")

    result = runner.invoke(setup_app, ["opencode", "--local", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(opencode_home / "opencode.json")
    entry = config["mcp"]["skyvern"]
    assert entry["type"] == "local"
    assert entry["command"] == [sys.executable, "-m", "skyvern", "run", "mcp"]
    assert entry["environment"]["SKYVERN_BASE_URL"] == "http://localhost:8000"
    assert entry["environment"]["SKYVERN_API_KEY"] == "test-key-1234567890"
    assert "oauth" not in entry


def test_setup_opencode_case_insensitive_key(opencode_home: Path) -> None:
    config_path = opencode_home / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "Skyvern": {
                        "type": "remote",
                        "url": "https://old.example.com/mcp/",
                        "oauth": True,
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(setup_app, ["opencode", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(config_path)
    entry = config["mcp"]["Skyvern"]
    assert entry["url"] == "https://api.skyvern.com/mcp/"
    assert entry["oauth"] is False
    assert entry["headers"]["x-api-key"] == "test-key-1234567890"


def test_setup_opencode_respects_opencode_config_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_config = tmp_path / "custom-opencode.json"
    monkeypatch.setenv("OPENCODE_CONFIG", str(custom_config))
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key-1234567890")
    monkeypatch.setenv("SKYVERN_BASE_URL", "https://api.skyvern.com")

    result = runner.invoke(setup_app, ["opencode", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(custom_config)
    assert config["mcp"]["skyvern"]["oauth"] is False


def test_setup_opencode_accepts_jsonc_config(opencode_home: Path) -> None:
    config_path = opencode_home / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
{
  // OpenCode configs are JSONC.
  "mcp": {
    "skyvern": {
      "type": "remote",
      "url": "https://old.example.com/mcp/",
      "oauth": true,
    },
  },
}
""",
        encoding="utf-8",
    )

    result = runner.invoke(setup_app, ["opencode", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(config_path)
    entry = config["mcp"]["skyvern"]
    assert entry["url"] == "https://api.skyvern.com/mcp/"
    assert entry["oauth"] is False
    assert entry["headers"]["x-api-key"] == "test-key-1234567890"


def test_mask_secrets_masks_opencode_environment_api_key() -> None:
    masked = _mask_secrets(
        {
            "type": "local",
            "command": [sys.executable, "-m", "skyvern", "run", "mcp"],
            "environment": {
                "SKYVERN_BASE_URL": "http://localhost:8000",
                "SKYVERN_API_KEY": "test-key-1234567890",
            },
        }
    )

    assert masked["environment"]["SKYVERN_API_KEY"] == "test****7890"
