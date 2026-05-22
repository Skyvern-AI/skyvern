from __future__ import annotations

import subprocess

from typer.testing import CliRunner

from skyvern.cli import quickstart as quickstart_module
from skyvern.cli.quickstart import quickstart_app


def test_install_server_extra_assume_yes_skips_prompt(monkeypatch) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(quickstart_module, "_server_extra_install_target", lambda: "skyvern[server]==1.2.3")
    monkeypatch.setattr(
        quickstart_module.Confirm,
        "ask",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )
    monkeypatch.setattr(
        quickstart_module.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, returncode=0),
    )

    assert quickstart_module._install_server_extra_for_quickstart(assume_yes=True) is True
    assert commands == [
        [
            quickstart_module.sys.executable,
            "-m",
            "pip",
            "install",
            "--retries",
            "5",
            "--timeout",
            "60",
            "skyvern[server]==1.2.3",
        ]
    ]


def test_quickstart_non_interactive_server_options_skip_prompts(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_init_env(**kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr("skyvern.cli.quickstart._has_server_quickstart_extra", lambda: True)
    monkeypatch.setattr("skyvern.cli.quickstart.check_docker_compose_file", lambda: False)
    monkeypatch.setattr("skyvern.cli.init_command.init_env", fake_init_env)
    monkeypatch.setattr(
        "skyvern.cli.quickstart.Confirm.ask",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )

    result = CliRunner().invoke(
        quickstart_app,
        [
            "--non-interactive",
            "--skip-browser-install",
            "--database-string",
            "postgresql+psycopg://skyvern:skyvern@postgres:5432/skyvern",
        ],
    )

    assert result.exit_code == 0
    assert calls
    call = calls[0]
    assert call["no_postgres"] is False
    assert call["mode"] == "local"
    assert call["skip_llm_setup"] is True
    assert call["configure_mcp"] is False
    assert call["browser_type"] == "chromium-headless"
    assert call["analytics_id"] == "anonymous"
    assert call["return_result"] is True


def test_quickstart_non_interactive_without_database_string_skips_postgres_prompt(monkeypatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr("skyvern.cli.quickstart._has_server_quickstart_extra", lambda: True)
    monkeypatch.setattr("skyvern.cli.quickstart.check_docker_compose_file", lambda: False)
    monkeypatch.setattr("skyvern.cli.quickstart._run_server_quickstart", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        "skyvern.cli.quickstart.Confirm.ask",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not prompt")),
    )

    result = CliRunner().invoke(quickstart_app, ["--non-interactive"])

    assert result.exit_code == 0
    assert "Non-interactive quickstart will not prompt to start PostgreSQL" in result.output
    assert "--database-string" in result.output
    assert calls
    call = calls[0]
    assert call["no_postgres"] is True
    assert call["database_string"] == ""
    assert call["skip_llm_setup"] is True
    assert call["configure_mcp"] is False
    assert call["browser_type"] == "chromium-headless"
    assert call["analytics_id"] == "anonymous"
    assert call["start_services_now"] is False
