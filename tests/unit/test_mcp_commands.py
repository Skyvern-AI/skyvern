from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import toml
from typer.testing import CliRunner

from skyvern.cli.mcp_commands import (
    MCPProfile,
    SwitchTarget,
    _apply_profile_to_target,
    _build_profile,
    _collect_profile_choices,
    _discover_switch_targets,
    _entry_kind,
    _list_profiles,
    _load_profile_from_path,
    _patch_entry_with_profile,
    _profile_to_mcp_url,
    _sanitize_prompt_response,
    _save_profile,
    mcp_app,
)


def test_save_profile_and_list_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path)

    profile = _build_profile("Work Prod", "sk-test-1234567890", "https://api.skyvern.com/")
    saved_path = _save_profile(profile)

    assert saved_path.exists()
    assert _list_profiles() == [
        MCPProfile(name="Work Prod", api_key="sk-test-1234567890", base_url="https://api.skyvern.com")
    ]


def test_save_profile_command_sanitizes_prompted_api_key_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path)
    monkeypatch.setattr("skyvern.cli.mcp_commands._get_env_credentials", lambda: ("", ""))
    monkeypatch.setattr("skyvern.cli.mcp_commands.Prompt.ask", lambda *args, **kwargs: "\x1b[Cprompt-key-1234567890")

    result = CliRunner().invoke(mcp_app, ["profile", "save", "Work Prod"])

    assert result.exit_code == 0
    saved = json.loads((tmp_path / "work-prod.json").read_text(encoding="utf-8"))
    assert saved["api_key"] == "prompt-key-1234567890"
    assert "plaintext JSON" in result.output


def test_save_profile_restricts_permissions_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permissions are not enforced on Windows in the same way.")

    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path / "profiles")

    saved_path = _save_profile(_build_profile("Work Prod", "sk-test-1234567890", "https://api.skyvern.com"))

    assert stat.S_IMODE(saved_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(saved_path.parent.stat().st_mode) == 0o700


def test_load_profile_from_path_rejects_non_string_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "invalid.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Work Prod",
                "api_key": {"secret": "bad"},
                "base_url": "https://api.skyvern.com",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="field 'api_key' must be a string"):
        _load_profile_from_path(profile_path)


def test_apply_profile_to_target_updates_local_entry_and_creates_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.json"
    original_config = {
        "mcpServers": {
            "Skyvern": {
                "command": "/opt/homebrew/bin/python3.11",
                "args": ["-m", "skyvern", "run", "mcp"],
                "env": {
                    "SKYVERN_BASE_URL": "http://localhost:8000",
                    "SKYVERN_API_KEY": "old-key",
                    "OTHER": "keep-me",
                },
            }
        }
    }
    config_path.write_text(json.dumps(original_config, indent=2) + "\n", encoding="utf-8")

    target = SwitchTarget(
        name="Cursor",
        config_path=config_path,
        entry_key="Skyvern",
        entry=original_config["mcpServers"]["Skyvern"],
    )
    profile = _build_profile("Prod", "new-key-1234567890", "https://api.skyvern.com")

    changed, backup_path = _apply_profile_to_target(target, profile)

    assert changed is True
    assert backup_path is not None
    assert backup_path.exists()

    written = json.loads(config_path.read_text(encoding="utf-8"))
    written_entry = written["mcpServers"]["Skyvern"]
    assert written_entry["command"] == "/opt/homebrew/bin/python3.11"
    assert written_entry["args"] == ["-m", "skyvern", "run", "mcp"]
    assert written_entry["env"]["SKYVERN_API_KEY"] == "new-key-1234567890"
    assert written_entry["env"]["SKYVERN_BASE_URL"] == "https://api.skyvern.com"
    assert written_entry["env"]["OTHER"] == "keep-me"

    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup["mcpServers"]["Skyvern"]["env"]["SKYVERN_API_KEY"] == "old-key"


