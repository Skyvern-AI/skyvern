from __future__ import annotations

import subprocess

import pytest
import typer

from skyvern.cli import init_command, quickstart


def test_running_skyvern_compose_services_filters_expected_services(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.CompletedProcess(
        ["docker", "compose", "ps"],
        0,
        stdout="postgres\nskyvern\nskyvern-ui\nunrelated\n",
        stderr="",
    )
    monkeypatch.setattr(quickstart, "_run_docker_command", lambda _args: result)

    assert quickstart._running_skyvern_compose_services() == ["postgres", "skyvern", "skyvern-ui"]


def test_handle_running_compose_stack_runs_down_when_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(quickstart, "_running_skyvern_compose_services", lambda: ["skyvern", "skyvern-ui"])
    monkeypatch.setattr(quickstart.Confirm, "ask", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(quickstart.subprocess, "run", fake_run)
    monkeypatch.setattr(quickstart, "capture_setup_event", lambda *_args, **_kwargs: None)

    quickstart._handle_running_compose_stack()

    assert calls == [["docker", "compose", "down"]]


def test_handle_postgres_container_conflict_removes_when_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(quickstart, "get_postgres_container_state", lambda: "running")
    monkeypatch.setattr(quickstart.Confirm, "ask", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(quickstart.subprocess, "run", fake_run)
    monkeypatch.setattr(quickstart, "capture_setup_event", lambda *_args, **_kwargs: None)

    quickstart._handle_postgres_container_conflict()

    assert calls == [["docker", "rm", "-f", "postgresql-container"]]


def test_quickstart_reraises_intentional_typer_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_errors: list[object] = []

    monkeypatch.setattr(
        init_command,
        "init_env",
        lambda **_kwargs: (_ for _ in ()).throw(typer.Exit(1)),
    )
    monkeypatch.setattr(quickstart, "capture_setup_error", lambda *args, **_kwargs: capture_errors.append(args))

    with pytest.raises(typer.Exit):
        quickstart._run_server_quickstart(
            no_postgres=False,
            database_string="",
            skip_browser_install=False,
            server_only=False,
        )

    assert capture_errors == []
