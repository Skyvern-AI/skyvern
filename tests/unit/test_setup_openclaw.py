"""Tests for skyvern setup openclaw command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyvern.cli.setup_commands import _looks_like_json5_source, setup_app

runner = CliRunner()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def openclaw_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("skyvern.cli.setup_commands.Path.home", lambda: tmp_path)
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key-1234567890")
    monkeypatch.setenv("SKYVERN_BASE_URL", "https://api.skyvern.com")
    return tmp_path / ".openclaw"


def test_setup_openclaw_writes_nested_remote_config(openclaw_home: Path) -> None:
    result = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(openclaw_home / "openclaw.json")
    entry = config["mcp"]["servers"]["skyvern"]
    assert entry["url"] == "https://api.skyvern.com/mcp/"
    assert entry["transport"] == "streamable-http"
    assert entry["headers"]["x-api-key"] == "test-key-1234567890"


def test_setup_openclaw_writes_local_stdio_config(
    openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKYVERN_BASE_URL", "http://localhost:8000")

    result = runner.invoke(setup_app, ["openclaw", "--local", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(openclaw_home / "openclaw.json")
    entry = config["mcp"]["servers"]["skyvern"]
    assert entry["command"]
    assert entry["args"]
    assert entry["env"]["SKYVERN_BASE_URL"] == "http://localhost:8000"
    assert entry["env"]["SKYVERN_API_KEY"] == "test-key-1234567890"
    assert "transport" not in entry


def test_setup_openclaw_case_insensitive_key(openclaw_home: Path) -> None:
    config_path = openclaw_home / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "Skyvern": {
                            "url": "https://old.example.com/mcp/",
                            "transport": "streamable-http",
                        }
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(config_path)
    keys = [key for key in config["mcp"]["servers"] if key.lower() == "skyvern"]
    assert keys == ["Skyvern"]
    assert config["mcp"]["servers"]["Skyvern"]["headers"]["x-api-key"] == "test-key-1234567890"


def test_setup_openclaw_idempotent_no_backup(openclaw_home: Path) -> None:
    first = runner.invoke(setup_app, ["openclaw", "--yes"])
    assert first.exit_code == 0, first.output

    backups_before = list(openclaw_home.glob("openclaw.json.bak-*"))

    second = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert second.exit_code == 0, second.output
    assert "Already configured for OpenClaw" in second.output
    backups_after = list(openclaw_home.glob("openclaw.json.bak-*"))
    assert len(backups_after) == len(backups_before)


def test_setup_openclaw_preserves_existing_remote_extras(openclaw_home: Path) -> None:
    config_path = openclaw_home / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "skyvern": {
                            "url": "https://old.example.com/mcp/",
                            "transport": "streamable-http",
                            "headers": {
                                "x-api-key": "old-key",
                                "x-extra": "keep-me",
                            },
                            "connectionTimeoutMs": 120000,
                        }
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        setup_app,
        ["openclaw", "--yes", "--api-key", "new-key-1234567890", "--url", "https://alt.skyvern.example/mcp/"],
    )

    assert result.exit_code == 0, result.output
    config = _load_json(config_path)
    entry = config["mcp"]["servers"]["skyvern"]
    assert entry["url"] == "https://alt.skyvern.example/mcp/"
    assert entry["transport"] == "streamable-http"
    assert entry["headers"]["x-api-key"] == "new-key-1234567890"
    assert entry["headers"]["x-extra"] == "keep-me"
    assert entry["connectionTimeoutMs"] == 120000


def test_setup_openclaw_repairs_legacy_remote_shape(openclaw_home: Path) -> None:
    config_path = openclaw_home / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "skyvern": {
                            "type": "http",
                            "url": "https://old.example.com/mcp/",
                            "headers": {"x-api-key": "old-key"},
                            "http_headers": {
                                "x-api-key": "stale-key",
                                "x-custom": "keep-me",
                            },
                            "connectionTimeoutMs": 120000,
                        }
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        setup_app,
        ["openclaw", "--yes", "--api-key", "new-key-1234567890", "--url", "https://alt.skyvern.example/mcp/"],
    )

    assert result.exit_code == 0, result.output
    config = _load_json(config_path)
    entry = config["mcp"]["servers"]["skyvern"]
    assert entry["url"] == "https://alt.skyvern.example/mcp/"
    assert entry["transport"] == "streamable-http"
    assert entry["headers"]["x-api-key"] == "new-key-1234567890"
    assert entry["headers"]["x-custom"] == "keep-me"
    assert entry["connectionTimeoutMs"] == 120000
    assert "type" not in entry
    assert "http_headers" not in entry


def test_setup_openclaw_errors_on_invalid_nested_structure(openclaw_home: Path) -> None:
    config_path = openclaw_home / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"mcp": 42}) + "\n", encoding="utf-8")

    result = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert result.exit_code == 1
    assert "Invalid nested structure" in result.output


def test_setup_openclaw_respects_openclaw_config_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "custom-openclaw.json"
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key-1234567890")
    monkeypatch.setenv("SKYVERN_BASE_URL", "https://api.skyvern.com")

    result = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert result.exit_code == 0, result.output
    assert config_path.exists()
    config = _load_json(config_path)
    assert config["mcp"]["servers"]["skyvern"]["transport"] == "streamable-http"


def test_setup_openclaw_uses_wsl_runtime_home(openclaw_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.cli.setup_commands.detect_os", lambda: "wsl")

    result = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert result.exit_code == 0, result.output
    config = _load_json(openclaw_home / "openclaw.json")
    assert config["mcp"]["servers"]["skyvern"]["url"] == "https://api.skyvern.com/mcp/"


def test_setup_openclaw_accepts_json5_config(
    openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = openclaw_home / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
        {
          // Existing OpenClaw config with JSON5 comments + trailing commas.
          mcp: {
            servers: {
              skyvern: {
                url: "https://old.example.com/mcp/",
                transport: "streamable-http",
                connectionTimeoutMs: 120000,
              },
            },
          },
        }
        """,
        encoding="utf-8",
    )
    printed: list[str] = []

    def fake_print(*args: object, **kwargs: object) -> None:
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("skyvern.cli.setup_commands.console.print", fake_print)

    result = runner.invoke(setup_app, ["openclaw", "--yes"])

    assert result.exit_code == 0, result.output
    assert any("rewrites the file as standard JSON" in message for message in printed)
    config = _load_json(config_path)
    entry = config["mcp"]["servers"]["skyvern"]
    assert entry["headers"]["x-api-key"] == "test-key-1234567890"
    assert entry["connectionTimeoutMs"] == 120000


