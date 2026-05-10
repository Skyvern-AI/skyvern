from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import skyvern.cli.quickstart as quickstart_module


def test_quickstart_without_server_extra_prints_install_paths(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [])

    assert result.exit_code == 0
    assert "Cloud/API SDK usage" in result.output
    assert "Embedded local Python SDK via skyvern[local]" in result.output
    assert "Self-hosted local server via skyvern[server]" in result.output
    assert "Next command: skyvern setup" in result.output
    assert "Skyvern(api_key=" in result.output
    assert 'pip install "skyvern[local]"' in result.output
    assert 'pip install "skyvern[server]"' in result.output
    assert "Postgres" in result.output
    assert "Missing Dependency" not in result.output
    assert "Missing:" not in result.output


def test_quickstart_with_local_extra_prints_embedded_guidance(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [])

    assert result.exit_code == 0
    assert "Embedded local Python SDK via skyvern[local]" in result.output
    assert "Installed: skyvern[local]" in result.output
    assert "Next command: python -m playwright install chromium" in result.output
    assert "Skyvern.local(use_in_memory_db=True)" in result.output
    assert "This path does not require Postgres" in result.output
    assert "Missing Dependency" not in result.output
    assert "Missing:" not in result.output


def test_quickstart_explicit_local_choice_without_local_extra_prints_install_steps(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--install-type", "local"])

    assert result.exit_code == 0
    assert "Choose how you want to use Skyvern" not in result.output
    assert 'Install: pip install "skyvern[local]"' in result.output
    assert "Next command: python -m playwright install chromium" in result.output
    assert "Skyvern.local(use_in_memory_db=True)" in result.output


def test_quickstart_server_flags_select_server_path_without_server_extra(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)

    result = CliRunner().invoke(
        quickstart_module.quickstart_app,
        ["--database-string", "postgresql+psycopg://user/db"],
    )

    assert result.exit_code == 0
    assert "Embedded local Python SDK" not in result.output
    assert "Cloud/API SDK usage" not in result.output
    assert 'Install: pip install "skyvern[server]"' in result.output
    assert "Next: python -m skyvern quickstart" in result.output


def test_quickstart_explicit_install_type_overrides_server_flags(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--install-type", "local", "--server-only"])

    assert result.exit_code == 0
    assert "Embedded local Python SDK" in result.output
    assert 'Install: pip install "skyvern[server]"' not in result.output


def test_quickstart_server_extra_probe_rejects_local_with_incidental_uvicorn(monkeypatch) -> None:
    monkeypatch.setattr(
        "skyvern.exceptions.find_spec",
        lambda module_name: None if module_name == "fastmcp" else object(),
    )

    assert quickstart_module._has_server_quickstart_extra() is False


def test_quickstart_interactive_server_choice_without_server_extra_prints_install_steps(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: True)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [], input="3\n")

    assert result.exit_code == 0
    assert "Choose a quickstart path" in result.output
    assert 'Install: pip install "skyvern[server]"' in result.output
    assert "Next: python -m skyvern quickstart" in result.output
    assert "local server, database, local API key, and MCP" in result.output
    assert "Wheel installs run the backend only" in result.output


def test_quickstart_interactive_choice_accepts_alias(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: True)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [], input="local\n")

    assert result.exit_code == 0
    assert 'Install: pip install "skyvern[local]"' in result.output
    assert "Choose a quickstart path" in result.output


def test_quickstart_interactive_choice_reprompts_invalid_alias(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: True)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [], input="wat\nserver\n")

    assert result.exit_code == 0
    assert "Choose one of: cloud/api, local/embedded, server/self-hosted, 1, 2, or 3." in result.output
    assert "Please select a valid option:" not in result.output
    assert 'Install: pip install "skyvern[server]"' in result.output


def test_quickstart_interactive_eof_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: True)

    def raise_eof(*_args, **_kwargs) -> str:
        raise EOFError

    monkeypatch.setattr(quickstart_module.Prompt, "ask", raise_eof)

    result = CliRunner().invoke(quickstart_module.quickstart_app, [])

    assert result.exit_code == 0
    assert "Installed: skyvern[local]" in result.output
    assert "Skyvern.local(use_in_memory_db=True)" in result.output


def test_quickstart_server_flags_skip_docker_compose_offer_without_server_extra(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(quickstart_module, "check_docker", lambda: True)
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)
    monkeypatch.setattr(quickstart_module, "run_docker_compose_setup", lambda: calls.append("docker"))

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--server-only"])

    assert result.exit_code == 0
    assert "Docker Compose file detected" not in result.output
    assert 'Install: pip install "skyvern[server]"' in result.output
    assert calls == []


def test_quickstart_server_choice_can_use_docker_compose_without_server_extra(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(quickstart_module, "check_docker", lambda: True)
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: True)
    monkeypatch.setattr(quickstart_module, "run_docker_compose_setup", lambda: calls.append("docker"))

    result = CliRunner().invoke(quickstart_module.quickstart_app, [], input="3\ny\n")

    assert result.exit_code == 0
    assert "Docker Compose file detected" in result.output
    assert calls == ["docker"]
    assert 'Install: pip install "skyvern[server]"' not in result.output


def test_quickstart_docker_compose_rejects_conflicting_install_type(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(quickstart_module, "check_docker", lambda: True)
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "run_docker_compose_setup", lambda: calls.append("docker"))

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--docker-compose", "--install-type", "cloud"])

    assert result.exit_code == 1
    assert "Conflicting quickstart options" in result.output
    assert "--docker-compose` starts the self-hosted server stack" in result.output
    assert calls == []


def test_quickstart_invalid_install_type_fails_before_welcome_banner(monkeypatch) -> None:
    monkeypatch.setattr(
        quickstart_module,
        "_has_server_quickstart_extra",
        lambda: (_ for _ in ()).throw(AssertionError("should not enter command body")),
    )

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--docker-compose", "--install-type", "wat"])

    assert result.exit_code == 2
    assert "Starting Skyvern Quickstart" not in result.output
    assert "Choose one of:" in result.output
    assert "local/embedded" in result.output
    assert "server/self-hosted" in result.output


def test_quickstart_with_server_extra_preserves_existing_flow(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)
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


def test_quickstart_docker_compose_bypasses_server_extra_guard(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(quickstart_module, "check_docker", lambda: True)
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "run_docker_compose_setup", lambda: calls.append("docker"))

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--docker-compose"])

    assert result.exit_code == 0
    assert calls == ["docker"]
    assert 'pip install "skyvern[server]"' not in result.output


@pytest.mark.asyncio
async def test_start_services_without_frontend_runtime_starts_backend_only(monkeypatch) -> None:
    import skyvern.cli.utils as utils

    commands = []

    class FakeProcess:
        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args):
        commands.append(args)
        return FakeProcess()

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(utils, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(utils, "resolve_backend_env_path", lambda: Path(".env"))
    monkeypatch.setattr(utils.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(utils.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(utils, "capture_setup_event", lambda *args, **kwargs: None)

    await utils.start_services()

    assert commands == [(utils.sys.executable, "-m", "skyvern.cli.commands", "run", "server")]
