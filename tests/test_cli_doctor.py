from pathlib import Path
import subprocess

import pytest

import skyvern.cli.doctor as doctor_module


def _write_valid_frontend_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "VITE_API_BASE_URL=http://localhost:8000/api/v1",
                "VITE_WSS_BASE_URL=ws://localhost:8000/api/v1",
                "VITE_ARTIFACT_API_BASE_URL=http://localhost:9090",
                "VITE_BROWSER_STREAMING_MODE=cdp",
                "",
            ]
        )
    )


def test_frontend_runtime_env_missing_reports_placeholder_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "skyvern-frontend").mkdir()
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    result = doctor_module._check_frontend_runtime_env()

    assert result.status == "error"
    assert "skyvern-frontend/.env is missing" in result.detail
    assert "Dockerfile placeholders" in result.detail


def test_frontend_runtime_env_detects_placeholder_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    (frontend_dir / ".env").write_text(
        "\n".join(
            [
                "VITE_API_BASE_URL=__VITE_API_BASE_URL_PLACEHOLDER__",
                "VITE_WSS_BASE_URL=ws://localhost:8000/api/v1",
                "VITE_ARTIFACT_API_BASE_URL=http://localhost:9090",
                "VITE_BROWSER_STREAMING_MODE=cdp",
                "",
            ]
        )
    )

    result = doctor_module._check_frontend_runtime_env()

    assert result.status == "error"
    assert "VITE_API_BASE_URL is still __VITE_API_BASE_URL_PLACEHOLDER__" in result.detail


def test_frontend_runtime_env_accepts_valid_host_env_without_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    _write_valid_frontend_env(frontend_dir / ".env")

    result = doctor_module._check_frontend_runtime_env()

    assert result.status == "ok"
    assert "valid Vite runtime URLs" in result.detail


def test_fix_frontend_runtime_env_writes_defaults_and_unquoted_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module, "_docker_compose_available", lambda: False)
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    (tmp_path / ".env").write_text("SKYVERN_API_KEY=sk-test\n")
    (frontend_dir / ".env").write_text("VITE_API_BASE_URL=__VITE_API_BASE_URL_PLACEHOLDER__\n")

    assert doctor_module._fix_frontend_runtime_env() is True

    contents = (frontend_dir / ".env").read_text()
    assert "VITE_API_BASE_URL=http://localhost:8000/api/v1" in contents
    assert "VITE_WSS_BASE_URL=ws://localhost:8000/api/v1" in contents
    assert "VITE_ARTIFACT_API_BASE_URL=http://localhost:9090" in contents
    assert "VITE_BROWSER_STREAMING_MODE=cdp" in contents
    assert "VITE_SKYVERN_API_KEY=sk-test" in contents
    assert "VITE_SKYVERN_API_KEY='sk-test'" not in contents


def test_local_streaming_mode_warns_when_frontend_is_vnc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module, "_docker_compose_available", lambda: False)
    (tmp_path / ".env").write_text("BROWSER_STREAMING_MODE=cdp\n")
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    (frontend_dir / ".env").write_text("VITE_BROWSER_STREAMING_MODE=vnc\n")

    result = doctor_module._check_local_streaming_mode()

    assert result.status == "warn"
    assert "VITE_BROWSER_STREAMING_MODE should be cdp" in result.detail


def test_fix_local_streaming_mode_writes_backend_and_frontend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor_module, "_docker_compose_available", lambda: False)
    frontend_dir = tmp_path / "skyvern-frontend"
    frontend_dir.mkdir()
    (tmp_path / ".env").write_text("BROWSER_STREAMING_MODE=vnc\n")
    (frontend_dir / ".env").write_text("VITE_BROWSER_STREAMING_MODE=vnc\n")

    assert doctor_module._fix_local_streaming_mode() is True

    assert "BROWSER_STREAMING_MODE=cdp" in (tmp_path / ".env").read_text()
    assert "VITE_BROWSER_STREAMING_MODE=cdp" in (frontend_dir / ".env").read_text()


def test_compose_database_connection_can_satisfy_database_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@localhost:5432/skyvern\n"
    )

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'sqlalchemy'",
        )

    monkeypatch.setattr(doctor_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        doctor_module,
        "_check_compose_database_connection",
        lambda: doctor_module.CheckResult(
            name="Database",
            status="ok",
            detail="Docker Compose backend can connect to database",
        ),
    )

    result = doctor_module._check_database()

    assert result.status == "ok"
    assert "Docker Compose backend can connect" in result.detail


def test_docker_local_auth_diagnostic_uses_backend_status_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_script = ""

    def fake_exec(_service: str, script: str, timeout: int = 30):
        nonlocal captured_script
        captured_script = script
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status": "ok", "organization_id": "o_test"}\n',
            stderr="",
        )

    monkeypatch.setattr(doctor_module, "_docker_compose_available", lambda: True)
    monkeypatch.setattr(doctor_module, "_run_docker_compose_exec", fake_exec)

    result = doctor_module._check_docker_local_auth()

    assert result.status == "ok"
    assert "/api/v1/internal/auth/status" in captured_script
    assert "from jose" not in captured_script
    assert "app.DATABASE" not in captured_script


def test_docker_local_auth_reports_backend_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_exec(_service: str, _script: str, timeout: int = 30):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"detail": "Unable to diagnose API key"}\n',
            stderr="",
        )

    monkeypatch.setattr(doctor_module, "_docker_compose_available", lambda: True)
    monkeypatch.setattr(doctor_module, "_run_docker_compose_exec", fake_exec)

    result = doctor_module._check_docker_local_auth()

    assert result.status == "error"
    assert "HTTP error" in result.detail
    assert "Unable to diagnose API key" in result.detail
