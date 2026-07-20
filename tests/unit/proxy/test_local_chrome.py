"""Launch-failure taxonomy tests for the local headless-Chrome adapter.

These run without a real browser; the full port contract (needs Chrome) lives in
test_upstream_browser_port_contract.py behind a skipif.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from skyvern.proxy.adapters import local_chrome
from skyvern.proxy.adapters.local_chrome import LocalChromeUpstreamBrowser
from skyvern.proxy.core.errors import LaunchEnvironmentError, LaunchTimeoutError
from skyvern.proxy.core.session import ProxySession


def _session() -> ProxySession:
    return ProxySession(session_id="test-session", upstream_ws_url="ws://localhost:0/unused")


def _fake_chrome(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-chrome"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.asyncio
async def test_no_executable_discovered_raises_launch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_chrome, "find_local_chrome_executable", lambda: None)
    with pytest.raises(LaunchEnvironmentError):
        await LocalChromeUpstreamBrowser().connect(_session())


@pytest.mark.asyncio
async def test_missing_executable_raises_launch_environment(tmp_path: Path) -> None:
    port = LocalChromeUpstreamBrowser(executable_path=str(tmp_path / "no-such-chrome"))
    with pytest.raises(LaunchEnvironmentError):
        await port.connect(_session())


@pytest.mark.asyncio
async def test_early_exit_raises_launch_environment(tmp_path: Path) -> None:
    port = LocalChromeUpstreamBrowser(executable_path=_fake_chrome(tmp_path, "exit 3"))
    with pytest.raises(LaunchEnvironmentError):
        await port.connect(_session())


@pytest.mark.asyncio
async def test_never_ready_process_raises_launch_timeout(tmp_path: Path) -> None:
    port = LocalChromeUpstreamBrowser(
        executable_path=_fake_chrome(tmp_path, "exec sleep 30"), launch_timeout_seconds=0.2
    )
    with pytest.raises(LaunchTimeoutError):
        await port.connect(_session())


@pytest.mark.asyncio
async def test_mkdtemp_failure_raises_launch_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_mkdtemp(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(tempfile, "mkdtemp", failing_mkdtemp)
    port = LocalChromeUpstreamBrowser(executable_path=str(tmp_path / "unused-chrome"))
    with pytest.raises(LaunchEnvironmentError):
        await port.connect(_session())


@pytest.mark.parametrize("port_line", ["notaport", "99999"])
@pytest.mark.asyncio
async def test_garbage_devtools_port_file_raises_launch_environment(tmp_path: Path, port_line: str) -> None:
    body = f'udd="${{3#--user-data-dir=}}"\nprintf \'{port_line}\\n/devtools/browser/x\\n\' > "$udd/DevToolsActivePort"\nexec sleep 30'
    port = LocalChromeUpstreamBrowser(executable_path=_fake_chrome(tmp_path, body), launch_timeout_seconds=5)
    with pytest.raises(LaunchEnvironmentError):
        await port.connect(_session())


@pytest.mark.asyncio
async def test_cancelled_connect_reaps_process_and_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "chrome.pid"
    executable = _fake_chrome(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 30')
    created_dirs: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        created_dirs.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    port = LocalChromeUpstreamBrowser(executable_path=executable, launch_timeout_seconds=30)
    task = asyncio.create_task(port.connect(_session()))
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        pytest.fail("fake chrome never started")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert created_dirs
    assert not Path(created_dirs[0]).exists()