def test_patch_entry_with_profile_updates_mcp_remote_bridge() -> None:
    profile = _build_profile("Cloud Alt", "new-key-1234567890", "https://alt.skyvern.example")
    entry = {
        "command": "npx",
        "args": [
            "mcp-remote",
            "https://api.skyvern.com/mcp/",
            "--header",
            "x-api-key:old-key",
            "--transport",
            "stdio",
        ],
    }

    patched = _patch_entry_with_profile(entry, profile)

    assert patched["args"][0] == "mcp-remote"
    assert patched["args"][1] == "https://alt.skyvern.example/mcp/"
    assert patched["args"].count("--header") == 1
    assert "x-api-key:new-key-1234567890" in patched["args"]
    assert "x-api-key:old-key" not in patched["args"]


def test_patch_entry_with_profile_updates_mcp_remote_bridge_even_with_env() -> None:
    profile = _build_profile("Cloud Alt", "new-key-1234567890", "https://alt.skyvern.example")
    entry = {
        "command": "npx",
        "args": [
            "mcp-remote",
            "https://api.skyvern.com/mcp/",
            "--header",
            "x-api-key:old-key",
        ],
        "env": {
            "DEBUG": "1",
            "SKYVERN_API_KEY": "do-not-touch",
        },
    }

    patched = _patch_entry_with_profile(entry, profile)

    assert _entry_kind(entry) == "mcp-remote bridge"
    assert "x-api-key:new-key-1234567890" in patched["args"]
    assert patched["env"]["SKYVERN_API_KEY"] == "do-not-touch"


def test_entry_kind_requires_exact_npx_for_mcp_remote_bridge() -> None:
    entry = {
        "command": "npx-wrapper",
        "args": ["mcp-remote", "https://api.skyvern.com/mcp/"],
    }

    assert _entry_kind(entry) == "unsupported"


def test_sanitize_prompt_response_strips_arrow_escape_noise() -> None:
    assert _sanitize_prompt_response("\x1b[C\x1b[C\x1b[Call") == "all"


def test_profile_to_mcp_url_normalizes_user_base_url() -> None:
    assert _profile_to_mcp_url("https://api.skyvern.com") == "https://api.skyvern.com/mcp/"
    assert _profile_to_mcp_url("https://api.skyvern.com/") == "https://api.skyvern.com/mcp/"
    assert _profile_to_mcp_url("https://api.skyvern.com/mcp/") == "https://api.skyvern.com/mcp/"


