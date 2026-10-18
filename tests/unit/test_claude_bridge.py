"""Regression checks for the shipped Claude Desktop API-key runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skyvern.cli import setup_commands as setup


def test_real_setup_uses_shipped_adapter_without_key_in_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "existing"}}}))
    monkeypatch.setattr(setup, "_claude_desktop_config_path", lambda: config)
    monkeypatch.setattr(setup, "_get_env_credentials", lambda: ("synthetic-key", "https://api.skyvern.com"))
    monkeypatch.setattr(setup, "capture_setup_event", lambda *a, **kw: None)
    monkeypatch.setattr(setup, "_has_node_runtime", lambda: True)
    monkeypatch.setenv("SKYVERN_MCP_CACHE_DIR", str(tmp_path / "bridge-cache"))
    result = CliRunner().invoke(setup.setup_app, ["claude", "--yes", "--url", "https://127.0.0.1:18443/mcp"])
    assert result.exit_code == 0, result.output
    servers = json.loads(config.read_text())["mcpServers"]
    entry = servers["skyvern"]
    assert Path(entry["command"]).stem.lower() == "node"
    assert entry["args"][0].endswith("proxy.mjs")
    assert Path(entry["args"][0]).is_file()
    assert "synthetic-key" not in " ".join(entry["args"])
    assert entry["env"]["SKYVERN_API_KEY"] == "synthetic-key"
    assert entry["env"]["SKYVERN_MCP_URL"] == "https://127.0.0.1:18443/mcp"
    assert servers["other"] == {"command": "existing"}


def test_dry_run_does_not_extract_runtime_or_write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "claude.json"
    cache = tmp_path / "bridge-cache"
    monkeypatch.setattr(setup, "_claude_desktop_config_path", lambda: config)
    monkeypatch.setattr(setup, "_get_env_credentials", lambda: ("synthetic-key", "https://api.skyvern.com"))
    monkeypatch.setattr(setup, "capture_setup_event", lambda *a, **kw: None)
    monkeypatch.setattr(setup, "_has_node_runtime", lambda: True)
    monkeypatch.setenv("SKYVERN_MCP_CACHE_DIR", str(cache))
    result = CliRunner().invoke(setup.setup_app, ["claude", "--dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert not config.exists() and not cache.exists()
    assert "synthetic-key" not in result.output


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/mcp", "https://user:pass@example.com/mcp", "https://example.com/mcp#x"]
)
def test_bridge_rejects_unsupported_url_without_leaking_key(url: str) -> None:
    from skyvern.cli.claude_bridge import BridgeSetupError, bridge_entry

    with pytest.raises(BridgeSetupError) as error:
        bridge_entry("synthetic-secret", url)
    assert "synthetic-secret" not in str(error.value) and "user:pass" not in str(error.value)


def test_changed_or_incomplete_cache_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli import claude_bridge as bridge

    monkeypatch.setenv("SKYVERN_MCP_CACHE_DIR", str(tmp_path / "cache"))
    bridge.ensure_runtime()
    proxy = Path(bridge.bridge_entry("test-key", "https://example.com/mcp")["args"][0])
    original = proxy.read_bytes()
    proxy.write_bytes(b"changed")
    with pytest.raises(bridge.BridgeSetupError, match="changed"):
        bridge.ensure_runtime()
    proxy.write_bytes(original)
    proxy.unlink()
    with pytest.raises(bridge.BridgeSetupError, match="incomplete"):
        bridge.ensure_runtime()


@pytest.mark.parametrize(
    "member", ["../outside", "/absolute", "C:/outside", "node_modules/../../outside", "bad\\member"]
)
def test_zip_escape_refused_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, member: str) -> None:
    import zipfile

    from skyvern.cli import claude_bridge as bridge

    archive = tmp_path / "unsafe.mcpb"
    with zipfile.ZipFile(archive, "w") as output:
        info = zipfile.ZipInfo("placeholder")
        info.filename = member
        info.orig_filename = member
        output.writestr(info, "data")
    with zipfile.ZipFile(archive) as check:
        assert check.infolist()[0].orig_filename == member, "Fixture must retain the raw unsafe member"
    monkeypatch.setattr(bridge, "_ARCHIVE", archive)
    cache = tmp_path / "cache"
    monkeypatch.setenv("SKYVERN_MCP_CACHE_DIR", str(cache))
    with pytest.raises(bridge.BridgeSetupError, match="Unsafe"):
        bridge.ensure_runtime()
    assert not cache.exists()


def test_declined_setup_does_not_materialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "claude.json"
    cache = tmp_path / "cache"
    monkeypatch.setattr(setup, "_claude_desktop_config_path", lambda: config)
    monkeypatch.setattr(setup, "_get_env_credentials", lambda: ("test-key", "https://api.skyvern.com"))
    monkeypatch.setattr(setup, "capture_setup_event", lambda *a, **kw: None)
    monkeypatch.setenv("SKYVERN_MCP_CACHE_DIR", str(cache))
    result = CliRunner().invoke(setup.setup_app, ["claude"], input="n\n")
    assert result.exit_code != 0
    assert not config.exists() and not cache.exists()


def test_missing_bundle_does_not_change_existing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.cli import claude_bridge as bridge

    config = tmp_path / "claude.json"
    before = '{"mcpServers":{"other":{"command":"existing"}}}'
    config.write_text(before)
    monkeypatch.setattr(setup, "_claude_desktop_config_path", lambda: config)
    monkeypatch.setattr(setup, "_get_env_credentials", lambda: ("test-key", "https://api.skyvern.com"))
    monkeypatch.setattr(setup, "capture_setup_event", lambda *a, **kw: None)
    monkeypatch.setattr(bridge, "_ARCHIVE", tmp_path / "missing.mcpb")
    result = CliRunner().invoke(setup.setup_app, ["claude", "--yes"])
    assert result.exit_code == 1 and config.read_text() == before


@pytest.mark.parametrize("key", ["", " ", "key with spaces", "key\tvalue", "key\nvalue", "caf\u00e9"])
def test_cli_key_contract_matches_packed_adapter(key: str) -> None:
    from skyvern.cli.claude_bridge import BridgeSetupError, bridge_entry

    with pytest.raises(BridgeSetupError):
        bridge_entry(key, "https://example.com/mcp")


def test_switch_retargets_adapter_key_and_destination_together() -> None:
    from skyvern.cli import mcp_commands as mcp
    from skyvern.cli.claude_bridge import bridge_entry

    entry = bridge_entry("first-key", "https://first.example/mcp/")
    entry["env"]["KEEP"] = "value"
    assert mcp._entry_kind(entry) == "api-key bridge"
    assert mcp._extract_entry_base_url(entry) == "https://first.example"
    patched = mcp._patch_entry_with_profile(entry, mcp.MCPProfile("second", "second-key", "https://second.example"))
    assert patched["env"]["SKYVERN_API_KEY"] == "second-key"
    assert patched["env"]["SKYVERN_MCP_URL"] == "https://second.example/mcp/"
    assert patched["env"]["KEEP"] == "value"
    assert entry["env"]["SKYVERN_MCP_URL"] == "https://first.example/mcp/"
    assert "SKYVERN_BASE_URL" not in patched["env"]


def test_switch_python_local_stdio_keeps_existing_semantics() -> None:
    from skyvern.cli import mcp_commands as mcp

    entry = {"command": "python", "args": ["-m", "skyvern", "run", "mcp"], "env": {"SKYVERN_API_KEY": "first-key"}}
    patched = mcp._patch_entry_with_profile(entry, mcp.MCPProfile("second", "second-key", "http://localhost:8000"))
    assert mcp._entry_kind(entry) == "local stdio"
    assert patched["env"]["SKYVERN_BASE_URL"] == "http://localhost:8000"
    assert "SKYVERN_MCP_URL" not in patched["env"]


def test_switch_windows_config_under_posix_path_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import PurePosixPath

    from skyvern.cli import mcp_commands as mcp

    monkeypatch.setattr(mcp, "Path", PurePosixPath)
    entry = {
        "command": "C:/Program Files/nodejs/node.exe".replace("/", chr(92)),
        "args": ["E:/cache/bundle/proxy.mjs".replace("/", chr(92))],
        "env": {"SKYVERN_API_KEY": "first-key", "SKYVERN_MCP_URL": "https://first.example/mcp/"},
    }
    assert mcp._entry_kind(entry) == "api-key bridge"
    assert mcp._extract_entry_base_url(entry) == "https://first.example"
    patched = mcp._patch_entry_with_profile(entry, mcp.MCPProfile("second", "second-key", "https://second.example"))
    assert patched["env"]["SKYVERN_API_KEY"] == "second-key"
    assert patched["env"]["SKYVERN_MCP_URL"] == "https://second.example/mcp/"
