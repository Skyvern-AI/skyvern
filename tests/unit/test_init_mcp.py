from __future__ import annotations

from typer.testing import CliRunner

from skyvern.cli import mcp
from skyvern.cli.commands import cli_app


def test_setup_mcp_local_claude_code_uses_local_stdio(monkeypatch) -> None:
    answers = iter([True, False, False, False])
    calls: list[dict] = []

    monkeypatch.setattr("skyvern.cli.mcp.Confirm.ask", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr("skyvern.cli.mcp.setup_claude", lambda **kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("skyvern.cli.mcp.setup_cursor", lambda **kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("skyvern.cli.mcp.setup_windsurf", lambda **kwargs: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr("skyvern.cli.mcp.setup_claude_code", lambda **kwargs: calls.append(kwargs))

    mcp.setup_mcp(local=True)

    assert calls == [
        {
            "api_key": None,
            "dry_run": False,
            "yes": True,
            "local": True,
            "use_python_path": True,
            "url": None,
            "project": False,
            "global_config": False,
            "skip_skills": False,
        }
    ]


def test_init_callback_passes_plain_database_string(monkeypatch) -> None:
    calls: list[tuple[bool, str]] = []

    monkeypatch.setattr(
        "skyvern.cli.commands.init_env",
        lambda no_postgres=False, database_string="": calls.append((no_postgres, database_string)),
    )

    result = CliRunner().invoke(cli_app, ["init"])

    assert result.exit_code == 0
    assert calls == [(False, "")]
