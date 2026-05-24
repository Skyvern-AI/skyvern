from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_bootstrap_creates_env_and_rewrites_localhost_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = tmp_path / ".env.example"
    example.write_text('KEY=value\nDATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern"\n')
    monkeypatch.chdir(tmp_path)

    quickstart._bootstrap_compose_env_files()

    result = (tmp_path / ".env").read_text()
    assert "localhost" not in result
    assert "KEY=value" in result
    assert 'DATABASE_STRING="postgresql+psycopg://skyvern:skyvern@postgres/skyvern"' in result


def test_bootstrap_creates_env_without_db_string_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = tmp_path / ".env.example"
    example.write_text("KEY=value\n")
    monkeypatch.chdir(tmp_path)

    quickstart._bootstrap_compose_env_files()

    assert (tmp_path / ".env").read_text() == "KEY=value\n"


def test_bootstrap_does_not_overwrite_existing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env.example").write_text("KEY=example\n")
    existing = tmp_path / ".env"
    existing.write_text("KEY=existing\n")
    monkeypatch.chdir(tmp_path)

    quickstart._bootstrap_compose_env_files()

    assert existing.read_text() == "KEY=existing\n"


def test_bootstrap_creates_frontend_env_from_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env.example").write_text("")
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    (frontend_dir / ".env.example").write_text("VITE_KEY=val\n")
    monkeypatch.chdir(tmp_path)

    quickstart._bootstrap_compose_env_files()

    assert (frontend_dir / ".env").read_text() == "VITE_KEY=val\n"


def test_bootstrap_does_not_overwrite_existing_frontend_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env.example").write_text("")
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    (frontend_dir / ".env.example").write_text("VITE_KEY=example\n")
    existing = frontend_dir / ".env"
    existing.write_text("VITE_KEY=existing\n")
    monkeypatch.chdir(tmp_path)

    quickstart._bootstrap_compose_env_files()

    assert existing.read_text() == "VITE_KEY=existing\n"


def test_bootstrap_rewrites_localhost_db_string_when_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_content = 'ENABLE_OPENAI=false\nDATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern"\nENV=local\n'
    (tmp_path / ".env").write_text(env_content)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quickstart.Confirm, "ask", lambda *_args, **_kwargs: True)

    quickstart._bootstrap_compose_env_files()

    result = (tmp_path / ".env").read_text()
    assert "localhost" not in result
    assert 'DATABASE_STRING="postgresql+psycopg://skyvern:skyvern@postgres/skyvern"' in result
    assert "ENABLE_OPENAI=false" in result
    assert "ENV=local" in result


def test_bootstrap_rewrites_export_prefixed_db_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_content = 'export DATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern"\n'
    (tmp_path / ".env").write_text(env_content)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quickstart.Confirm, "ask", lambda *_args, **_kwargs: True)

    quickstart._bootstrap_compose_env_files()

    result = (tmp_path / ".env").read_text()
    assert "localhost" not in result
    assert 'DATABASE_STRING="postgresql+psycopg://skyvern:skyvern@postgres/skyvern"' in result


def test_bootstrap_keeps_localhost_db_string_when_declined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_content = 'DATABASE_STRING="postgresql+psycopg://skyvern@localhost/skyvern"\n'
    (tmp_path / ".env").write_text(env_content)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quickstart.Confirm, "ask", lambda *_args, **_kwargs: False)

    quickstart._bootstrap_compose_env_files()

    assert (tmp_path / ".env").read_text() == env_content


def test_bootstrap_skips_rewrite_for_non_localhost_db_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_content = 'DATABASE_STRING="postgresql+psycopg://skyvern@postgres/skyvern"\n'
    (tmp_path / ".env").write_text(env_content)
    confirm_calls: list[object] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(quickstart.Confirm, "ask", lambda *args, **kwargs: confirm_calls.append(args) or True)

    quickstart._bootstrap_compose_env_files()

    assert confirm_calls == [], "should not prompt when DATABASE_STRING does not point to localhost"
    assert (tmp_path / ".env").read_text() == env_content