def test_apply_profile_to_target_updates_codex_entry_and_creates_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "model": "gpt-5.4",
                "mcp_servers": {
                    "skyvern": {
                        "url": "https://api.skyvern.com/mcp/",
                        "http_headers": {"x-api-key": "old-key"},
                        "startup_timeout_sec": 30,
                        "tool_timeout_sec": 120,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    target = SwitchTarget(
        name="Codex",
        config_path=config_path,
        entry_key="skyvern",
        entry={
            "url": "https://api.skyvern.com/mcp/",
            "http_headers": {"x-api-key": "old-key"},
            "startup_timeout_sec": 30,
            "tool_timeout_sec": 120,
        },
        config_format="codex_toml",
    )
    profile = _build_profile("Prod", "new-key-1234567890", "https://alt.skyvern.example")

    changed, backup_path = _apply_profile_to_target(target, profile)

    assert changed is True
    assert backup_path is not None
    assert backup_path.exists()

    written = toml.loads(config_path.read_text(encoding="utf-8"))
    written_entry = written["mcp_servers"]["skyvern"]
    assert written_entry["url"] == "https://alt.skyvern.example/mcp/"
    assert written_entry["http_headers"]["x-api-key"] == "new-key-1234567890"
    assert written_entry["startup_timeout_sec"] == 30
    assert written_entry["tool_timeout_sec"] == 120

    backup = toml.loads(backup_path.read_text(encoding="utf-8"))
    assert backup["mcp_servers"]["skyvern"]["http_headers"]["x-api-key"] == "old-key"


def test_collect_profile_choices_includes_env_and_existing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path / "profiles")
    monkeypatch.setattr(
        "skyvern.cli.mcp_commands._get_env_credentials",
        lambda: ("env-key-1234567890", "https://api.skyvern.com"),
    )

    target = SwitchTarget(
        name="Cursor",
        config_path=tmp_path / "mcp.json",
        entry_key="Skyvern",
        entry={
            "command": "npx",
            "args": ["mcp-remote", "https://staging.skyvern.example/mcp/", "--header", "x-api-key:staging-key"],
        },
    )

    choices = _collect_profile_choices([target])

    assert len(choices) == 2
    assert any(choice.label == "Current environment" for choice in choices)
    assert any(choice.label == "Cursor current config" for choice in choices)


def test_switch_uses_env_candidate_without_saved_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "Skyvern": {
                        "command": "skyvern",
                        "args": ["run", "mcp"],
                        "env": {
                            "SKYVERN_BASE_URL": "http://localhost:8000",
                            "SKYVERN_API_KEY": "old-key",
                        },
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target = SwitchTarget(
        name="Cursor",
        config_path=config_path,
        entry_key="Skyvern",
        entry={
            "command": "skyvern",
            "args": ["run", "mcp"],
            "env": {
                "SKYVERN_BASE_URL": "http://localhost:8000",
                "SKYVERN_API_KEY": "old-key",
            },
        },
    )

    monkeypatch.setattr("skyvern.cli.mcp_commands._discover_switch_targets", lambda: ([target], []))
    monkeypatch.setattr(
        "skyvern.cli.mcp_commands._get_env_credentials",
        lambda: ("env-key-1234567890", "https://api.skyvern.com"),
    )
    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path / "profiles")

    result = CliRunner().invoke(mcp_app, ["switch"], input="1\ny\n")

    assert result.exit_code == 0
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["Skyvern"]["env"]["SKYVERN_API_KEY"] == "env-key-1234567890"
    assert written["mcpServers"]["Skyvern"]["env"]["SKYVERN_BASE_URL"] == "https://api.skyvern.com"


def test_switch_manual_entry_does_not_prompt_for_profile_name_and_normalizes_remote_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "claude.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "Skyvern": {
                        "type": "http",
                        "url": "https://old.skyvern.example/mcp/",
                        "headers": {
                            "x-api-key": "old-key",
                        },
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target = SwitchTarget(
        name="Claude Code (global)",
        config_path=config_path,
        entry_key="Skyvern",
        entry={
            "type": "http",
            "url": "https://old.skyvern.example/mcp/",
            "headers": {
                "x-api-key": "old-key",
            },
        },
    )

    monkeypatch.setattr("skyvern.cli.mcp_commands._discover_switch_targets", lambda: ([target], []))
    monkeypatch.setattr("skyvern.cli.mcp_commands._get_env_credentials", lambda: ("", "https://api.skyvern.com"))
    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path / "profiles")
    prompt_calls: list[str] = []
    prompt_values = iter(
        [
            "2",
            "manual-key-1234567890",
            "https://alt.skyvern.example/mcp/",
        ]
    )

    def fake_prompt(prompt: str, *, default: str | None = None, password: bool = False) -> str:
        prompt_calls.append(prompt)
        return next(prompt_values)

    monkeypatch.setattr("skyvern.cli.mcp_commands._prompt_text", fake_prompt)
    monkeypatch.setattr("skyvern.cli.mcp_commands.Confirm.ask", lambda *args, **kwargs: True)

    result = CliRunner().invoke(mcp_app, ["switch"])

    assert result.exit_code == 0
    assert "Available switch sources" in result.output
    assert "Available switch profiles" not in result.output
    assert "Selected source:" in result.output
    assert "Remote MCP URL:" in result.output
    assert "Profile name" not in prompt_calls

    written = json.loads(config_path.read_text(encoding="utf-8"))
    entry = written["mcpServers"]["Skyvern"]
    assert entry["headers"]["x-api-key"] == "manual-key-1234567890"
    assert entry["url"] == "https://alt.skyvern.example/mcp/"


