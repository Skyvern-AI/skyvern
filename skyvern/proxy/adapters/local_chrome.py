"""Local headless-Chrome upstream adapter for dev and CI.

Launches a throwaway headless Chrome per connection with --remote-debugging-port=0,
discovers the browser websocket endpoint from DevToolsActivePort, and dials it with
the generic WebSocket adapter so both share one dial-and-classify path. Ignores
session.upstream_ws_url — each connection gets its own browser.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import structlog

from skyvern.proxy.adapters.websocket_upstream import WebSocketUpstreamBrowser
from skyvern.proxy.core.errors import LaunchEnvironmentError, LaunchTimeoutError
from skyvern.proxy.core.session import ProxySession
from skyvern.proxy.ports import UpstreamConnection

LOG = structlog.get_logger(__name__)

_EXECUTABLE_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
_DEVTOOLS_PORT_FILE = "DevToolsActivePort"
_POLL_INTERVAL_SECONDS = 0.05
_TERMINATE_GRACE_SECONDS = 5.0


def find_local_chrome_executable() -> str | None:
    # Env var directly, not skyvern.config: the proxy image ships zero runner deps.
    if configured := os.environ.get("CHROME_EXECUTABLE_PATH"):
        return configured
    for candidate in _EXECUTABLE_CANDIDATES:
        if os.path.sep in candidate:
            if os.path.exists(candidate):
                return candidate
        elif resolved := shutil.which(candidate):
            return resolved
    return None


def _read_devtools_endpoint(user_data_dir: Path) -> tuple[int, str] | None:
    """None while the port file is absent or still being written; raises
    LaunchEnvironmentError when its contents are garbage."""
    try:
        lines = (user_data_dir / _DEVTOOLS_PORT_FILE).read_text().splitlines()
    except OSError:
        return None
    except UnicodeDecodeError as exc:
        raise LaunchEnvironmentError("devtools port file is not valid UTF-8") from exc
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0])
    except ValueError as exc:
        raise LaunchEnvironmentError("devtools port file did not contain a port number") from exc
    if not 1 <= port <= 65535:
        raise LaunchEnvironmentError(f"devtools port file contains an out-of-range port ({port})")
    return port, lines[1]


# Accepted gap: SIGKILL to the parent cannot cascade to Chrome's renderer/zygote
# children; dev/CI-only adapter, so stray children are bounded by the sandbox lifetime.
async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


class LocalChromeUpstreamConnection:
    def __init__(
        self, connection: UpstreamConnection, process: asyncio.subprocess.Process, user_data_dir: Path
    ) -> None:
        self._connection = connection
        self._process = process
        self._user_data_dir = user_data_dir

    async def send(self, raw: str) -> None:
        await self._connection.send(raw)

    async def receive(self) -> str:
        return await self._connection.receive()

    async def close(self) -> None:
        try:
            await self._connection.close()
        finally:
            try:
                await _terminate_process(self._process)
            finally:
                shutil.rmtree(self._user_data_dir, ignore_errors=True)


class LocalChromeUpstreamBrowser:
    def __init__(
        self,
        executable_path: str | None = None,
        launch_timeout_seconds: float = 30.0,
    ) -> None:
        self._executable_path = executable_path
        self._launch_timeout_seconds = launch_timeout_seconds
        self._dialer = WebSocketUpstreamBrowser()

    async def connect(self, session: ProxySession) -> UpstreamConnection:
        executable = self._executable_path or find_local_chrome_executable()
        if executable is None:
            raise LaunchEnvironmentError("no Chrome/Chromium executable found; set CHROME_EXECUTABLE_PATH")
        try:
            user_data_dir = Path(tempfile.mkdtemp(prefix="skyvern-cdp-proxy-chrome-"))
        except OSError as exc:
            raise LaunchEnvironmentError(
                f"failed to create a browser profile directory ({type(exc).__name__})"
            ) from exc
        process: asyncio.subprocess.Process | None = None
        try:
            process = await self._launch(executable, user_data_dir)
            port, browser_path = await self._wait_for_devtools_endpoint(process, user_data_dir)
            connection = await self._dialer.dial(f"ws://127.0.0.1:{port}{browser_path}")
            return LocalChromeUpstreamConnection(connection, process, user_data_dir)
        except BaseException:
            # BaseException: a cancelled connect must still reap the process and profile dir.
            try:
                if process is not None:
                    await _terminate_process(process)
            finally:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            raise

    async def _launch(self, executable: str, user_data_dir: Path) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                executable,
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                # Dev/CI-only adapter: containerized CI has no user namespaces for the sandbox.
                "--no-sandbox",
                "--disable-gpu",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LaunchEnvironmentError(f"failed to launch local browser ({type(exc).__name__})") from exc

    async def _wait_for_devtools_endpoint(
        self, process: asyncio.subprocess.Process, user_data_dir: Path
    ) -> tuple[int, str]:
        deadline = asyncio.get_running_loop().time() + self._launch_timeout_seconds
        while True:
            if process.returncode is not None:
                raise LaunchEnvironmentError(
                    f"local browser exited with code {process.returncode} before its devtools endpoint became ready"
                )
            endpoint = _read_devtools_endpoint(user_data_dir)
            if endpoint is not None:
                return endpoint
            if asyncio.get_running_loop().time() >= deadline:
                raise LaunchTimeoutError(
                    f"local browser did not expose a devtools endpoint within {self._launch_timeout_seconds}s"
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
