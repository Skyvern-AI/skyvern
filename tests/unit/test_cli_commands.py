"""Tests for CLI commands infrastructure: _state.py and _output.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

from skyvern.cli.commands._state import CLIState, clear_state, load_state, save_state

# ---------------------------------------------------------------------------
# _state.py
# ---------------------------------------------------------------------------


def _patch_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("skyvern.cli.commands._state.STATE_DIR", tmp_path)
    monkeypatch.setattr("skyvern.cli.commands._state.STATE_FILE", tmp_path / "state.json")


class TestCLIState:
    def test_save_load_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_state_dir(monkeypatch, tmp_path)
        save_state(CLIState(session_id="pbs_123", cdp_url=None, mode="cloud"))
        loaded = load_state()
        assert loaded is not None
        assert loaded.session_id == "pbs_123"
        assert loaded.cdp_url is None
        assert loaded.mode == "cloud"

    def test_save_load_roundtrip_cdp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_state_dir(monkeypatch, tmp_path)
        save_state(CLIState(session_id=None, cdp_url="ws://localhost:9222/devtools/browser/abc", mode="cdp"))
        loaded = load_state()
        assert loaded is not None
        assert loaded.session_id is None
        assert loaded.cdp_url == "ws://localhost:9222/devtools/browser/abc"
        assert loaded.mode == "cdp"

    def test_load_returns_none_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("skyvern.cli.commands._state.STATE_FILE", tmp_path / "nonexistent.json")
        assert load_state() is None

    def test_24h_ttl_expires(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_state_dir(monkeypatch, tmp_path)
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "session_id": "pbs_old",
                    "mode": "cloud",
                    "created_at": "2020-01-01T00:00:00+00:00",
                }
            )
        )
        assert load_state() is None

    def test_clear_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_state_dir(monkeypatch, tmp_path)
        save_state(CLIState(session_id="pbs_123"))
        clear_state()
        assert not (tmp_path / "state.json").exists()

    def test_load_ignores_corrupt_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        state_file = tmp_path / "state.json"
        monkeypatch.setattr("skyvern.cli.commands._state.STATE_FILE", state_file)
        state_file.write_text("not-json")
        assert load_state() is None


# ---------------------------------------------------------------------------
# _output.py
# ---------------------------------------------------------------------------


class TestOutput:
    def test_json_envelope(self, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli.commands._output import output

        output({"key": "value"}, action="test", json_mode=True)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["action"] == "test"
        assert parsed["data"]["key"] == "value"

    def test_json_error(self, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli.commands._output import output_error

        with pytest.raises(SystemExit, match="1"):
            output_error("bad thing", hint="fix it", json_mode=True)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert parsed["error"]["message"] == "bad thing"


# ---------------------------------------------------------------------------
# Connection resolution
# ---------------------------------------------------------------------------


class TestResolveConnection:
    def test_explicit_session_wins(self) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        result = _resolve_connection("pbs_explicit", None)
        assert result.mode == "cloud"
        assert result.session_id == "pbs_explicit"
        assert result.cdp_url is None

    def test_explicit_cdp_wins(self) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        result = _resolve_connection(None, "ws://localhost:9222/devtools/browser/abc")
        assert result.mode == "cdp"
        assert result.session_id is None
        assert result.cdp_url == "ws://localhost:9222/devtools/browser/abc"

    def test_rejects_both_connection_flags(self) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        with pytest.raises(typer.BadParameter, match="Pass only one of --session or --cdp"):
            _resolve_connection("pbs_explicit", "ws://localhost:9222/devtools/browser/abc")

    def test_state_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        _patch_state_dir(monkeypatch, tmp_path)
        save_state(CLIState(session_id="pbs_from_state", mode="cloud"))
        result = _resolve_connection(None, None)
        assert result.mode == "cloud"
        assert result.session_id == "pbs_from_state"

    def test_state_fallback_cdp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        _patch_state_dir(monkeypatch, tmp_path)
        save_state(CLIState(session_id=None, cdp_url="ws://localhost:9222/devtools/browser/abc", mode="cdp"))
        result = _resolve_connection(None, None)
        assert result.mode == "cdp"
        assert result.cdp_url == "ws://localhost:9222/devtools/browser/abc"

    def test_no_session_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        monkeypatch.setattr("skyvern.cli.commands._state.STATE_FILE", tmp_path / "nonexistent.json")
        with pytest.raises(typer.BadParameter, match="No active browser connection"):
            _resolve_connection(None, None)
