from __future__ import annotations

import importlib
import logging
import subprocess
from pathlib import Path

import pytest
import typer

from skyvern.cli import run_commands


def test_run_ui_without_source_or_packaged_assets_prints_install_hint(monkeypatch, capsys) -> None:
    port_checks: list[int] = []

    monkeypatch.setattr(run_commands, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(run_commands, "installed_ui_dist_available", lambda: False)
    monkeypatch.setattr(run_commands, "is_interactive", lambda: False)
    monkeypatch.setattr(
        run_commands,
        "_handle_port_conflict",
        lambda port, **_kwargs: port_checks.append(port) or True,
    )

    run_commands.run_ui()

    captured = capsys.readouterr()
    assert "Skyvern UI assets are not installed" in captured.out
    assert 'pip install "skyvern[ui]"' in captured.out
    assert port_checks == []


def test_run_ui_can_install_packaged_assets_interactively(tmp_path, monkeypatch) -> None:
    install_commands: list[list[str]] = []
    prompts: list[str] = []
    cache_invalidated = False
    availability = iter([False, True, True])
    served = []

    monkeypatch.setattr(run_commands, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(run_commands, "installed_ui_dist_available", lambda: next(availability))
    monkeypatch.setattr(run_commands, "is_interactive", lambda: True)
    monkeypatch.setattr(run_commands, "_ui_install_target", lambda: "skyvern-ui==1.0.36")
    monkeypatch.setattr(
        run_commands,
        "_ui_install_command",
        lambda install_target: [run_commands.sys.executable, "-m", "pip", "install", install_target],
    )
    monkeypatch.setattr(
        run_commands.Confirm,
        "ask",
        lambda message, **_kwargs: prompts.append(message) or True,
    )
    monkeypatch.setattr(
        run_commands,
        "_handle_port_conflict",
        lambda _port, **_kwargs: True,
    )
    monkeypatch.setattr(run_commands, "prepare_installed_ui_dist", lambda _config: tmp_path / "runtime")

    def fake_run(command: list[str], **_kwargs):
        install_commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    def fake_invalidate_caches() -> None:
        nonlocal cache_invalidated
        cache_invalidated = True

    def fake_serve(dist_dir: Path, *, ui_port: int, artifact_port: int, artifact_token: str | None = None) -> None:
        del artifact_token
        served.append((dist_dir, ui_port, artifact_port))

    monkeypatch.setattr(run_commands.subprocess, "run", fake_run)
    monkeypatch.setattr(run_commands.importlib, "invalidate_caches", fake_invalidate_caches)
    monkeypatch.setattr(run_commands, "serve_installed_ui", fake_serve)

    run_commands.run_ui()

    assert prompts == ["Install packaged Skyvern UI assets now (skyvern-ui==1.0.36)?"]
    assert install_commands == [[run_commands.sys.executable, "-m", "pip", "install", "skyvern-ui==1.0.36"]]
    assert cache_invalidated is True
    assert served == [(tmp_path / "runtime", run_commands.UI_PORT, run_commands.ARTIFACT_PORT)]


def test_run_ui_install_ui_flag_skips_interactive_prompt(tmp_path, monkeypatch) -> None:
    install_commands: list[list[str]] = []
    availability = iter([False, True])
    served = []

    monkeypatch.setattr(run_commands, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(run_commands, "installed_ui_dist_available", lambda: next(availability))
    monkeypatch.setattr(run_commands, "_ui_install_target", lambda: "skyvern-ui==1.0.36")
    monkeypatch.setattr(
        run_commands,
        "_ui_install_command",
        lambda install_target: [run_commands.sys.executable, "-m", "pip", "install", install_target],
    )
    monkeypatch.setattr(
        run_commands.Confirm,
        "ask",
        lambda *_args, **_kwargs: pytest.fail("run ui --install-ui should not prompt"),
    )
    monkeypatch.setattr(
        run_commands,
        "_handle_port_conflict",
        lambda _port, **_kwargs: True,
    )
    monkeypatch.setattr(run_commands, "prepare_installed_ui_dist", lambda _config: tmp_path / "runtime")

    def fake_run(command: list[str], **_kwargs):
        install_commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    def fake_serve(dist_dir: Path, *, ui_port: int, artifact_port: int, artifact_token: str | None = None) -> None:
        del artifact_token
        served.append((dist_dir, ui_port, artifact_port))

    monkeypatch.setattr(run_commands.subprocess, "run", fake_run)
    monkeypatch.setattr(run_commands, "serve_installed_ui", fake_serve)

    run_commands.run_ui(install_ui=True)

    assert install_commands == [[run_commands.sys.executable, "-m", "pip", "install", "skyvern-ui==1.0.36"]]
    assert served == [(tmp_path / "runtime", run_commands.UI_PORT, run_commands.ARTIFACT_PORT)]


def test_run_all_can_install_ui_before_starting_services(monkeypatch) -> None:
    calls = []

    async def fake_start_services() -> None:
        calls.append("start")

    monkeypatch.setattr(run_commands, "has_frontend_runtime", lambda: False)
    monkeypatch.setattr(run_commands, "_missing_run_all_dependencies", lambda: [])
    monkeypatch.setattr(
        run_commands,
        "_install_packaged_ui_if_requested",
        lambda *, assume_yes: calls.append(("install", assume_yes)) or True,
    )
    monkeypatch.setattr("skyvern.cli.utils.start_services", fake_start_services)

    run_commands.run_all(install_ui=True)

    assert calls == [("install", True), "start"]


def test_run_all_install_ui_exits_when_explicit_install_fails(monkeypatch) -> None:
    calls = []

    async def fake_start_services() -> None:
        calls.append("start")

    monkeypatch.setattr(run_commands, "has_frontend_runtime", lambda: False)
    monkeypatch.setattr(run_commands, "_missing_run_all_dependencies", lambda: [])
    monkeypatch.setattr(
        run_commands,
        "_install_packaged_ui_if_requested",
        lambda *, assume_yes: calls.append(("install", assume_yes)) or False,
    )
    monkeypatch.setattr("skyvern.cli.utils.start_services", fake_start_services)

    with pytest.raises(typer.Exit) as exc_info:
        run_commands.run_all(install_ui=True)

    assert exc_info.value.exit_code == 1
    assert calls == [("install", True)]


def test_run_all_without_install_ui_can_degrade_to_backend_only(monkeypatch) -> None:
    calls = []

    async def fake_start_services() -> None:
        calls.append("start")

    monkeypatch.setattr(run_commands, "has_frontend_runtime", lambda: False)
    monkeypatch.setattr(run_commands, "_missing_run_all_dependencies", lambda: [])
    monkeypatch.setattr(
        run_commands,
        "_install_packaged_ui_if_requested",
        lambda *, assume_yes: calls.append(("install", assume_yes)) or False,
    )
    monkeypatch.setattr("skyvern.cli.utils.start_services", fake_start_services)

    run_commands.run_all()

    assert calls == [("install", False), "start"]


def test_install_packaged_ui_warns_when_non_interactive(monkeypatch, capsys, caplog) -> None:
    monkeypatch.setattr(run_commands, "is_interactive", lambda: False)
    monkeypatch.setattr(run_commands, "_ui_install_target", lambda: "skyvern-ui==1.0.36")
    monkeypatch.setattr(
        run_commands,
        "_ui_install_command",
        lambda install_target: [run_commands.sys.executable, "-m", "pip", "install", install_target],
    )

    with caplog.at_level(logging.WARNING):
        installed = run_commands._install_packaged_ui_if_requested()

    captured = capsys.readouterr()
    assert installed is False
    assert "Automatic install was skipped" in captured.out
    assert any(record.message == "packaged_ui_install_skipped_non_interactive" for record in caplog.records)


def test_run_all_skips_ui_install_when_runtime_exists(monkeypatch) -> None:
    calls = []

    async def fake_start_services() -> None:
        calls.append("start")

    monkeypatch.setattr(run_commands, "has_frontend_runtime", lambda: True)
    monkeypatch.setattr(run_commands, "_missing_run_all_dependencies", lambda: [])
    monkeypatch.setattr(
        run_commands,
        "_install_packaged_ui_if_requested",
        lambda *, assume_yes: calls.append(("install", assume_yes)) or True,
    )
    monkeypatch.setattr("skyvern.cli.utils.start_services", fake_start_services)

    run_commands.run_all()

    assert calls == ["start"]


def test_run_all_without_server_dependencies_prints_all_extra_hint(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_commands, "_missing_run_all_dependencies", lambda: ["uvicorn", "sqlalchemy"])
    monkeypatch.setattr(
        "skyvern.cli.utils.start_services",
        lambda: pytest.fail("run all should not import/start services when server deps are missing"),
    )

    with pytest.raises(typer.Exit):
        run_commands.run_all(install_ui=True)

    captured = capsys.readouterr()
    assert "`skyvern run all` needs the local server dependencies" in captured.out
    assert 'pip install "skyvern[all]"' in captured.out
    assert "skyvern run ui --install-ui" in captured.out


def test_ui_install_target_matches_installed_skyvern_version(monkeypatch) -> None:
    monkeypatch.setattr(run_commands.importlib.metadata, "version", lambda _package: "1.2.3")

    assert run_commands._ui_install_target() == "skyvern-ui==1.2.3"


def test_ui_install_target_falls_back_when_skyvern_metadata_is_missing(monkeypatch) -> None:
    def raise_package_not_found(_package: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(run_commands.importlib.metadata, "version", raise_package_not_found)

    assert run_commands._ui_install_target() == "skyvern-ui"


def test_ui_install_command_prefers_uv_inside_virtualenv(monkeypatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/.venv")
    monkeypatch.setattr(run_commands.shutil, "which", lambda binary: "/usr/local/bin/uv" if binary == "uv" else None)

    assert run_commands._ui_install_command("skyvern-ui==1.0.36") == [
        "/usr/local/bin/uv",
        "pip",
        "install",
        "skyvern-ui==1.0.36",
    ]


def test_ui_install_command_uses_python_pip_without_uv_virtualenv(monkeypatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(run_commands.shutil, "which", lambda _binary: "/usr/local/bin/uv")

    assert run_commands._ui_install_command("skyvern-ui==1.0.36") == [
        run_commands.sys.executable,
        "-m",
        "pip",
        "install",
        "skyvern-ui==1.0.36",
    ]


def test_installed_ui_config_accepts_frontend_env_vars(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VITE_API_BASE_URL", "https://api.example.com/api/v1")
    monkeypatch.delenv("VITE_WSS_BASE_URL", raising=False)
    monkeypatch.setenv("VITE_ARTIFACT_API_BASE_URL", "https://artifacts.example.com")
    monkeypatch.setenv("VITE_SKYVERN_API_KEY", "frontend-key")
    monkeypatch.setenv("VITE_BROWSER_STREAMING_MODE", "cdp")
    monkeypatch.setattr(run_commands, "resolve_backend_env_path", lambda **_kwargs: tmp_path / ".env")

    config = run_commands._installed_ui_config()

    assert config.api_base_url == "https://api.example.com/api/v1"
    assert config.wss_base_url == "wss://api.example.com/api/v1"
    assert config.artifact_api_base_url == "https://artifacts.example.com"
    assert config.skyvern_api_key == "frontend-key"
    assert config.browser_streaming_mode == "cdp"


def test_run_ui_with_packaged_assets_serves_python_runtime(tmp_path, monkeypatch) -> None:
    port_checks: list[int] = []
    prepared_configs = []
    served = []

    monkeypatch.setenv("PORT", "8765")
    monkeypatch.setenv("SKYVERN_API_KEY", "test-key")
    monkeypatch.setenv("BROWSER_STREAMING_MODE", "cdp")
    monkeypatch.setattr(run_commands, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(run_commands, "installed_ui_dist_available", lambda: True)
    monkeypatch.setattr(run_commands, "resolve_backend_env_path", lambda **_kwargs: tmp_path / ".env")
    monkeypatch.setattr(run_commands.secrets, "token_urlsafe", lambda _size: "test-token")
    monkeypatch.setattr(
        run_commands,
        "_handle_port_conflict",
        lambda port, **_kwargs: port_checks.append(port) or True,
    )

    def fake_prepare(config):
        prepared_configs.append(config)
        return tmp_path / "runtime"

    def fake_serve(dist_dir: Path, *, ui_port: int, artifact_port: int, artifact_token: str | None = None) -> None:
        served.append((dist_dir, ui_port, artifact_port, artifact_token))

    monkeypatch.setattr(run_commands, "prepare_installed_ui_dist", fake_prepare)
    monkeypatch.setattr(run_commands, "serve_installed_ui", fake_serve)

    run_commands.run_ui()

    assert port_checks == [run_commands.UI_PORT, run_commands.ARTIFACT_PORT]
    assert len(prepared_configs) == 1
    config = prepared_configs[0]
    assert config.api_base_url == "http://localhost:8765/api/v1"
    assert config.wss_base_url == "ws://localhost:8765/api/v1"
    assert config.artifact_api_base_url == "http://localhost:9090/test-token"
    assert config.skyvern_api_key == "test-key"
    assert config.browser_streaming_mode == "cdp"
    assert served == [(tmp_path / "runtime", run_commands.UI_PORT, run_commands.ARTIFACT_PORT, "test-token")]


def test_run_ui_with_packaged_assets_accepts_remote_api_options(tmp_path, monkeypatch) -> None:
    prepared_configs = []
    served = []

    monkeypatch.setattr(run_commands, "resolve_frontend_env_path", lambda: None)
    monkeypatch.setattr(run_commands, "installed_ui_dist_available", lambda: True)
    monkeypatch.setattr(run_commands, "resolve_backend_env_path", lambda **_kwargs: tmp_path / ".env")
    monkeypatch.setattr(run_commands.secrets, "token_urlsafe", lambda _size: "test-token")
    monkeypatch.setattr(run_commands, "_handle_port_conflict", lambda _port, **_kwargs: True)

    def fake_prepare(config):
        prepared_configs.append(config)
        return tmp_path / "runtime"

    def fake_serve(dist_dir: Path, *, ui_port: int, artifact_port: int, artifact_token: str | None = None) -> None:
        served.append((dist_dir, ui_port, artifact_port, artifact_token))

    monkeypatch.setattr(run_commands, "prepare_installed_ui_dist", fake_prepare)
    monkeypatch.setattr(run_commands, "serve_installed_ui", fake_serve)

    run_commands.run_ui(
        api_url="https://api.example.com/api/v1",
        artifact_api_url="https://artifacts.example.com",
        api_key="remote-key",
        browser_streaming_mode="cdp",
    )

    assert len(prepared_configs) == 1
    config = prepared_configs[0]
    assert config.api_base_url == "https://api.example.com/api/v1"
    assert config.wss_base_url == "wss://api.example.com/api/v1"
    assert config.artifact_api_base_url == "https://artifacts.example.com/test-token"
    assert config.skyvern_api_key == "remote-key"
    assert config.browser_streaming_mode == "cdp"
    assert served == [(tmp_path / "runtime", run_commands.UI_PORT, run_commands.ARTIFACT_PORT, "test-token")]
