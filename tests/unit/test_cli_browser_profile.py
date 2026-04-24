from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner


def _mcp_result(action: str, data: dict) -> dict:
    return {
        "ok": True,
        "action": action,
        "browser_context": {"mode": "none", "session_id": None, "cdp_url": None},
        "data": data,
        "artifacts": [],
        "timing_ms": {},
        "warnings": [],
        "error": None,
    }


class TestBrowserProfileCli:
    def test_list_delegates_to_mcp_tool(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli import browser_profile as browser_profile_cmd

        tool = AsyncMock(return_value=_mcp_result("skyvern_browser_profile_list", {"browser_profiles": [], "count": 0}))
        monkeypatch.setattr(browser_profile_cmd, "tool_browser_profile_list", tool)

        browser_profile_cmd.browser_profile_list(include_deleted=True, json_output=True)

        tool.assert_awaited_once_with(include_deleted=True)
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ok"] is True
        assert parsed["action"] == "skyvern_browser_profile_list"

    def test_get_delegates_to_mcp_tool(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli import browser_profile as browser_profile_cmd

        tool = AsyncMock(return_value=_mcp_result("skyvern_browser_profile_get", {"browser_profile_id": "bp_saved"}))
        monkeypatch.setattr(browser_profile_cmd, "tool_browser_profile_get", tool)

        browser_profile_cmd.browser_profile_get(browser_profile_id="bp_saved", json_output=True)

        tool.assert_awaited_once_with(browser_profile_id="bp_saved")
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["data"]["browser_profile_id"] == "bp_saved"

    def test_create_delegates_alias_sources_to_mcp_tool(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from skyvern.cli import browser_profile as browser_profile_cmd

        tool = AsyncMock(return_value=_mcp_result("skyvern_browser_profile_create", {"browser_profile_id": "bp_saved"}))
        monkeypatch.setattr(browser_profile_cmd, "tool_browser_profile_create", tool)

        browser_profile_cmd.browser_profile_create(
            name="site-signed-in",
            workflow_run_id="wr_123",
            browser_session_id=None,
            description="logged in",
            json_output=True,
        )

        tool.assert_awaited_once_with(
            name="site-signed-in",
            browser_session_id=None,
            workflow_run_id="wr_123",
            description="logged in",
        )
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["data"]["browser_profile_id"] == "bp_saved"

    def test_delete_delegates_to_mcp_tool(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        from skyvern.cli import browser_profile as browser_profile_cmd

        tool = AsyncMock(return_value=_mcp_result("skyvern_browser_profile_delete", {"browser_profile_id": "bp_saved"}))
        monkeypatch.setattr(browser_profile_cmd, "tool_browser_profile_delete", tool)

        browser_profile_cmd.browser_profile_delete(browser_profile_id="bp_saved", json_output=True)

        tool.assert_awaited_once_with(browser_profile_id="bp_saved")
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["data"]["browser_profile_id"] == "bp_saved"

    def test_browser_profile_help_describes_cloud_saved_login_lifecycle(self) -> None:
        # Inspect registered Click options directly rather than asserting on rendered
        # Rich help output, which wraps unpredictably on narrow CI terminals.
        from typer.main import get_command

        from skyvern.cli.commands import cli_app

        click_app = get_command(cli_app)
        create = click_app.get_command(None, "browser-profile").get_command(None, "create")  # type: ignore[union-attr]
        opts = {flag for p in create.params for flag in getattr(p, "opts", [])}

        assert opts >= {"--name", "--workflow-run-id", "--from-run", "--browser-session-id"}
        # Help wording ("cloud" + "saved-login") lives in the command docstring
        # that Click surfaces as the command's short help. This avoids relying on
        # rendered Rich panel output, which wraps unpredictably across terminals.
        help_source = (create.help or create.short_help or "").lower()
        assert "cloud" in help_source
        assert "saved login" in help_source or "saved-login" in help_source

    def test_browser_profile_save_alias_is_not_registered(self) -> None:
        from skyvern.cli.commands import cli_app

        result = CliRunner().invoke(cli_app, ["browser-profile", "save", "--help"])

        assert result.exit_code != 0
        assert "save" in result.output

    def test_workflow_and_session_help_expose_browser_profile_id(self) -> None:
        from typer.main import get_command

        from skyvern.cli.commands import cli_app

        click_app = get_command(cli_app)
        workflow_run = click_app.get_command(None, "workflow").get_command(None, "run")  # type: ignore[union-attr]
        session_create = (
            click_app.get_command(None, "browser").get_command(None, "session").get_command(None, "create")  # type: ignore[union-attr]
        )

        workflow_opts = {flag for p in workflow_run.params for flag in getattr(p, "opts", [])}
        session_opts = {flag for p in session_create.params for flag in getattr(p, "opts", [])}

        assert "--browser-profile-id" in workflow_opts
        assert "--browser-profile-id" in session_opts
