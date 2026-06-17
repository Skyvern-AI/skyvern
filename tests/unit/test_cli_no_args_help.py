"""Regression guard: typer groups must exit 0 (printing help) when invoked with
no subcommand, across click <8.2 and >=8.2.

Click 8.2 changed ``no_args_is_help`` to raise ``NoArgsIsHelpError`` (exit code
2) instead of echoing help and exiting 0. ``SkyvernTyperGroup`` restores the
exit-0 behavior so ``skyvern <group>`` keeps working on click 8.2+.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from skyvern.cli.lazy import SkyvernTyperGroup

runner = CliRunner()


def _build_group() -> typer.Typer:
    app = typer.Typer(cls=SkyvernTyperGroup, help="Top help.", no_args_is_help=True)
    sub = typer.Typer(cls=SkyvernTyperGroup, help="Sub help.", no_args_is_help=True)
    app.add_typer(sub, name="sub")

    @app.command("foo")
    def foo() -> None:
        typer.echo("foo ran")

    @sub.command("bar")
    def bar() -> None:
        typer.echo("bar ran")

    return app


class TestSkyvernTyperGroupNoArgs:
    def test_group_with_no_subcommand_exits_zero(self) -> None:
        result = runner.invoke(_build_group(), [])
        assert result.exit_code == 0
        assert "Top help." in result.output

    def test_nested_group_with_no_subcommand_exits_zero(self) -> None:
        result = runner.invoke(_build_group(), ["sub"])
        assert result.exit_code == 0
        assert "Sub help." in result.output

    def test_valid_subcommand_runs(self) -> None:
        result = runner.invoke(_build_group(), ["foo"])
        assert result.exit_code == 0
        assert "foo ran" in result.output

    def test_unknown_subcommand_is_usage_error(self) -> None:
        result = runner.invoke(_build_group(), ["does-not-exist"])
        assert result.exit_code == 2


class TestRealCliGroupsNoArgs:
    def test_top_level_cli_exits_zero(self) -> None:
        from skyvern.cli.commands import cli_app

        result = runner.invoke(cli_app, [])
        assert result.exit_code == 0
        assert "Skyvern CLI" in result.output

    def test_browser_group_exits_zero(self) -> None:
        from skyvern.cli.commands.browser import browser_app

        result = runner.invoke(browser_app, [])
        assert result.exit_code == 0
        assert "Browser automation commands" in result.output

    def test_browser_session_group_exits_zero(self) -> None:
        from skyvern.cli.commands.browser import browser_app

        result = runner.invoke(browser_app, ["session"])
        assert result.exit_code == 0
        assert "Manage browser sessions" in result.output
