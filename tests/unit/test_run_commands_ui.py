from __future__ import annotations

import importlib
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
