from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

import skyvern.cli.quickstart as quickstart_module
from skyvern.cli.llm_setup import update_or_add_env_var
from skyvern.utils.env_paths import (
    BACKEND_ENV_FILE_ENV_VAR,
    BACKEND_ENV_INTENT_ENV_VAR,
    EnvIntent,
    EnvScope,
    backend_env_path_for_scope,
    load_backend_env_files,
    resolve_backend_env_path,
)


def _set_home(monkeypatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


def test_backend_env_read_uses_intent_specific_precedence(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(BACKEND_ENV_FILE_ENV_VAR, raising=False)

    legacy_env = tmp_path / ".env"
    global_env = tmp_path / "home" / ".skyvern" / ".env"
    global_env.parent.mkdir(parents=True)
    legacy_env.write_text("SKYVERN_BASE_URL=http://legacy\n")
    global_env.write_text("SKYVERN_BASE_URL=http://global\n")

    assert resolve_backend_env_path() == legacy_env
    assert resolve_backend_env_path(intent=EnvIntent.SERVER) == legacy_env
    assert resolve_backend_env_path(intent=EnvIntent.CLOUD) == global_env
    assert backend_env_path_for_scope("cwd") == legacy_env
    assert backend_env_path_for_scope("2") == tmp_path / ".skyvern" / ".env"


def test_backend_env_loader_layers_cloud_scopes_and_keeps_server_legacy(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(BACKEND_ENV_FILE_ENV_VAR, raising=False)
    for key in ("SKYVERN_API_KEY", "SKYVERN_BASE_URL", "GLOBAL_ONLY", "LEGACY_ONLY", BACKEND_ENV_INTENT_ENV_VAR):
        monkeypatch.delenv(key, raising=False)

    legacy_env = tmp_path / ".env"
    project_env = tmp_path / ".skyvern" / ".env"
    global_env = tmp_path / "home" / ".skyvern" / ".env"
    project_env.parent.mkdir(parents=True)
    global_env.parent.mkdir(parents=True)
    legacy_env.write_text("SKYVERN_API_KEY=legacy-key\nLEGACY_ONLY=legacy\n")
    global_env.write_text("SKYVERN_API_KEY=global-key\nGLOBAL_ONLY=global\n")
    project_env.write_text("SKYVERN_BASE_URL=http://project\n")

    assert load_backend_env_files(intent=EnvIntent.CLOUD) == project_env
    assert os.environ["SKYVERN_API_KEY"] == "global-key"
    assert os.environ["SKYVERN_BASE_URL"] == "http://project"
    assert os.environ["GLOBAL_ONLY"] == "global"
    assert os.environ["LEGACY_ONLY"] == "legacy"
    assert os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.CLOUD.value

    for key in ("SKYVERN_API_KEY", "SKYVERN_BASE_URL", "GLOBAL_ONLY", "LEGACY_ONLY"):
        monkeypatch.delenv(key, raising=False)
    assert load_backend_env_files(intent=EnvIntent.SERVER) == legacy_env
    assert os.environ["SKYVERN_API_KEY"] == "legacy-key"
    assert "SKYVERN_BASE_URL" not in os.environ
    assert os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.SERVER.value


def test_backend_env_loader_preserves_scope_precedence_across_staged_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(BACKEND_ENV_FILE_ENV_VAR, raising=False)
    for key in ("SKYVERN_API_KEY", "GLOBAL_PROD_ONLY", BACKEND_ENV_INTENT_ENV_VAR):
        monkeypatch.delenv(key, raising=False)

    project_env = tmp_path / ".skyvern" / ".env"
    global_prod_env = tmp_path / "home" / ".skyvern" / ".env.prod"
    project_env.parent.mkdir(parents=True)
    global_prod_env.parent.mkdir(parents=True)
    project_env.write_text("SKYVERN_API_KEY=project-key\n")
    global_prod_env.write_text("SKYVERN_API_KEY=global-prod-key\nGLOBAL_PROD_ONLY=yes\n")

    assert load_backend_env_files(intent=EnvIntent.CLOUD) == project_env
    assert os.environ["SKYVERN_API_KEY"] == "project-key"
    assert os.environ["GLOBAL_PROD_ONLY"] == "yes"
    assert os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.CLOUD.value


def test_backend_env_scope_normalization_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="Choose one of:"):
        backend_env_path_for_scope("workspace")


def test_backend_env_loader_preserves_staged_dotenv_precedence(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(BACKEND_ENV_FILE_ENV_VAR, raising=False)
    for key in ("PORT", "SKYVERN_BASE_URL", BACKEND_ENV_INTENT_ENV_VAR):
        monkeypatch.delenv(key, raising=False)

    legacy_env = tmp_path / ".env"
    legacy_prod_env = tmp_path / ".env.prod"
    legacy_env.write_text("PORT=8000\nSKYVERN_BASE_URL=http://server\n")
    legacy_prod_env.write_text("PORT=9000\n")

    assert load_backend_env_files(intent=EnvIntent.SERVER) == legacy_env
    assert os.environ["PORT"] == "9000"
    assert os.environ["SKYVERN_BASE_URL"] == "http://server"


def test_server_intent_settings_ignore_cloud_env_files(tmp_path) -> None:
    home = tmp_path / "home"
    global_env = home / ".skyvern" / ".env"
    global_env.parent.mkdir(parents=True)
    global_env.write_text("SKYVERN_BASE_URL=http://cloud\nPORT=9001\n")
    (tmp_path / ".env").write_text("SKYVERN_BASE_URL=http://server\nPORT=8765\n")

    script = """
import json
import os
from skyvern.utils.env_paths import EnvIntent, load_backend_env_files
for key in ("SKYVERN_BASE_URL", "PORT", "SKYVERN_ENV_INTENT"):
    os.environ.pop(key, None)
load_backend_env_files(intent=EnvIntent.SERVER)
from skyvern.config import settings
print(json.dumps({"base_url": settings.SKYVERN_BASE_URL, "port": settings.PORT}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {"base_url": "http://server", "port": 8765}


def test_unscoped_settings_import_prefers_legacy_env(tmp_path) -> None:
    home = tmp_path / "home"
    project_env = tmp_path / ".skyvern" / ".env"
    global_env = home / ".skyvern" / ".env"
    project_env.parent.mkdir(parents=True)
    global_env.parent.mkdir(parents=True)
    (tmp_path / ".env").write_text("SKYVERN_BASE_URL=http://legacy\nPORT=8765\n")
    project_env.write_text("SKYVERN_BASE_URL=http://project\nPORT=9001\n")
    global_env.write_text("SKYVERN_BASE_URL=http://global\nPORT=9002\n")

    script = """
import json
from skyvern.config import settings
print(json.dumps({"base_url": settings.SKYVERN_BASE_URL, "port": settings.PORT}))
"""
    env = {**os.environ, "HOME": str(home)}
    for key in ("SKYVERN_BASE_URL", "PORT", "SKYVERN_ENV_INTENT", "SKYVERN_ENV_FILE"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {"base_url": "http://legacy", "port": 8765}


def test_run_commands_import_does_not_initialize_settings_before_server_intent(tmp_path) -> None:
    home = tmp_path / "home"
    global_env = home / ".skyvern" / ".env"
    global_env.parent.mkdir(parents=True)
    global_env.write_text("PORT=9001\n")
    (tmp_path / ".env").write_text("PORT=8765\n")

    script = """
import json
import os
import sys
for key in ("PORT", "SKYVERN_ENV_INTENT"):
    os.environ.pop(key, None)
import skyvern.cli.run_commands
config_imported_before_intent = "skyvern.config" in sys.modules
from skyvern.utils.env_paths import EnvIntent, load_backend_env_files
load_backend_env_files(intent=EnvIntent.SERVER)
from skyvern.config import settings
print(json.dumps({"config_imported_before_intent": config_imported_before_intent, "port": settings.PORT}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {"config_imported_before_intent": False, "port": 8765}


def test_run_mcp_prepares_cloud_env_before_starting_mcp(tmp_path, monkeypatch) -> None:
    from skyvern import _cli_bootstrap
    from skyvern.cli import run_commands

    project_env = tmp_path / ".skyvern" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text("SKYVERN_BASE_URL=http://project\nSKYVERN_API_KEY=project-key\n")
    (tmp_path / ".env").write_text("SKYVERN_BASE_URL=http://legacy\nSKYVERN_API_KEY=legacy-key\n")

    events: list[str] = []

    fake_forge_log = types.ModuleType("skyvern.forge.sdk.forge_log")

    def fake_setup_logger() -> None:
        assert os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.CLOUD.value
        assert os.environ["SKYVERN_BASE_URL"] == "http://project"
        events.append("setup_logger")

    fake_forge_log.setup_logger = fake_setup_logger

    fake_auth = types.ModuleType("skyvern.cli.core.mcp_http_auth")
    fake_auth.MCPAPIKeyMiddleware = object

    fake_session_manager = types.ModuleType("skyvern.cli.core.session_manager")
    fake_session_manager.set_stateless_http_mode = lambda enabled: events.append(f"stateless:{enabled}")

    fake_telemetry = types.ModuleType("skyvern.cli.mcp_tools.telemetry")

    def fake_configure_mcp_telemetry_runtime(*, server_mode: str, transport: str | None) -> None:
        assert os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.CLOUD.value
        assert os.environ["SKYVERN_BASE_URL"] == "http://project"
        events.append(f"telemetry:{server_mode}:{transport}")

    fake_telemetry.configure_mcp_telemetry_runtime = fake_configure_mcp_telemetry_runtime

    fake_mcp_tools = types.ModuleType("skyvern.cli.mcp_tools")

    class FakeMCP:
        def run(self, *, transport: str, **_: object) -> None:
            assert os.environ[BACKEND_ENV_INTENT_ENV_VAR] == EnvIntent.CLOUD.value
            assert os.environ["SKYVERN_BASE_URL"] == "http://project"
            events.append(f"run:{transport}")

    fake_mcp_tools.mcp = FakeMCP()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ("SKYVERN_API_KEY", "SKYVERN_BASE_URL", BACKEND_ENV_INTENT_ENV_VAR):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_cli_bootstrap, "_RUNTIME_LOGGING_CONFIGURED", False)
    monkeypatch.setattr(run_commands.atexit, "register", lambda _: None)
    monkeypatch.setattr(run_commands, "_cleanup_mcp_resources_blocking", lambda: events.append("cleanup"))
    monkeypatch.setitem(sys.modules, "skyvern.forge.sdk.forge_log", fake_forge_log)
    monkeypatch.setitem(sys.modules, "skyvern.cli.core.mcp_http_auth", fake_auth)
    monkeypatch.setitem(sys.modules, "skyvern.cli.core.session_manager", fake_session_manager)
    monkeypatch.setitem(sys.modules, "skyvern.cli.mcp_tools", fake_mcp_tools)
    monkeypatch.setitem(sys.modules, "skyvern.cli.mcp_tools.telemetry", fake_telemetry)

    run_commands.run_mcp()

    assert events == [
        "setup_logger",
        "telemetry:local_cli:stdio",
        "stateless:False",
        "run:stdio",
        "stateless:False",
        "cleanup",
    ]


@pytest.mark.parametrize(
    "module_name",
    [
        "skyvern.cli.workflow",
        "skyvern.cli.credential",
        "skyvern.cli.schedule_command",
        "skyvern.cli.config_command",
        "skyvern.cli.block",
        "skyvern.cli.setup_commands",
        "skyvern.cli.mcp_commands",
        "skyvern.cli.init_command",
        "skyvern.cli.quickstart",
    ],
)
def test_cloud_command_import_does_not_initialize_settings_before_cloud_intent(
    tmp_path: Path, module_name: str
) -> None:
    home = tmp_path / "home"
    project_env = tmp_path / ".skyvern" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text("SKYVERN_BASE_URL=http://project\n")
    (tmp_path / ".env").write_text("SKYVERN_BASE_URL=http://legacy\n")

    script = f"""
import importlib
import json
import os
import sys
for key in ("SKYVERN_BASE_URL", "SKYVERN_ENV_INTENT"):
    os.environ.pop(key, None)
importlib.import_module({module_name!r})
config_imported_before_intent = "skyvern.config" in sys.modules
from skyvern.utils.env_paths import EnvIntent, load_backend_env_files
load_backend_env_files(intent=EnvIntent.CLOUD)
from skyvern.config import settings
payload = {{
    "config_imported_before_intent": config_imported_before_intent,
    "base_url": settings.SKYVERN_BASE_URL,
}}
print(json.dumps(payload))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "config_imported_before_intent": False,
        "base_url": "http://project",
    }


def test_setup_credentials_use_cloud_and_local_env_intents(tmp_path) -> None:
    home = tmp_path / "home"
    project_env = tmp_path / ".skyvern" / ".env"
    global_env = home / ".skyvern" / ".env"
    project_env.parent.mkdir(parents=True)
    global_env.parent.mkdir(parents=True)
    (tmp_path / ".env").write_text("SKYVERN_API_KEY=legacy-key\nSKYVERN_BASE_URL=http://legacy\n")
    project_env.write_text("SKYVERN_API_KEY=project-key\nSKYVERN_BASE_URL=http://project\n")
    global_env.write_text("SKYVERN_API_KEY=global-key\nSKYVERN_BASE_URL=http://global\n")

    env = {**os.environ, "HOME": str(home)}
    for key in ("SKYVERN_API_KEY", "SKYVERN_BASE_URL", "SKYVERN_ENV_INTENT", "SKYVERN_ENV_FILE"):
        env.pop(key, None)

    remote_script = """
import json
from skyvern.cli.setup_commands import _get_env_credentials
print(json.dumps(dict(zip(("api_key", "base_url"), _get_env_credentials()))))
"""
    remote_result = subprocess.run(
        [sys.executable, "-c", remote_script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(remote_result.stdout) == {"api_key": "project-key", "base_url": "http://project"}

    local_script = """
import json
from skyvern.cli.setup_commands import _get_local_env_credentials
print(json.dumps(dict(zip(("api_key", "base_url"), _get_local_env_credentials()))))
"""
    local_result = subprocess.run(
        [sys.executable, "-c", local_script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(local_result.stdout) == {"api_key": "legacy-key", "base_url": "http://legacy"}


def test_backend_env_write_defaults_are_intent_specific(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(BACKEND_ENV_FILE_ENV_VAR, raising=False)

    assert resolve_backend_env_path(intent=EnvIntent.CLOUD, for_write=True) == backend_env_path_for_scope(
        EnvScope.GLOBAL
    )
    assert resolve_backend_env_path(intent=EnvIntent.LOCAL, for_write=True) == backend_env_path_for_scope(
        EnvScope.PROJECT
    )
    assert resolve_backend_env_path(intent=EnvIntent.SERVER, for_write=True) == backend_env_path_for_scope(
        EnvScope.LEGACY
    )

    monkeypatch.setenv(BACKEND_ENV_FILE_ENV_VAR, str(tmp_path / "custom.env"))
    assert resolve_backend_env_path(intent=EnvIntent.CLOUD, for_write=True) == tmp_path / "custom.env"


def test_update_or_add_env_var_creates_selected_env_parent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _set_home(monkeypatch, tmp_path / "home")
    monkeypatch.delenv(BACKEND_ENV_FILE_ENV_VAR, raising=False)

    project_env = resolve_backend_env_path(scope=EnvScope.PROJECT, for_write=True)

    update_or_add_env_var("SKYVERN_BASE_URL", "http://localhost:8000", env_path=project_env)

    assert project_env.exists()
    assert not (tmp_path / ".env").exists()
    assert "SKYVERN_BASE_URL" in project_env.read_text()


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
    install_calls = []
    server_calls = []
    server_extra_checks = iter([False, False, True])

    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: next(server_extra_checks))
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)
    monkeypatch.setattr(
        quickstart_module,
        "_install_server_extra_for_quickstart",
        lambda: install_calls.append("install") or True,
    )
    monkeypatch.setattr(quickstart_module, "_run_server_quickstart", lambda **kwargs: server_calls.append(kwargs))

    result = CliRunner().invoke(
        quickstart_module.quickstart_app,
        ["--database-string", "postgresql+psycopg://user/db"],
    )

    assert result.exit_code == 0
    assert "Embedded local Python SDK" not in result.output
    assert "Cloud/API SDK usage" not in result.output
    assert install_calls == ["install"]
    assert server_calls == [
        {
            "no_postgres": False,
            "database_string": "postgresql+psycopg://user/db",
            "skip_browser_install": False,
            "server_only": False,
        }
    ]


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

    result = CliRunner().invoke(quickstart_module.quickstart_app, [], input="3\nn\n")

    assert result.exit_code == 1
    assert "Choose a quickstart path" in result.output
    assert "Install the missing server dependencies now?" in result.output
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

    result = CliRunner().invoke(quickstart_module.quickstart_app, [], input="wat\nserver\nn\n")

    assert result.exit_code == 1
    assert "Choose one of: cloud/api, local/embedded, server/self-hosted, 1, 2, or 3." in result.output
    assert "Please select a valid option:" not in result.output
    assert "Install the missing server dependencies now?" in result.output
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
    install_calls = []
    server_calls = []
    server_extra_checks = iter([False, False, True])

    monkeypatch.setattr(quickstart_module, "check_docker", lambda: True)
    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: True)
    monkeypatch.setattr(quickstart_module, "_has_local_quickstart_extra", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: next(server_extra_checks))
    monkeypatch.setattr(quickstart_module, "_is_interactive_input", lambda: False)
    monkeypatch.setattr(quickstart_module, "run_docker_compose_setup", lambda: calls.append("docker"))
    monkeypatch.setattr(
        quickstart_module,
        "_install_server_extra_for_quickstart",
        lambda: install_calls.append("install") or True,
    )
    monkeypatch.setattr(quickstart_module, "_run_server_quickstart", lambda **kwargs: server_calls.append(kwargs))

    result = CliRunner().invoke(quickstart_module.quickstart_app, ["--server-only"])

    assert result.exit_code == 0
    assert "Docker Compose file detected" not in result.output
    assert install_calls == ["install"]
    assert server_calls == [
        {
            "no_postgres": False,
            "database_string": "",
            "skip_browser_install": False,
            "server_only": True,
        }
    ]
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


def test_quickstart_rejects_non_legacy_env_scope_for_server_init(monkeypatch) -> None:
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
            "--install-type",
            "server",
            "--env-scope",
            "project",
            "--server-only",
        ],
    )

    assert result.exit_code == 1
    assert "Self-hosted local server setup writes ./.env" in result.output
    assert calls == []


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
    monkeypatch.setattr(utils, "resolve_backend_env_path", lambda **_kwargs: Path(".env"))
    monkeypatch.setattr(utils.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(utils.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(utils, "capture_setup_event", lambda *args, **kwargs: None)

    await utils.start_services()

    assert commands == [(utils.sys.executable, "-m", "skyvern.cli.commands", "run", "server")]
