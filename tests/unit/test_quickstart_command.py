from __future__ import annotations

from typer.testing import CliRunner

import skyvern.cli.quickstart as quickstart_module


def test_quickstart_without_server_extra_prints_install_paths(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [])

    assert result.exit_code == 0
    assert "Cloud/API SDK" in result.output
    assert 'pip install "skyvern[local]"' in result.output
    assert "Skyvern.local(use_in_memory_db=True)" in result.output
    assert 'pip install "skyvern[server]"' in result.output
    assert "Postgres" in result.output
    assert "Missing Dependency" not in result.output
    assert "Missing:" not in result.output


def test_quickstart_with_server_extra_preserves_existing_flow(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: True)
    monkeypatch.setattr(
        quickstart_module,
        "_run_server_quickstart",
        lambda **kwargs: calls.append(kwargs),
    )

    result = CliRunner().invoke(
        quickstart_module.quickstart_app,
        [
            "--no-postgres",
            "--database-string",
            "postgresql+psycopg://user/db",
            "--skip-browser-install",
            "--server-only",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "no_postgres": True,
            "database_string": "postgresql+psycopg://user/db",
            "skip_browser_install": True,
            "server_only": True,
        }
    ]
