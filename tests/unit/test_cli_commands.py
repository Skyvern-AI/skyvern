"""Tests for CLI commands infrastructure: _state.py and _output.py."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# Browser command helpers and command behavior
# ---------------------------------------------------------------------------


class TestBrowserCommandGuards:
    def test_resolve_ai_target_requires_selector_or_intent(self) -> None:
        from skyvern.cli.commands.browser import _resolve_ai_target
        from skyvern.cli.core.guards import GuardError

        with pytest.raises(GuardError, match="Must provide intent, selector, or both"):
            _resolve_ai_target(None, None, operation="click")

    def test_validate_wait_state_rejects_invalid(self) -> None:
        from skyvern.cli.commands.browser import _validate_wait_state
        from skyvern.cli.core.guards import GuardError

        with pytest.raises(GuardError, match="Invalid state"):
            _validate_wait_state("bad-state")


class TestBrowserCommands:
    def test_session_get_outputs_session_details(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        session_obj = SimpleNamespace(
            browser_session_id="pbs_123",
            status="active",
            started_at=datetime(2026, 2, 17, 12, 0, tzinfo=timezone.utc),
            completed_at=None,
            timeout=60,
            runnable_id=None,
        )
        skyvern = SimpleNamespace(get_browser_session=AsyncMock(return_value=session_obj))
        monkeypatch.setattr(browser_cmd, "get_skyvern", lambda: skyvern)
        monkeypatch.setattr(browser_cmd, "load_state", lambda: CLIState(session_id="pbs_123", mode="cloud"))

        browser_cmd.session_get(session="pbs_123", json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["action"] == "session_get"
        assert parsed["data"]["session_id"] == "pbs_123"
        assert parsed["data"]["is_current"] is True

    def test_evaluate_blocks_password_js_before_connection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        monkeypatch.setattr(
            browser_cmd,
            "_resolve_connection",
            lambda _session, _cdp: (_ for _ in ()).throw(AssertionError("should not resolve connection")),
        )

        with pytest.raises(SystemExit, match="1"):
            browser_cmd.evaluate(
                expression='document.querySelector("input[type=password]").value = ""', json_output=True
            )

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert "Cannot set password field values" in parsed["error"]["message"]

    def test_click_requires_target_before_connection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        monkeypatch.setattr(
            browser_cmd,
            "_resolve_connection",
            lambda _session, _cdp: (_ for _ in ()).throw(AssertionError("should not resolve connection")),
        )

        with pytest.raises(SystemExit, match="1"):
            browser_cmd.click(
                intent=None,
                selector=None,
                session=None,
                cdp=None,
                timeout=30000,
                button=None,
                click_count=None,
                json_output=True,
            )

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert "Must provide intent, selector, or both" in parsed["error"]["message"]

    def test_click_with_intent_uses_proactive_ai_mode(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        page = MagicMock()
        page.click = AsyncMock(return_value="xpath=//button[@id='submit']")
        browser = SimpleNamespace(get_working_page=AsyncMock(return_value=page))

        monkeypatch.setattr(
            browser_cmd,
            "_resolve_connection",
            lambda _session, _cdp: browser_cmd.ConnectionTarget(mode="cloud", session_id="pbs_123"),
        )
        monkeypatch.setattr(browser_cmd, "_connect_browser", AsyncMock(return_value=browser))

        browser_cmd.click(
            intent="the Submit button",
            selector=None,
            session="pbs_123",
            cdp=None,
            timeout=30000,
            button=None,
            click_count=None,
            json_output=True,
        )

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["action"] == "click"
        assert parsed["data"]["ai_mode"] == "proactive"
        assert parsed["data"]["resolved_selector"] == "xpath=//button[@id='submit']"

    def test_wait_rejects_invalid_state_before_connection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        monkeypatch.setattr(
            browser_cmd,
            "_resolve_connection",
            lambda _session, _cdp: (_ for _ in ()).throw(AssertionError("should not resolve connection")),
        )

        with pytest.raises(SystemExit, match="1"):
            browser_cmd.wait(state="bad-state", time_ms=1000, json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert "Invalid state" in parsed["error"]["message"]


# ---------------------------------------------------------------------------
# Workflow command behavior
# ---------------------------------------------------------------------------


class TestWorkflowCommands:
    def test_workflow_get_outputs_mcp_envelope_in_json_mode(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        expected = {
            "ok": True,
            "action": "skyvern_workflow_get",
            "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
            "data": {"workflow_permanent_id": "wpid_123"},
            "artifacts": [],
            "timing_ms": {},
            "warnings": [],
            "error": None,
        }
        tool = AsyncMock(return_value=expected)
        monkeypatch.setattr(workflow_cmd, "tool_workflow_get", tool)

        workflow_cmd.workflow_get(workflow_id="wpid_123", version=2, json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed == expected
        assert tool.await_args.kwargs == {"workflow_id": "wpid_123", "version": 2}

    def test_workflow_create_reads_definition_from_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        definition_file = tmp_path / "workflow.json"
        definition_text = '{"title": "Example", "workflow_definition": {"blocks": []}}'
        definition_file.write_text(definition_text)

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_workflow_create",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"workflow_permanent_id": "wpid_new"},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(workflow_cmd, "tool_workflow_create", tool)

        workflow_cmd.workflow_create(
            definition=f"@{definition_file}",
            definition_format="json",
            folder_id="fld_123",
            json_output=True,
        )

        assert tool.await_args.kwargs == {
            "definition": definition_text,
            "format": "json",
            "folder_id": "fld_123",
        }
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["workflow_permanent_id"] == "wpid_new"

    def test_workflow_run_reads_params_file_and_maps_options(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        params_file = tmp_path / "params.json"
        params_file.write_text('{"company": "Acme"}')

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_workflow_run",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"run_id": "wr_123", "status": "queued"},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(workflow_cmd, "tool_workflow_run", tool)

        workflow_cmd.workflow_run(
            workflow_id="wpid_123",
            params=f"@{params_file}",
            session="pbs_456",
            webhook="https://example.com/webhook",
            proxy="RESIDENTIAL",
            wait=True,
            timeout=450,
            json_output=True,
        )

        assert tool.await_args.kwargs == {
            "workflow_id": "wpid_123",
            "parameters": '{"company": "Acme"}',
            "browser_session_id": "pbs_456",
            "webhook_url": "https://example.com/webhook",
            "proxy_location": "RESIDENTIAL",
            "wait": True,
            "timeout_seconds": 450,
        }
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["run_id"] == "wr_123"

    def test_workflow_status_json_error_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        tool = AsyncMock(
            return_value={
                "ok": False,
                "action": "skyvern_workflow_status",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": None,
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": {
                    "code": "RUN_NOT_FOUND",
                    "message": "Run 'wr_missing' not found",
                    "hint": "Verify the run ID",
                    "details": {},
                },
            }
        )
        monkeypatch.setattr(workflow_cmd, "tool_workflow_status", tool)

        with pytest.raises(SystemExit, match="1"):
            workflow_cmd.workflow_status(run_id="wr_missing", json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "RUN_NOT_FOUND"

    def test_workflow_update_missing_definition_file_raises_bad_parameter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        tool = AsyncMock()
        monkeypatch.setattr(workflow_cmd, "tool_workflow_update", tool)
        missing_file = tmp_path / "missing-definition.json"

        with pytest.raises(typer.BadParameter, match="Unable to read definition file"):
            workflow_cmd.workflow_update(
                workflow_id="wpid_123",
                definition=f"@{missing_file}",
                definition_format="json",
                json_output=False,
            )

        tool.assert_not_called()
# ---------------------------------------------------------------------------
# PR C parity command behavior
# ---------------------------------------------------------------------------


class TestCredentialParityCommands:
    def test_credential_list_maps_options(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli import credential as credential_cmd

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_credential_list",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"credentials": [], "page": 2, "page_size": 25, "count": 0, "has_more": False},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(credential_cmd, "tool_credential_list", tool)

        credential_cmd.credential_list(page=2, page_size=25, json_output=True)

        assert tool.await_args.kwargs == {"page": 2, "page_size": 25}
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["action"] == "skyvern_credential_list"

    def test_credential_get_json_error_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import credential as credential_cmd

        tool = AsyncMock(
            return_value={
                "ok": False,
                "action": "skyvern_credential_get",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": None,
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "Invalid credential_id format: 'bad'",
                    "hint": "Credential IDs start with cred_",
                    "details": {},
                },
            }
        )
        monkeypatch.setattr(credential_cmd, "tool_credential_get", tool)

        with pytest.raises(SystemExit, match="1"):
            credential_cmd.credential_get(credential_id="bad", json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert "Invalid credential_id format" in parsed["error"]["message"]

    def test_credential_delete_maps_options(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import credential as credential_cmd

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_credential_delete",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"credential_id": "cred_123", "deleted": True},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(credential_cmd, "tool_credential_delete", tool)

        credential_cmd.credential_delete(credential_id="cred_123", json_output=True)

        assert tool.await_args.kwargs == {"credential_id": "cred_123"}
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["deleted"] is True


class TestBlockParityCommands:
    def test_block_schema_passes_block_type(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import block as block_cmd

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_block_schema",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"block_type": "navigation", "schema": {"type": "object"}},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(block_cmd, "tool_block_schema", tool)

        block_cmd.block_schema(block_type="navigation", json_output=True)

        assert tool.await_args.kwargs == {"block_type": "navigation"}
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["block_type"] == "navigation"

    def test_block_validate_reads_json_from_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import block as block_cmd

        block_file = tmp_path / "block.json"
        block_file.write_text('{"block_type":"navigation","label":"step1","navigation_goal":"Go to page"}')

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_block_validate",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"valid": True, "block_type": "navigation", "label": "step1"},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(block_cmd, "tool_block_validate", tool)

        block_cmd.block_validate(block_json=f"@{block_file}", json_output=True)

        assert tool.await_args.kwargs == {
            "block_json": '{"block_type":"navigation","label":"step1","navigation_goal":"Go to page"}'
        }
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["valid"] is True


class TestBrowserPRCCommands:
    def test_run_task_uses_resolved_connection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        monkeypatch.setattr(
            browser_cmd,
            "_resolve_connection",
            lambda _session, _cdp: browser_cmd.ConnectionTarget(mode="cloud", session_id="pbs_123"),
        )
        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_run_task",
                "browser_context": {"mode": "cloud", "session_id": "pbs_123", "cdp_url": None},
                "data": {"run_id": "run_123", "status": "completed"},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(browser_cmd, "tool_run_task", tool)

        browser_cmd.run_task(
            prompt="Find the latest headline",
            session=None,
            cdp=None,
            url="https://news.ycombinator.com",
            data_extraction_schema='{"type":"object"}',
            max_steps=5,
            timeout_seconds=240,
            json_output=True,
        )

        assert tool.await_args.kwargs == {
            "prompt": "Find the latest headline",
            "session_id": "pbs_123",
            "cdp_url": None,
            "url": "https://news.ycombinator.com",
            "data_extraction_schema": '{"type":"object"}',
            "max_steps": 5,
            "timeout_seconds": 240,
        }
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["run_id"] == "run_123"

    def test_login_json_error_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        monkeypatch.setattr(
            browser_cmd,
            "_resolve_connection",
            lambda _session, _cdp: browser_cmd.ConnectionTarget(mode="cloud", session_id="pbs_abc"),
        )
        tool = AsyncMock(
            return_value={
                "ok": False,
                "action": "skyvern_login",
                "browser_context": {"mode": "cloud", "session_id": "pbs_abc", "cdp_url": None},
                "data": None,
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "Missing required fields for credential_type='skyvern': credential_id",
                    "hint": "Provide: credential_id",
                    "details": {},
                },
            }
        )
        monkeypatch.setattr(browser_cmd, "tool_login", tool)

        with pytest.raises(SystemExit, match="1"):
            browser_cmd.login(
                credential_type="skyvern",
                session=None,
                cdp=None,
                credential_id=None,
                json_output=True,
            )

        kwargs = tool.await_args.kwargs
        assert kwargs["session_id"] == "pbs_abc"
        assert kwargs["credential_type"] == "skyvern"
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert "Missing required fields" in parsed["error"]["message"]


class TestParityErrorFormatting:
    def test_credential_emit_tool_result_handles_none_message_and_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli import credential as credential_cmd

        captured: dict[str, str | bool] = {}

        def _fake_output_error(message: str, *, hint: str = "", json_mode: bool = False, exit_code: int = 1) -> None:
            captured["message"] = message
            captured["hint"] = hint
            captured["json_mode"] = json_mode
            raise SystemExit(exit_code)

        monkeypatch.setattr(credential_cmd, "output_error", _fake_output_error)

        with pytest.raises(SystemExit, match="1"):
            credential_cmd._emit_tool_result(
                {"ok": False, "error": {"message": None, "hint": None}},
                json_output=False,
            )

        assert captured == {"message": "Unknown error", "hint": "", "json_mode": False}

    def test_block_emit_tool_result_handles_none_message_and_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli import block as block_cmd

        captured: dict[str, str | bool] = {}

        def _fake_output_error(message: str, *, hint: str = "", json_mode: bool = False, exit_code: int = 1) -> None:
            captured["message"] = message
            captured["hint"] = hint
            captured["json_mode"] = json_mode
            raise SystemExit(exit_code)

        monkeypatch.setattr(block_cmd, "output_error", _fake_output_error)

        with pytest.raises(SystemExit, match="1"):
            block_cmd._emit_tool_result(
                {"ok": False, "error": {"message": None, "hint": None}},
                json_output=False,
            )

        assert captured == {"message": "Unknown error", "hint": "", "json_mode": False}

    def test_browser_emit_tool_result_handles_none_message_and_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands import browser as browser_cmd

        captured: dict[str, str | bool] = {}

        def _fake_output_error(message: str, *, hint: str = "", json_mode: bool = False, exit_code: int = 1) -> None:
            captured["message"] = message
            captured["hint"] = hint
            captured["json_mode"] = json_mode
            raise SystemExit(exit_code)

        monkeypatch.setattr(browser_cmd, "output_error", _fake_output_error)

        with pytest.raises(SystemExit, match="1"):
            browser_cmd._emit_tool_result(
                {"ok": False, "error": {"message": None, "hint": None}},
                json_output=False,
                action="login",
            )

        assert captured == {"message": "Unknown error", "hint": "", "json_mode": False}
