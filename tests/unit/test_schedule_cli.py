from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from skyvern.cli.schedule_command import schedule_app


def _patch_tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    list_all_result: dict[str, Any] | None = None,
    list_for_workflow_result: dict[str, Any] | None = None,
    create_result: dict[str, Any] | None = None,
    update_result: dict[str, Any] | None = None,
    delete_result: dict[str, Any] | None = None,
    enable_result: dict[str, Any] | None = None,
    disable_result: dict[str, Any] | None = None,
) -> dict[str, AsyncMock]:
    """Patch the MCP tools imported into schedule_command with AsyncMocks."""
    mocks: dict[str, AsyncMock] = {}
    cases = [
        ("tool_schedule_list", list_all_result),
        ("tool_schedule_list_for_workflow", list_for_workflow_result),
        ("tool_schedule_create", create_result),
        ("tool_schedule_update", update_result),
        ("tool_schedule_delete", delete_result),
        ("tool_schedule_enable", enable_result),
        ("tool_schedule_disable", disable_result),
    ]
    for name, ret in cases:
        m = AsyncMock(return_value=ret or {"ok": True, "data": {"schedules": []}})
        mocks[name] = m
        monkeypatch.setattr(f"skyvern.cli.schedule_command.{name}", m)
    return mocks


# -- create --


class TestCliCreate:
    def test_disabled_flag_maps_to_enabled_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(
            monkeypatch,
            create_result={"ok": True, "data": {"schedule": {"workflow_schedule_id": "wfs_test_1"}}},
        )
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "create",
                "--workflow-id",
                "wpid_test_1",
                "--cron",
                "0 9 * * *",
                "--timezone",
                "UTC",
                "--disabled",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = mocks["tool_schedule_create"].call_args.kwargs
        assert kwargs["enabled"] is False

    def test_parameters_invalid_json_friendly_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "create",
                "--workflow-id",
                "wpid_test_1",
                "--cron",
                "0 9 * * *",
                "--timezone",
                "UTC",
                "--parameters",
                "not valid json {",
            ],
        )
        assert result.exit_code != 0
        # MCP tool must not be invoked when parsing fails.
        mocks["tool_schedule_create"].assert_not_called()

    def test_parameters_must_be_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "create",
                "--workflow-id",
                "wpid_test_1",
                "--cron",
                "0 9 * * *",
                "--timezone",
                "UTC",
                "--parameters",
                '["a", "b"]',
            ],
        )
        assert result.exit_code != 0
        mocks["tool_schedule_create"].assert_not_called()


# -- update --


class TestCliUpdate:
    def test_mutex_flags_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "update",
                "--workflow-id",
                "wpid_test_1",
                "--id",
                "wfs_test_1",
                "--name",
                "x",
                "--clear-name",
            ],
        )
        assert result.exit_code != 0
        mocks["tool_schedule_update"].assert_not_called()

    def test_no_flags_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No fields, no clear flags, no --exact → MCP tool returns INVALID_INPUT
        # and the CLI exits non-zero.
        mocks = _patch_tools(
            monkeypatch,
            update_result={
                "ok": False,
                "error": {"code": "INVALID_INPUT", "message": "Empty update — no fields supplied.", "hint": "..."},
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "update",
                "--workflow-id",
                "wpid_test_1",
                "--id",
                "wfs_test_1",
                "--json",
            ],
        )
        assert result.exit_code != 0
        # MCP tool IS called (CLI doesn't pre-check); the tool itself rejects.
        mocks["tool_schedule_update"].assert_called_once()

    def test_exact_missing_field_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # In --exact mode without --enabled, the MCP tool must reject.
        mocks = _patch_tools(
            monkeypatch,
            update_result={
                "ok": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "exact=True requires explicit values for: enabled",
                    "hint": "...",
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "update",
                "--workflow-id",
                "wpid_test_1",
                "--id",
                "wfs_test_1",
                "--cron",
                "0 9 * * *",
                "--timezone",
                "UTC",
                "--exact",
                "--json",
            ],
        )
        assert result.exit_code != 0
        # The CLI forwards exact=True; the tool itself does the completeness check.
        kwargs = mocks["tool_schedule_update"].call_args.kwargs
        assert kwargs["exact"] is True
        assert kwargs["enabled"] is None


# -- delete --


class TestCliDelete:
    def test_without_yes_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "delete",
                "--workflow-id",
                "wpid_test_1",
                "--id",
                "wfs_test_1",
            ],
        )
        assert result.exit_code != 0
        mocks["tool_schedule_delete"].assert_not_called()

    def test_with_yes_forwards_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(
            monkeypatch,
            delete_result={"ok": True, "data": {"deleted": True}},
        )
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            [
                "delete",
                "--workflow-id",
                "wpid_test_1",
                "--id",
                "wfs_test_1",
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = mocks["tool_schedule_delete"].call_args.kwargs
        assert kwargs["force"] is True


# -- list --


class TestCliList:
    def test_workflow_id_routes_to_per_workflow_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(
            monkeypatch,
            list_for_workflow_result={"ok": True, "data": {"schedules": []}},
        )
        runner = CliRunner()
        result = runner.invoke(
            schedule_app,
            ["list", "--workflow-id", "wpid_test_1"],
        )
        assert result.exit_code == 0, result.output
        mocks["tool_schedule_list_for_workflow"].assert_called_once()
        mocks["tool_schedule_list"].assert_not_called()

    def test_no_workflow_id_routes_to_org_wide(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mocks = _patch_tools(
            monkeypatch,
            list_all_result={
                "ok": True,
                "data": {"schedules": [], "total_count": 0, "page": 1, "page_size": 10},
            },
        )
        runner = CliRunner()
        result = runner.invoke(schedule_app, ["list"])
        assert result.exit_code == 0, result.output
        mocks["tool_schedule_list"].assert_called_once()
        mocks["tool_schedule_list_for_workflow"].assert_not_called()


# -- capabilities surface --


class TestCliCapabilities:
    def test_capabilities_schedule_lists_subcommands(self) -> None:
        from skyvern.cli.commands import cli_app

        runner = CliRunner()
        result = runner.invoke(cli_app, ["capabilities", "schedule"])
        assert result.exit_code == 0, result.output
        # Output is JSON by default (--json/--no-json default True per the capabilities cmd).
        for sub in ("list", "get", "create", "update", "enable", "disable", "delete"):
            assert sub in result.output, f"missing subcommand {sub} in: {result.output}"
