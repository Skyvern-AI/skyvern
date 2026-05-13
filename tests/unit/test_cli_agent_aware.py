"""Tests for agent-aware CLI features: JSON envelope, TTY detection, capabilities."""

from __future__ import annotations

import io
import json
import os
from unittest.mock import patch

import pytest

from skyvern.cli.commands._output import (
    ENVELOPE_SCHEMA_VERSION,
    emit_tool_result,
    output,
    output_error,
    resolve_inline_or_file,
    run_tool,
)
from skyvern.cli.commands._tty import is_interactive, require_interactive_or_flag

# ---------------------------------------------------------------------------
# JSON envelope tests
# ---------------------------------------------------------------------------


class TestOutputEnvelope:
    def test_json_envelope_has_schema_version(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            output({"key": "value"}, action="test", json_mode=True)
        envelope = json.loads(buf.getvalue())
        assert envelope["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert envelope["ok"] is True
        assert envelope["action"] == "test"
        assert envelope["data"] == {"key": "value"}
        assert envelope["error"] is None

    def test_json_envelope_has_mcp_fields(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            output({"x": 1}, action="t", json_mode=True)
        envelope = json.loads(buf.getvalue())
        assert "warnings" in envelope
        assert envelope["warnings"] == []
        assert "browser_context" in envelope
        assert envelope["browser_context"] is None
        assert "artifacts" in envelope
        assert envelope["artifacts"] is None
        assert "timing_ms" in envelope
        assert envelope["timing_ms"] is None

    def test_json_error_envelope_has_schema_version(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf), pytest.raises(SystemExit) as exc_info:
            output_error("bad thing", hint="try again", json_mode=True)
        assert exc_info.value.code == 1
        envelope = json.loads(buf.getvalue())
        assert envelope["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert envelope["ok"] is False
        assert envelope["error"]["message"] == "bad thing"
        assert envelope["error"]["hint"] == "try again"

    def test_json_error_envelope_preserves_action(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf), pytest.raises(SystemExit) as exc_info:
            output_error("bad thing", hint="try again", action="tasks.list", json_mode=True)
        assert exc_info.value.code == 1
        envelope = json.loads(buf.getvalue())
        assert envelope["action"] == "tasks.list"

    def test_human_output_unchanged(self, capsys):
        output({"name": "test", "value": 42}, json_mode=False)
        captured = capsys.readouterr()
        assert "name" in captured.out
        assert "42" in captured.out


# ---------------------------------------------------------------------------
# emit_tool_result tests
# ---------------------------------------------------------------------------


class TestEmitToolResult:
    def test_preserves_mcp_result_shape(self):
        mcp_result = {
            "ok": True,
            "action": "credential_list",
            "data": [{"id": "cred_1"}],
            "error": None,
            "browser_context": {"session_id": "pbs_123"},
            "artifacts": [{"type": "screenshot"}],
            "timing_ms": 150,
        }
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            emit_tool_result(mcp_result.copy(), json_output=True)
        result = json.loads(buf.getvalue())
        assert result["browser_context"] == {"session_id": "pbs_123"}
        assert result["artifacts"] == [{"type": "screenshot"}]
        assert result["timing_ms"] == 150
        assert result["schema_version"] == ENVELOPE_SCHEMA_VERSION

    def test_adds_defaults_to_minimal_result(self):
        minimal = {"ok": True, "action": "test", "data": {}, "error": None}
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            emit_tool_result(minimal.copy(), json_output=True)
        result = json.loads(buf.getvalue())
        assert result["schema_version"] == ENVELOPE_SCHEMA_VERSION
        assert result["warnings"] == []

    def test_exits_on_not_ok(self):
        error_result = {"ok": False, "action": "fail", "data": None, "error": {"message": "oops"}}
        buf = io.StringIO()
        with patch("sys.stdout", buf), pytest.raises(SystemExit) as exc_info:
            emit_tool_result(error_result, json_output=True)
        assert exc_info.value.code == 1

    def test_captures_cli_telemetry_when_tool_name_provided(self):
        result = {"ok": True, "action": "test", "data": {}, "error": None}
        buf = io.StringIO()
        with (
            patch("sys.stdout", buf),
            patch("skyvern.cli.commands._output.capture_cli_tool_call") as capture_mock,
        ):
            emit_tool_result(result, json_output=True, telemetry_tool_name="skyvern_test")

        capture_mock.assert_called_once_with("skyvern_test", ok=True)


# ---------------------------------------------------------------------------
# run_tool tests
# ---------------------------------------------------------------------------


class TestRunTool:
    def test_catches_exceptions(self):
        async def failing():
            raise RuntimeError("network error")

        buf = io.StringIO()
        with patch("sys.stdout", buf), pytest.raises(SystemExit):
            run_tool(failing, json_output=True, hint_on_exception="check connection")
        envelope = json.loads(buf.getvalue())
        assert envelope["ok"] is False
        assert "network error" in envelope["error"]["message"]

    def test_catches_exceptions_with_action(self):
        async def failing():
            raise RuntimeError("network error")

        buf = io.StringIO()
        with (
            patch("sys.stdout", buf),
            patch("skyvern.cli.commands._output.capture_cli_tool_call") as capture_mock,
            pytest.raises(SystemExit),
        ):
            run_tool(
                failing,
                json_output=True,
                hint_on_exception="check connection",
                action="workflow.create",
                telemetry_tool_name="skyvern_workflow_create",
            )
        envelope = json.loads(buf.getvalue())
        assert envelope["ok"] is False
        assert envelope["action"] == "workflow.create"
        capture_mock.assert_called_once()
        assert capture_mock.call_args.args[0] == "skyvern_workflow_create"
        assert capture_mock.call_args.kwargs["ok"] is False

    def test_passes_bad_parameter_through(self):
        import typer

        async def bad_param():
            raise typer.BadParameter("invalid value")

        with (
            patch("skyvern.cli.commands._output.capture_cli_tool_call") as capture_mock,
            pytest.raises(typer.BadParameter),
        ):
            run_tool(
                bad_param,
                json_output=False,
                hint_on_exception="",
                telemetry_tool_name="skyvern_workflow_create",
            )
        capture_mock.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_inline_or_file tests
# ---------------------------------------------------------------------------


class TestResolveInlineOrFile:
    def test_returns_none_for_none(self):
        assert resolve_inline_or_file(None, param_name="test") is None

    def test_returns_literal_string(self):
        assert resolve_inline_or_file("hello", param_name="test") == "hello"

    def test_reads_file(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("file content")
        assert resolve_inline_or_file(f"@{f}", param_name="test") == "file content"

    def test_raises_on_missing_file(self):
        import typer

        with pytest.raises(typer.BadParameter):
            resolve_inline_or_file("@/nonexistent/file.txt", param_name="test")

    def test_raises_on_empty_path(self):
        import typer

        with pytest.raises(typer.BadParameter):
            resolve_inline_or_file("@", param_name="test")


# ---------------------------------------------------------------------------
# TTY detection tests
# ---------------------------------------------------------------------------


class TestIsInteractive:
    def test_returns_false_when_ci_true(self):
        with patch.dict(os.environ, {"CI": "true"}, clear=False):
            assert is_interactive() is False

    def test_returns_false_when_ci_one(self):
        with patch.dict(os.environ, {"CI": "1"}, clear=False):
            assert is_interactive() is False

    def test_ci_false_does_not_force_non_interactive(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "SKYVERN_NON_INTERACTIVE")}
        env["CI"] = "false"
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_interactive() is True

    def test_returns_false_when_non_interactive_set(self):
        with patch.dict(os.environ, {"SKYVERN_NON_INTERACTIVE": "1"}, clear=False):
            assert is_interactive() is False

    def test_non_interactive_zero_does_not_force(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "SKYVERN_NON_INTERACTIVE")}
        env["SKYVERN_NON_INTERACTIVE"] = "0"
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_interactive() is True

    def test_returns_false_when_stdin_not_tty(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "SKYVERN_NON_INTERACTIVE")}
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            assert is_interactive() is False

    def test_returns_true_when_tty(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "SKYVERN_NON_INTERACTIVE")}
        with patch.dict(os.environ, env, clear=True), patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            assert is_interactive() is True


class TestRequireInteractiveOrFlag:
    def test_returns_flag_value_when_provided(self):
        assert require_interactive_or_flag("mypass", flag_name="password", message="Need password.") == "mypass"

    def test_returns_none_when_interactive(self):
        with patch("skyvern.cli.commands._tty.is_interactive", return_value=True):
            result = require_interactive_or_flag(None, flag_name="password", message="Need password.")
            assert result is None

    def test_exits_when_non_interactive_no_flag(self):
        with patch("skyvern.cli.commands._tty.is_interactive", return_value=False), pytest.raises(SystemExit):
            require_interactive_or_flag(None, flag_name="password", message="Need password.")

    def test_json_mode_produces_json_error(self):
        buf = io.StringIO()
        with (
            patch("skyvern.cli.commands._tty.is_interactive", return_value=False),
            patch("sys.stdout", buf),
            pytest.raises(SystemExit),
        ):
            require_interactive_or_flag(None, flag_name="password", message="Need password.", json_mode=True)
        envelope = json.loads(buf.getvalue())
        assert envelope["ok"] is False
        assert "SKYVERN_CRED_PASSWORD" in envelope["error"]["hint"]

    def test_json_mode_uses_custom_env_var_prefix(self):
        buf = io.StringIO()
        with (
            patch("skyvern.cli.commands._tty.is_interactive", return_value=False),
            patch("sys.stdout", buf),
            pytest.raises(SystemExit),
        ):
            require_interactive_or_flag(
                None,
                flag_name="password",
                message="Need password.",
                json_mode=True,
                env_var_prefix="SKYVERN_API_",
            )
        envelope = json.loads(buf.getvalue())
        assert envelope["ok"] is False
        assert "SKYVERN_API_PASSWORD" in envelope["error"]["hint"]


# ---------------------------------------------------------------------------
# Capabilities command tests
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_depth_0_returns_names_only(self):
        import typer

        from skyvern.cli.commands import _walk_command_tree, cli_app

        click_app = typer.main.get_command(cli_app)
        tree = _walk_command_tree(click_app, max_depth=0)
        assert tree["name"] == "skyvern"
        assert "subcommand_names" in tree
        assert "workflow" in tree["subcommand_names"]
        assert "subcommands" not in tree  # not expanded at depth 0

    def test_depth_1_returns_subcommands(self):
        import typer

        from skyvern.cli.commands import _walk_command_tree, cli_app

        click_app = typer.main.get_command(cli_app)
        tree = _walk_command_tree(click_app, max_depth=1)
        assert "subcommands" in tree
        sub_names = [s["name"] for s in tree["subcommands"]]
        assert "workflow" in sub_names
        assert "credential" in sub_names
        assert "capabilities" in sub_names

    def test_hidden_commands_excluded(self):
        import typer

        from skyvern.cli.commands import _walk_command_tree, cli_app

        click_app = typer.main.get_command(cli_app)
        tree = _walk_command_tree(click_app, max_depth=1)
        sub_names = [s["name"] for s in tree["subcommands"]]
        assert "signup" not in sub_names

    def test_deep_tree_has_options(self):
        import typer

        from skyvern.cli.commands import _walk_command_tree, cli_app

        click_app = typer.main.get_command(cli_app)
        tree = _walk_command_tree(click_app, max_depth=3)
        workflow = next(s for s in tree["subcommands"] if s["name"] == "workflow")
        assert "subcommands" in workflow
        list_cmd = next(s for s in workflow["subcommands"] if s["name"].endswith("list"))
        assert "options" in list_cmd
        option_names = [o["name"] for o in list_cmd["options"]]
        assert "json_output" in option_names or "json" in option_names

    def test_group_command_includes_group_level_options(self):
        import typer

        from skyvern.cli.commands import _walk_command_tree, cli_app

        click_app = typer.main.get_command(cli_app)
        workflow = next(s for s in _walk_command_tree(click_app, max_depth=2)["subcommands"] if s["name"] == "workflow")
        assert "options" in workflow
        option_names = [o["name"] for o in workflow["options"]]
        assert "api_key" in option_names

    def test_subcommand_filter(self):
        import typer

        from skyvern.cli.commands import _walk_command_tree, cli_app

        click_app = typer.main.get_command(cli_app)
        import click

        ctx = click.Context(click_app, info_name="skyvern")
        workflow_cmd = click_app.get_command(ctx, "workflow")
        tree = _walk_command_tree(workflow_cmd, prefix="workflow", max_depth=2)
        assert tree["name"] == "workflow"
        assert "subcommands" in tree

    def test_cli_runner_json_output(self):
        from typer.testing import CliRunner

        from skyvern.cli.commands import cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["capabilities", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert parsed["ok"] is True
        assert parsed["schema_version"] == "1.0"
        assert "subcommands" in parsed["data"]

    def test_cli_runner_no_json_output(self):
        from typer.testing import CliRunner

        from skyvern.cli.commands import cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["capabilities", "--no-json"])
        assert result.exit_code == 0
        assert "workflow" in result.stdout

    def test_cli_runner_depth_parsing(self):
        from typer.testing import CliRunner

        from skyvern.cli.commands import cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["capabilities", "--depth", "0", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert "subcommand_names" in parsed["data"]
        assert "subcommands" not in parsed["data"]


# ---------------------------------------------------------------------------
# Regression: existing tests should still work after DRY consolidation
# ---------------------------------------------------------------------------


class TestDryConsolidationRegression:
    def test_credential_imports_shared_run_tool(self):
        from skyvern.cli.credential import run_tool as imported_run_tool

        assert imported_run_tool is run_tool

    def test_workflow_imports_shared_functions(self):
        from skyvern.cli.workflow import resolve_inline_or_file as wf_resolve
        from skyvern.cli.workflow import run_tool as wf_run_tool

        assert wf_run_tool is run_tool
        assert wf_resolve is resolve_inline_or_file

    def test_block_imports_shared_functions(self):
        from skyvern.cli.block import resolve_inline_or_file as blk_resolve
        from skyvern.cli.block import run_tool as blk_run_tool

        assert blk_run_tool is run_tool
        assert blk_resolve is resolve_inline_or_file
