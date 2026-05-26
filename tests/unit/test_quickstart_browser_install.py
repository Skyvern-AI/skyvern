from __future__ import annotations

import subprocess

import pytest

from skyvern.cli import init_command
from skyvern.cli import quickstart as quickstart_module
from skyvern.cli.init_command import BrowserInstallStatus, InitEnvResult


def test_cdp_mode_skips_playwright_chromium_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        init_command,
        "_is_playwright_chromium_installed",
        lambda: (_ for _ in ()).throw(AssertionError("should not check Chromium")),
    )
    monkeypatch.setattr(
        init_command.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not install Chromium")),
    )

    status = init_command._ensure_playwright_chromium("cdp-connect", skip_browser_install=False)

    assert status.required is False
    assert status.ready is True
    assert status.skipped is True


def test_installed_playwright_chromium_skips_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_command, "_is_playwright_chromium_installed", lambda: True)
    monkeypatch.setattr(
        init_command.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not install Chromium")),
    )

    status = init_command._ensure_playwright_chromium("chromium-headful", skip_browser_install=False)

    assert status.required is True
    assert status.ready is True
    assert status.already_installed is True


def test_playwright_chromium_install_failure_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, bool | None, str | None]] = []

    def fake_capture(event: str, success: bool | None = None, error_message: str | None = None, **_kwargs) -> None:
        events.append((event, success, error_message))

    def fake_run(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["playwright", "install", "chromium"],
            stderr="network failed",
        )

    monkeypatch.setattr(init_command, "_is_playwright_chromium_installed", lambda: False)
    monkeypatch.setattr(init_command, "capture_setup_event", fake_capture)
    monkeypatch.setattr(init_command.subprocess, "run", fake_run)

    status = init_command._ensure_playwright_chromium("chromium-headful", skip_browser_install=False)

    assert status.required is True
    assert status.ready is False
    assert status.attempted is True
    assert status.error == "network failed"
    assert ("playwright-install-fail", False, "network failed") in events


def test_quickstart_does_not_start_services_when_required_browser_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_result = InitEnvResult(
        run_local=True,
        browser_type="chromium-headful",
        browser_install=BrowserInstallStatus(required=True, ready=False, attempted=True, error="network failed"),
    )

    monkeypatch.setattr(quickstart_module, "check_docker_compose_file", lambda: False)
    monkeypatch.setattr(quickstart_module, "_has_server_quickstart_extra", lambda: True)
    monkeypatch.setattr(init_command, "init_env", lambda **_kwargs: init_result)
    monkeypatch.setattr(quickstart_module, "_configure_local_browser_streaming_defaults", lambda: None)
    monkeypatch.setattr(
        quickstart_module.typer,
        "confirm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not prompt to start")),
    )
    monkeypatch.setattr(
        "skyvern.cli.utils.start_services",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not start services")),
    )

    quickstart_module.quickstart(
        ctx=None,  # type: ignore[arg-type]
        no_postgres=False,
        database_string="",
        skip_browser_install=False,
        server_only=False,
        docker_compose=False,
    )