def test_switch_accepts_arrow_key_noise_in_target_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_a = tmp_path / "claude.json"
    config_b = tmp_path / "codex.json"
    for config_path in (config_a, config_b):
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "Skyvern": {
                            "command": "skyvern",
                            "args": ["run", "mcp"],
                            "env": {
                                "SKYVERN_BASE_URL": "http://localhost:8000",
                                "SKYVERN_API_KEY": "old-key",
                            },
                        }
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    targets = [
        SwitchTarget(
            name="Claude Code (global)",
            config_path=config_a,
            entry_key="Skyvern",
            entry={
                "command": "skyvern",
                "args": ["run", "mcp"],
                "env": {
                    "SKYVERN_BASE_URL": "http://localhost:8000",
                    "SKYVERN_API_KEY": "old-key",
                },
            },
        ),
        SwitchTarget(
            name="Codex",
            config_path=config_b,
            entry_key="Skyvern",
            entry={
                "command": "skyvern",
                "args": ["run", "mcp"],
                "env": {
                    "SKYVERN_BASE_URL": "http://localhost:8000",
                    "SKYVERN_API_KEY": "old-key",
                },
            },
        ),
    ]

    monkeypatch.setattr("skyvern.cli.mcp_commands._discover_switch_targets", lambda: (targets, []))
    monkeypatch.setattr(
        "skyvern.cli.mcp_commands._select_profile",
        lambda profile_name, discovered: MCPProfile(
            name="Env",
            api_key="env-key-1234567890",
            base_url="https://api.skyvern.com",
        ),
    )
    monkeypatch.setattr("skyvern.cli.mcp_commands._profile_store_dir", lambda: tmp_path / "profiles")

    result = CliRunner().invoke(mcp_app, ["switch"], input="\x1b[C\x1b[C\x1b[Call\ny\n")

    assert result.exit_code == 0
    for config_path in (config_a, config_b):
        written = json.loads(config_path.read_text(encoding="utf-8"))
        assert written["mcpServers"]["Skyvern"]["env"]["SKYVERN_API_KEY"] == "env-key-1234567890"
        assert written["mcpServers"]["Skyvern"]["env"]["SKYVERN_BASE_URL"] == "https://api.skyvern.com"


def test_discover_switch_targets_finds_claude_code_and_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_config = tmp_path / ".claude.json"
    global_config.write_text(
        json.dumps({"mcpServers": {"skyvern": {"type": "http", "url": "https://api.skyvern.com/mcp/"}}}) + "\n",
        encoding="utf-8",
    )
    project_config = tmp_path / ".mcp.json"
    project_config.write_text(
        json.dumps({"mcpServers": {"Skyvern": {"command": "skyvern", "args": ["run", "mcp"], "env": {}}}}) + "\n",
        encoding="utf-8",
    )
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        toml.dumps({"mcp_servers": {"skyvern": {"url": "https://api.skyvern.com/mcp/"}}}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("skyvern.cli.mcp_commands._claude_code_global_config_path", lambda: global_config)
    monkeypatch.setattr("skyvern.cli.mcp_commands._claude_code_project_config_path", lambda: project_config)
    monkeypatch.setattr(
        "skyvern.cli.mcp_commands._claude_desktop_config_path", lambda: tmp_path / "missing-claude.json"
    )
    monkeypatch.setattr("skyvern.cli.mcp_commands._cursor_config_path", lambda: tmp_path / "missing-cursor.json")
    monkeypatch.setattr("skyvern.cli.mcp_commands._windsurf_config_path", lambda: tmp_path / "missing-windsurf.json")
    monkeypatch.setattr("skyvern.cli.mcp_commands._codex_config_path", lambda: codex_config)
    monkeypatch.setattr("skyvern.cli.mcp_commands._hermes_config_path", lambda: tmp_path / "missing-hermes.yaml")

    discovered, missing = _discover_switch_targets()

    discovered_by_name = {target.name: target for target in discovered}
    assert "Claude Code (global)" in discovered_by_name
    assert "Claude Code (project)" in discovered_by_name
    assert "Codex" in discovered_by_name
    assert discovered_by_name["Codex"].config_format == "codex_toml"
    assert discovered_by_name["Codex"].entry_key == "skyvern"
    assert {name for name, _ in missing} == {"Claude Desktop", "Cursor", "Windsurf", "Hermes"}
