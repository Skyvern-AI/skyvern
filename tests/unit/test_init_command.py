from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from skyvern.cli import init_command


def test_server_extra_install_target_pins_installed_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_command.importlib.metadata, "version", lambda _package: "1.2.3")

    assert init_command._server_extra_install_target() == "skyvern[server]==1.2.3"


def test_server_extra_install_target_uses_source_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'skyvern'\n", encoding="utf-8")
    (tmp_path / "skyvern").mkdir()
    monkeypatch.chdir(tmp_path)

    assert init_command._server_extra_install_target() == ".[server]"


def test_run_with_server_dependency_install_installs_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    install_calls: list[list[str]] = []

    def action() -> str:
        calls.append("action")
        if len(calls) == 1:
            raise ModuleNotFoundError("No module named fuzzysearch", name="fuzzysearch")
        return "ok"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        install_calls.append(args)
        assert kwargs == {"check": True}
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(init_command, "_SERVER_EXTRA_INSTALL_ATTEMPTED", False)
    monkeypatch.setattr(init_command, "_server_extra_install_target", lambda: "skyvern[server]==1.2.3")
    monkeypatch.setattr(init_command.Confirm, "ask", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(init_command.subprocess, "run", fake_run)
    monkeypatch.setattr(init_command.sys, "executable", "/usr/bin/python")

    assert init_command._run_with_server_dependency_install(action) == "ok"
    assert calls == ["action", "action"]
    assert install_calls == [
        [
            "/usr/bin/python",
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


def test_init_env_wraps_local_org_setup_with_server_dependency_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapped_results: list[Any] = []

    def fake_run_with_server_dependency_install(action: Callable[[], Any]) -> Any:
        result = action()
        wrapped_results.append(result)
        return result

    async def fake_setup_local_organization() -> str:
        return "skyvern-api-key"

    env_updates: dict[str, str] = {}

    def fake_update_or_add_env_var(key: str, value: str, **_kwargs: Any) -> None:
        env_updates[key] = value

    monkeypatch.setenv(init_command.BACKEND_ENV_FILE_ENV_VAR, "")
    monkeypatch.setattr(init_command, "setup_postgresql", lambda *args, **kwargs: None)
    monkeypatch.setattr("skyvern.utils.migrate_db", lambda: None)
    monkeypatch.setattr(init_command, "_setup_local_organization_from_database", fake_setup_local_organization)
    monkeypatch.setattr(init_command, "_run_with_server_dependency_install", fake_run_with_server_dependency_install)
    monkeypatch.setattr(init_command, "update_or_add_env_var", fake_update_or_add_env_var)

    result = init_command.init_env(
        no_postgres=True,
        skip_browser_install=True,
        mode="local",
        skip_llm_setup=True,
        configure_mcp=False,
        browser_type="cdp-connect",
        analytics_id="anonymous",
        env_path=tmp_path / ".env",
        return_result=True,
    )

    assert result.run_local is True
    assert wrapped_results == [None, "skyvern-api-key"]
    assert env_updates["SKYVERN_API_KEY"] == "skyvern-api-key"
