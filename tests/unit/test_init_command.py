from __future__ import annotations

import subprocess
from pathlib import Path

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
    assert install_calls == [["/usr/bin/python", "-m", "pip", "install", "skyvern[server]==1.2.3"]]