def test_setup_openclaw_does_not_warn_for_plain_json_urls(
    openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = openclaw_home / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "skyvern": {
                            "url": "https://api.skyvern.com/mcp/",
                            "transport": "streamable-http",
                            "headers": {"x-api-key": "old-key"},
                            "description": "Marc's plain JSON config",
                        }
                    }
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    printed: list[str] = []

    def fake_print(*args: object, **kwargs: object) -> None:
        printed.append(" ".join(str(arg) for arg in args))

    monkeypatch.setattr("skyvern.cli.setup_commands.console.print", fake_print)

    result = runner.invoke(
        setup_app,
        ["openclaw", "--yes", "--api-key", "new-key-1234567890", "--url", "https://alt.skyvern.example/mcp/"],
    )

    assert result.exit_code == 0, result.output
    assert not any("rewrites the file as standard JSON" in message for message in printed)


def test_looks_like_json5_source_ignores_standard_json_urls() -> None:
    raw = json.dumps({"url": "https://api.skyvern.com/mcp/"})
    assert _looks_like_json5_source(raw) is False


def test_looks_like_json5_source_detects_comments() -> None:
    raw = '{\n  // comment\n  "url": "https://api.skyvern.com/mcp/"\n}'
    assert _looks_like_json5_source(raw) is True


def test_looks_like_json5_source_detects_trailing_commas() -> None:
    raw = '{\n  "mcp": {\n    "servers": {},\n  }\n}'
    assert _looks_like_json5_source(raw) is True


def test_looks_like_json5_source_detects_unquoted_keys() -> None:
    raw = "{ mcp: { servers: {} } }"
    assert _looks_like_json5_source(raw) is True


def test_looks_like_json5_source_detects_single_quoted_strings() -> None:
    raw = "{ key: 'value' }"
    assert _looks_like_json5_source(raw) is True


def test_looks_like_json5_source_ignores_apostrophes_in_standard_json_strings() -> None:
    raw = json.dumps({"description": "Marc's plain JSON config"})
    assert _looks_like_json5_source(raw) is False


def test_looks_like_json5_source_ignores_escaped_quotes_inside_strings() -> None:
    raw = json.dumps({"description": 'He said "look at https://api.skyvern.com" and left'})
    assert _looks_like_json5_source(raw) is False
