"""Tests for CLI commands infrastructure: _state.py and _output.py."""

from __future__ import annotations

import importlib
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import typer
from typer.testing import CliRunner

from skyvern.cli.commands import browser as cli_browser
from skyvern.cli.commands import cli_app
from skyvern.cli.commands._state import CLIState, clear_state, load_state, save_state
from skyvern.cli.core.guards import STALE_FRAME_HINT
from skyvern.cli.status import _status_data
from skyvern.exceptions import StaleFrameSelectionError
from tests.unit._mcp_browser_fakes import StaleScopePage

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
# status.py
# ---------------------------------------------------------------------------


class TestStatusCommands:
    def test_status_start_commands_are_accepted_by_cli_parser(self) -> None:
        runner = CliRunner()

        for row in _status_data():
            command_parts = shlex.split(row["start_command"])
            assert command_parts[0] == "skyvern"

            result = runner.invoke(cli_app, [*command_parts[1:], "--help"])

            assert result.exit_code == 0, result.output


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

        cdp_url = "wss://browser.test/devtools/browser/pbs_123"
        result = _resolve_connection(None, cdp_url)
        assert result.mode == "cdp"
        assert result.session_id is None
        assert result.cdp_url == cdp_url

    def test_bare_session_id_in_explicit_cdp_routes_to_cloud(self) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        result = _resolve_connection(None, "pbs_explicit")
        assert result.mode == "cloud"
        assert result.session_id == "pbs_explicit"
        assert result.cdp_url is None

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

    @pytest.mark.parametrize("mode", ["cdp", None])
    def test_state_fallback_cdp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        _patch_state_dir(monkeypatch, tmp_path)
        cdp_url = "wss://browser.test/devtools/browser/pbs_123"
        save_state(CLIState(session_id=None, cdp_url=cdp_url, mode=mode))
        result = _resolve_connection(None, None)
        assert result.mode == "cdp"
        assert result.cdp_url == cdp_url

    @pytest.mark.parametrize("mode", ["cdp", None])
    def test_bare_session_id_in_cdp_state_routes_to_cloud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
    ) -> None:
        from skyvern.cli.commands.browser import _resolve_connection

        _patch_state_dir(monkeypatch, tmp_path)
        save_state(CLIState(session_id=None, cdp_url="pbs_from_state", mode=mode))
        result = _resolve_connection(None, None)
        assert result.mode == "cloud"
        assert result.session_id == "pbs_from_state"
        assert result.cdp_url is None

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

    @pytest.mark.parametrize("clear", [True, False], ids=["fill", "append"])
    def test_type_with_intent_refuses_top_document_password_match(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, clear: bool
    ) -> None:
        top_match = MagicMock()
        top_match.first = top_match
        top_match.evaluate = AsyncMock(return_value=True)
        top_match.type = AsyncMock()
        raw_page = MagicMock()
        raw_page.locator = MagicMock(return_value=top_match)
        frame_match = MagicMock()
        frame_match.first = frame_match
        frame_match.evaluate = AsyncMock(return_value=False)
        active_frame = MagicMock()
        active_frame.locator = MagicMock(return_value=frame_match)
        page = MagicMock()
        page.page = raw_page
        page.locator_scope = active_frame
        page.locator = MagicMock(return_value=top_match)
        browser = SimpleNamespace(get_working_page=AsyncMock(return_value=page))
        monkeypatch.setattr(cli_browser, "_resolve_connection", MagicMock())
        monkeypatch.setattr(cli_browser, "_connect_browser", AsyncMock(return_value=browser))
        monkeypatch.setattr(cli_browser, "_apply_cli_frame_state", AsyncMock())
        monkeypatch.setattr(cli_browser, "capture_cli_tool_call", MagicMock())

        with pytest.raises(SystemExit):
            cli_browser.type_text(
                text="opaque-value",
                intent="the promo code box",
                selector=".shared-field",
                session=None,
                cdp=None,
                timeout=1000,
                clear=clear,
                delay=None,
                json_output=True,
            )

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert "password" in parsed["error"]["message"].lower()
        top_match.type.assert_not_awaited()
        page.fill.assert_not_called()


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
            app_url="https://app.skyvern.com/browser-session/pbs_123",
            recordings=[],
            downloaded_files=[],
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

        capture_mock = MagicMock()
        monkeypatch.setattr(browser_cmd, "capture_cli_tool_call", capture_mock)
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
        capture_mock.assert_not_called()

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
        capture_mock = MagicMock()
        monkeypatch.setattr(browser_cmd, "capture_cli_tool_call", capture_mock)

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
        capture_mock.assert_called_once_with("skyvern_click", ok=True)

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
    def test_workflow_list_query_alias_maps_to_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli import workflow as workflow_cmd

        monkeypatch.setattr(workflow_cmd, "prepare_cli_runtime", lambda **_: None)
        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_workflow_list",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"workflows": [], "page": 1, "page_size": 10, "count": 0, "has_more": False},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(workflow_cmd, "tool_workflow_list", tool)

        result = CliRunner().invoke(workflow_cmd.workflow_app, ["list", "--query", "invoice", "--json"])

        assert result.exit_code == 0, result.output
        assert tool.await_args.kwargs == {
            "search": "invoice",
            "page": 1,
            "page_size": 10,
            "only_workflows": False,
        }
        parsed = json.loads(result.output)
        assert parsed["ok"] is True

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

        workflow_cmd.workflow_get(workflow_id="wpid_123", version=2, definition_file=None, json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        # emit_tool_result adds schema_version and warnings defaults to the output copy
        assert parsed["ok"] is True
        assert parsed["data"] == expected["data"]
        assert parsed["schema_version"] == "1.0"
        assert tool.await_args.kwargs == {"workflow_id": "wpid_123", "version": 2}

    def test_workflow_get_definition_file_feeds_directly_into_update(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio

        from skyvern.cli import workflow as workflow_cmd
        from skyvern.cli.mcp_tools import workflow as workflow_tools

        preserved_settings = {
            "description": "Existing description",
            "webhook_callback_url": "https://example.com/webhook",
            "persist_browser_session": True,
            "browser_profile_id": "bp_existing",
            "run_with": "code",
            "cache_key": None,
            "adaptive_caching": True,
            "ai_fallback": False,
        }
        existing_workflow = {
            "title": "Status workflow",
            "proxy_location": "RESIDENTIAL",
            "workflow_definition": {
                "parameters": [],
                "blocks": [{"block_type": "code", "label": "report_status", "code": "return {'status': 'ready'}"}],
            },
            **preserved_settings,
        }
        exported = {
            "title": existing_workflow["title"],
            "proxy_location": existing_workflow["proxy_location"],
            "workflow_definition": existing_workflow["workflow_definition"],
        }
        monkeypatch.setattr(workflow_cmd, "prepare_cli_runtime", lambda **_: None)
        monkeypatch.setattr(
            workflow_cmd,
            "tool_workflow_get",
            AsyncMock(return_value={"ok": True, "data": existing_workflow}),
        )
        definition_file = tmp_path / "wf.json"

        cli_result = CliRunner().invoke(
            workflow_cmd.workflow_app,
            ["get", "--id", "wpid_123", "--definition-file", str(definition_file)],
        )

        assert cli_result.exit_code == 0, cli_result.output
        assert "Wrote workflow definition to" in cli_result.output
        assert json.loads(definition_file.read_text()) == exported

        monkeypatch.setattr(workflow_tools, "get_workflow_by_id", AsyncMock(return_value=existing_workflow))
        update_raw = AsyncMock(
            return_value={"workflow_permanent_id": "wpid_123", "title": "Status workflow", "version": 2}
        )
        monkeypatch.setattr(workflow_tools, "update_workflow_raw", update_raw)

        update_result = asyncio.run(
            workflow_cmd.tool_workflow_update(
                workflow_id="wpid_123",
                definition=definition_file.read_text(),
                format="json",
            )
        )

        assert update_result["ok"] is True
        update_raw.assert_awaited_once()
        sent_definition = update_raw.await_args.kwargs["json_definition"]
        assert {field: sent_definition[field] for field in preserved_settings} == preserved_settings

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

    def test_workflow_create_preserves_empty_definition_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        definition_file = tmp_path / "workflow.json"
        definition_file.write_text("")

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_workflow_create",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"workflow_permanent_id": "wpid_empty"},
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
            folder_id=None,
            json_output=True,
        )

        assert tool.await_args.kwargs["definition"] == ""
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True

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
            run_with=None,
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
            "run_with": None,
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

    def test_workflow_status_cli_preserves_full_detail_behavior(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_workflow_status",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"run_id": "wr_123", "recording_url": "https://example.com/recording"},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(workflow_cmd, "tool_workflow_status", tool)

        workflow_cmd.workflow_status(run_id="wr_123", json_output=True)

        assert tool.await_args.kwargs == {"run_id": "wr_123", "verbosity": "full"}
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["data"]["recording_url"] == "https://example.com/recording"

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

    def test_workflow_update_preserves_empty_definition_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        definition_file = tmp_path / "workflow.json"
        definition_file.write_text("")

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_workflow_update",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"workflow_permanent_id": "wpid_123"},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(workflow_cmd, "tool_workflow_update", tool)

        workflow_cmd.workflow_update(
            workflow_id="wpid_123",
            definition=f"@{definition_file}",
            definition_format="json",
            json_output=True,
        )

        assert tool.await_args.kwargs["definition"] == ""
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True


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

    def test_block_validate_preserves_empty_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import block as block_cmd

        block_file = tmp_path / "block.json"
        block_file.write_text("")

        tool = AsyncMock(
            return_value={
                "ok": True,
                "action": "skyvern_block_validate",
                "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
                "data": {"valid": False},
                "artifacts": [],
                "timing_ms": {},
                "warnings": [],
                "error": None,
            }
        )
        monkeypatch.setattr(block_cmd, "tool_block_validate", tool)

        block_cmd.block_validate(block_json=f"@{block_file}", json_output=True)

        assert tool.await_args.kwargs["block_json"] == ""
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True


class TestTasksCommands:
    def test_tasks_list_json_error_includes_action(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import tasks as tasks_cmd

        monkeypatch.setattr(tasks_cmd, "_get_client", lambda _api_key=None, **_: object())
        monkeypatch.setattr(
            tasks_cmd, "_list_workflow_tasks", lambda _client, _run_id: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        with pytest.raises(SystemExit, match="1"):
            tasks_cmd.list_tasks(
                ctx=SimpleNamespace(obj={}),
                workflow_run_id="wr_123",
                json_output=True,
            )

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert parsed["action"] == "tasks.list"


@pytest.mark.parametrize("module_name", ["skyvern.cli.credentials", "skyvern.cli.tasks"])
class TestCredentialClientBaseUrlGuard:
    def test_rejects_credential_when_base_url_uses_untouched_default(
        self, module_name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern import config

        monkeypatch.delenv("SKYVERN_API_KEY", raising=False)
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        cli_module = importlib.import_module(module_name)
        monkeypatch.setattr(cli_module, "prepare_cli_runtime", lambda **_: None)
        monkeypatch.setattr(config, "settings", config.Settings(_env_file=None))

        with pytest.raises(SystemExit, match="1"):
            cli_module._get_client("test-api-key")

        output = " ".join(capsys.readouterr().out.split())
        assert "Set SKYVERN_BASE_URL" in output
        # The remediation must not hand the user the production URL this guard exists to protect.
        assert "https://api.skyvern.com" not in output

    def test_rejection_uses_json_envelope_when_requested(
        self, module_name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern import config

        monkeypatch.delenv("SKYVERN_API_KEY", raising=False)
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        cli_module = importlib.import_module(module_name)
        monkeypatch.setattr(cli_module, "prepare_cli_runtime", lambda **_: None)
        monkeypatch.setattr(config, "settings", config.Settings(_env_file=None))

        with pytest.raises(SystemExit, match="1"):
            cli_module._get_client("test-api-key", action="credentials.list", json_mode=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert parsed["action"] == "credentials.list"
        assert "Set SKYVERN_BASE_URL" in parsed["error"]["message"]
        assert "https://api.skyvern.com" not in parsed["error"]["message"]

    @pytest.mark.parametrize("base_url", ["https://api.skyvern.com", "https://staging.skyvern.com"])
    def test_allows_credential_when_base_url_is_explicitly_configured(
        self, module_name: str, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern import config

        monkeypatch.delenv("SKYVERN_API_KEY", raising=False)
        monkeypatch.setenv("SKYVERN_BASE_URL", base_url)
        cli_module = importlib.import_module(module_name)
        client_constructor = MagicMock(return_value=object())
        monkeypatch.setattr(cli_module, "prepare_cli_runtime", lambda **_: None)
        monkeypatch.setattr(cli_module, "Skyvern", client_constructor)
        monkeypatch.setattr(config, "settings", config.Settings(_env_file=None))

        cli_module._get_client("test-api-key")

        client_constructor.assert_called_once_with(base_url=base_url, api_key="test-api-key")

    def test_allows_untouched_default_without_a_credential(
        self, module_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern import config

        monkeypatch.delenv("SKYVERN_API_KEY", raising=False)
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        cli_module = importlib.import_module(module_name)
        client_constructor = MagicMock(return_value=object())
        monkeypatch.setattr(cli_module, "prepare_cli_runtime", lambda **_: None)
        monkeypatch.setattr(cli_module, "Skyvern", client_constructor)
        monkeypatch.setattr(config, "settings", config.Settings(_env_file=None))

        cli_module._get_client()

        client_constructor.assert_called_once_with(base_url="https://api.skyvern.com", api_key="PLACEHOLDER")


class TestSharedFactoryBaseUrlGuard:
    def _client_module_with_fresh_settings(self, monkeypatch: pytest.MonkeyPatch) -> object:
        from skyvern import config
        from skyvern.cli.core import client as client_module

        # Stateless HTTP mode is process-global; pin it so tests hold under xdist
        # regardless of what an earlier test on the same worker left behind.
        monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)
        monkeypatch.setattr(client_module, "settings", config.Settings(_env_file=None))
        return client_module

    def test_cli_process_rejects_credential_on_untouched_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        client_module = self._client_module_with_fresh_settings(monkeypatch)
        monkeypatch.setattr(client_module, "is_cli_runtime", lambda: True)

        with pytest.raises(RuntimeError, match="Set SKYVERN_BASE_URL"):
            client_module._build_cloud_client("test-api-key")

    def test_non_cli_process_builds_client_on_untouched_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prod temporal workers run with SKYVERN_BASE_URL unset and reach this factory via
        copilot self-heal; an unconditional guard here is a production outage."""
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        client_module = self._client_module_with_fresh_settings(monkeypatch)
        monkeypatch.setattr(client_module, "is_cli_runtime", lambda: False)
        client_constructor = MagicMock(return_value=object())
        monkeypatch.setattr(client_module, "Skyvern", client_constructor)

        client_module._build_cloud_client("test-api-key")

        assert client_constructor.call_args.kwargs["base_url"] == "https://api.skyvern.com"

    @pytest.mark.parametrize("base_url", ["https://api.skyvern.com", "http://localhost:8000"])
    def test_cli_process_builds_client_when_base_url_is_explicit(
        self, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SKYVERN_BASE_URL", base_url)
        client_module = self._client_module_with_fresh_settings(monkeypatch)
        monkeypatch.setattr(client_module, "is_cli_runtime", lambda: True)
        client_constructor = MagicMock(return_value=object())
        monkeypatch.setattr(client_module, "Skyvern", client_constructor)

        client_module._build_cloud_client("test-api-key")

        assert client_constructor.call_args.kwargs["base_url"] == base_url

    def test_cli_process_builds_client_with_placeholder_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        client_module = self._client_module_with_fresh_settings(monkeypatch)
        monkeypatch.setattr(client_module, "is_cli_runtime", lambda: True)
        client_constructor = MagicMock(return_value=object())
        monkeypatch.setattr(client_module, "Skyvern", client_constructor)

        client_module._build_cloud_client("PLACEHOLDER")

        assert client_constructor.call_args.kwargs["base_url"] == "https://api.skyvern.com"

    def test_stateless_http_mode_targets_self_and_skips_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        client_module = self._client_module_with_fresh_settings(monkeypatch)
        monkeypatch.setattr(client_module, "is_cli_runtime", lambda: True)
        monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: True)
        client_constructor = MagicMock(return_value=object())
        monkeypatch.setattr(client_module, "Skyvern", client_constructor)

        client_module._build_cloud_client("test-api-key")

        assert client_constructor.call_args.kwargs["base_url"] == f"http://127.0.0.1:{client_module.settings.PORT}"


class TestBrowserGroupBaseUrlGuard:
    def test_browser_session_create_refuses_credential_on_untouched_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The browser group callback must mark the CLI runtime so the shared factory guard
        fires for nested subcommands; without it a real key silently reaches production."""
        from skyvern import _cli_bootstrap, config
        from skyvern.cli.commands import browser as browser_cmd
        from skyvern.cli.core import client as client_module

        monkeypatch.delenv("SKYVERN_BASE_URL", raising=False)
        monkeypatch.setattr(_cli_bootstrap, "_CLI_RUNTIME_PREPARED", False)
        monkeypatch.setattr("skyvern.utils.env_paths.load_backend_env_files", lambda intent: tmp_path / ".env")
        monkeypatch.setattr(_cli_bootstrap, "configure_cli_runtime_logging", lambda: None)
        monkeypatch.setattr("skyvern.cli.core.session_manager.is_stateless_http_mode", lambda: False)
        monkeypatch.setattr(client_module, "settings", config.Settings(_env_file=None))
        monkeypatch.setattr(client_module, "_resolve_api_key", lambda: "test-api-key")
        monkeypatch.setattr(client_module, "_global_skyvern_instance", None)
        monkeypatch.setattr(browser_cmd, "capture_cli_tool_call", lambda *_args, **_kwargs: None)

        token = client_module._skyvern_instance.set(None)
        try:
            result = CliRunner().invoke(browser_cmd.browser_app, ["session", "create", "--json"])
        finally:
            client_module._skyvern_instance.reset(token)

        assert result.exit_code == 1, result.output
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert "Set SKYVERN_BASE_URL" in parsed["error"]["message"]


class TestCliRuntimeFlag:
    def test_prepare_cli_runtime_marks_cli_process(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from skyvern import _cli_bootstrap

        monkeypatch.setattr(_cli_bootstrap, "_CLI_RUNTIME_PREPARED", False)
        monkeypatch.setattr("skyvern.utils.env_paths.load_backend_env_files", lambda intent: tmp_path / ".env")
        monkeypatch.setattr(_cli_bootstrap, "configure_cli_runtime_logging", lambda: None)

        assert not _cli_bootstrap.is_cli_runtime()
        _cli_bootstrap.prepare_cli_runtime(intent="cloud")
        assert _cli_bootstrap.is_cli_runtime()

    def test_cli_import_defers_settings_construction(self) -> None:
        """Both base-url guards read a settings singleton that must be constructed only after
        prepare_cli_runtime has loaded env files; an eager skyvern.config import breaks that."""
        code = "import sys; import skyvern.cli; raise SystemExit(1 if 'skyvern.config' in sys.modules else 0)"
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert completed.returncode == 0, completed.stderr


class TestCredentialsCommands:
    def test_add_credential_invalid_type_json_error_includes_action(self, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli import credentials as credentials_cmd

        ctx = SimpleNamespace(obj={})

        with pytest.raises(SystemExit, match="1"):
            credentials_cmd.add_credential(
                ctx=ctx,
                name="Bad",
                credential_type="not-real",
                username=None,
                password=None,
                totp=None,
                card_number=None,
                cvv=None,
                exp_month=None,
                exp_year=None,
                card_brand=None,
                holder_name=None,
                secret_value=None,
                secret_label=None,
                json_output=True,
            )

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert parsed["action"] == "credentials.add"


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
        from skyvern.cli.commands import _output as output_mod
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
        capture_mock = MagicMock()
        monkeypatch.setattr(output_mod, "capture_cli_tool_call", capture_mock)

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
        capture_mock.assert_called_once_with("skyvern_login", ok=False)

    def test_workflow_create_missing_definition_file_does_not_emit_cli_telemetry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from skyvern.cli import workflow as workflow_cmd

        capture_mock = MagicMock()
        monkeypatch.setattr("skyvern.cli.commands._output.capture_cli_tool_call", capture_mock)

        with pytest.raises(typer.BadParameter):
            workflow_cmd.workflow_create(
                definition="@/does/not/exist.json",
                definition_format="json",
                folder_id=None,
                json_output=False,
            )

        capture_mock.assert_not_called()


class TestParityErrorFormatting:
    def test_credential_emit_tool_result_handles_none_message_and_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands import _output as output_mod

        captured: dict[str, str | bool] = {}

        def _fake_output_error(
            message: str,
            *,
            hint: str = "",
            action: str = "",
            json_mode: bool = False,
            exit_code: int = 1,
        ) -> None:
            captured["message"] = message
            captured["hint"] = hint
            captured["json_mode"] = json_mode
            raise SystemExit(exit_code)

        monkeypatch.setattr(output_mod, "output_error", _fake_output_error)

        with pytest.raises(SystemExit, match="1"):
            output_mod.emit_tool_result(
                {"ok": False, "error": {"message": None, "hint": None}},
                json_output=False,
            )

        assert captured == {"message": "Unknown error", "hint": "", "json_mode": False}

    def test_block_emit_tool_result_handles_none_message_and_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands import _output as output_mod

        captured: dict[str, str | bool] = {}

        def _fake_output_error(
            message: str,
            *,
            hint: str = "",
            action: str = "",
            json_mode: bool = False,
            exit_code: int = 1,
        ) -> None:
            captured["message"] = message
            captured["hint"] = hint
            captured["json_mode"] = json_mode
            raise SystemExit(exit_code)

        monkeypatch.setattr(output_mod, "output_error", _fake_output_error)

        with pytest.raises(SystemExit, match="1"):
            output_mod.emit_tool_result(
                {"ok": False, "error": {"message": None, "hint": None}},
                json_output=False,
            )

        assert captured == {"message": "Unknown error", "hint": "", "json_mode": False}

    def test_browser_emit_tool_result_handles_none_message_and_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.cli.commands import _output as output_mod
        from skyvern.cli.commands import browser as browser_cmd

        captured: dict[str, str | bool] = {}

        def _fake_output_error(
            message: str,
            *,
            hint: str = "",
            action: str = "",
            json_mode: bool = False,
            exit_code: int = 1,
        ) -> None:
            captured["message"] = message
            captured["hint"] = hint
            captured["json_mode"] = json_mode
            raise SystemExit(exit_code)

        monkeypatch.setattr(output_mod, "output_error", _fake_output_error)

        with pytest.raises(SystemExit, match="1"):
            browser_cmd._emit_tool_result(
                {"ok": False, "error": {"message": None, "hint": None}},
                json_output=False,
                action="login",
            )

        assert captured == {"message": "Unknown error", "hint": "", "json_mode": False}


class TestStopCommands:
    def test_stop_all_json_reports_failure_when_nothing_running(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import stop_commands as stop_cmd

        monkeypatch.setattr(stop_cmd, "get_pids_on_port", lambda _port: [])
        monkeypatch.setattr(stop_cmd, "kill_pids", lambda pids, service_name, quiet=False: False)

        with pytest.raises(SystemExit, match="1"):
            stop_cmd.stop_all(json_output=True)

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is False
        assert parsed["action"] == "stop.all"
        assert parsed["error"]["message"] == "No Skyvern services found running on ports 8000, 8080, or 9090."


class TestCLIStaleFrameSurface:
    @staticmethod
    def _patch_page(monkeypatch: pytest.MonkeyPatch, page: object) -> None:
        browser = SimpleNamespace(get_working_page=AsyncMock(return_value=page))
        monkeypatch.setattr(cli_browser, "_resolve_connection", MagicMock())
        monkeypatch.setattr(cli_browser, "_connect_browser", AsyncMock(return_value=browser))
        monkeypatch.setattr(cli_browser, "_apply_cli_frame_state", AsyncMock())
        monkeypatch.setattr(cli_browser, "capture_cli_tool_call", MagicMock())

    @pytest.mark.parametrize(
        "invoke",
        [
            lambda: cli_browser.evaluate(expression="1 + 1", session=None, cdp=None, json_output=True),
            lambda: cli_browser.scroll(
                direction="down", session=None, cdp=None, amount=None, intent=None, selector=None, json_output=True
            ),
            lambda: cli_browser.press_key(
                key="Enter", session=None, cdp=None, intent=None, selector=None, json_output=True
            ),
            lambda: cli_browser.select(
                value="Ground",
                intent=None,
                selector="#ship",
                session=None,
                cdp=None,
                timeout=30000,
                by_label=True,
                json_output=True,
            ),
        ],
        ids=["evaluate", "scroll", "press_key", "select_by_label"],
    )
    def test_page_space_commands_refuse_an_unowned_frame_selection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        invoke: Callable[[], None],
    ) -> None:
        raw = MagicMock()
        raw.evaluate = AsyncMock(return_value=2)
        raw.keyboard = SimpleNamespace(press=AsyncMock())
        self._patch_page(monkeypatch, StaleScopePage(raw))

        with pytest.raises(SystemExit):
            invoke()

        error = json.loads(capsys.readouterr().out)["error"]
        assert "Stale frame selection" in error["message"]
        assert error["hint"] == STALE_FRAME_HINT
        raw.locator.assert_not_called()

    def test_select_by_label_resolves_in_page_space_not_the_selected_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = MagicMock()
        raw.locator = MagicMock(return_value=SimpleNamespace(select_option=AsyncMock()))
        frame = MagicMock()
        self._patch_page(monkeypatch, SimpleNamespace(page=raw, locator_scope=frame))

        cli_browser.select(
            value="Ground",
            intent=None,
            selector="#ship",
            session=None,
            cdp=None,
            timeout=30000,
            by_label=True,
            json_output=True,
        )

        raw.locator.assert_called_once_with("#ship")
        frame.locator.assert_not_called()

    def test_wait_intent_envelope_keeps_the_stale_selection_hint(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        page = SimpleNamespace(
            wait_for_timeout=AsyncMock(),
            validate=AsyncMock(side_effect=StaleFrameSelectionError("pay", "https://pay.example.com/")),
        )
        browser = SimpleNamespace(get_working_page=AsyncMock(return_value=page))
        monkeypatch.setattr(cli_browser, "_resolve_connection", MagicMock())
        monkeypatch.setattr(cli_browser, "_connect_browser", AsyncMock(return_value=browser))
        monkeypatch.setattr(cli_browser, "_apply_cli_frame_state", AsyncMock())
        monkeypatch.setattr(cli_browser, "capture_cli_tool_call", MagicMock())

        with pytest.raises(SystemExit):
            cli_browser.wait(
                session=None,
                cdp=None,
                time_ms=None,
                intent="the card form is ready",
                selector=None,
                state="visible",
                timeout=0,
                poll_interval=0,
                json_output=True,
            )

        error = json.loads(capsys.readouterr().out)["error"]
        assert "Stale frame selection" in error["message"]
        assert error["hint"] == STALE_FRAME_HINT
