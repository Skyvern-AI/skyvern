"""Chrome browser launcher for CDP-based browser serve command."""

from __future__ import annotations

import asyncio
import platform
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from subprocess import Popen

# Default Skyvern directory for browser data
SKYVERN_DATA_DIR = Path.home() / ".skyvern"


@dataclass
class LocalBrowserInfo:
    """Information about a locally launched browser instance."""

    cdp_ws_url: str
    profile_dir: str
    process: Popen[bytes]


def generate_browser_id() -> str:
    """Generate a unique browser ID for this browser instance."""
    return f"br_{uuid.uuid4().hex[:12]}"


def get_default_chrome_path() -> str:
    """Get the default Chrome executable path based on the platform."""
    system = platform.system()

    paths: list[Path]
    if system == "Darwin":
        # macOS
        paths = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    elif system == "Windows":
        # Windows
        paths = [
            Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ]
    else:
        # Linux
        paths = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/snap/bin/chromium"),
        ]

    for path in paths:
        if path.exists():
            return str(path)

    raise FileNotFoundError(
        f"Chrome executable not found. Searched paths: {paths}. Install Chrome or specify the path with --chrome-path."
    )


def get_default_profile_dir() -> str:
    """Get the default Chrome profile directory."""
    profile_dir = SKYVERN_DATA_DIR / "chrome-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return str(profile_dir)


def get_default_download_dir(browser_id: str) -> str:
    """Get the default download directory for a browser instance."""
    download_dir = SKYVERN_DATA_DIR / "downloads" / browser_id
    download_dir.mkdir(parents=True, exist_ok=True)
    return str(download_dir)


async def launch_chrome_with_cdp(
    port: int,
    profile_dir: str | None = None,
    headless: bool = False,
    chrome_path: str | None = None,
    download_dir: str | None = None,
) -> LocalBrowserInfo:
    """Launch Chrome with CDP enabled and wait for it to be ready.

    Args:
        port: The port for Chrome's CDP server.
        profile_dir: Chrome user data directory. Uses default if not specified.
        headless: Whether to run in headless mode.
        chrome_path: Path to Chrome executable. Auto-detects if not specified.
        download_dir: Directory for downloads. Uses default if not specified.

    Returns:
        LocalBrowserInfo with connection details.

    Raises:
        FileNotFoundError: If Chrome executable is not found.
        TimeoutError: If Chrome fails to start within timeout.
    """
    resolved_chrome_path = chrome_path or get_default_chrome_path()
    resolved_profile_dir = profile_dir or get_default_profile_dir()

    # Build Chrome args
    args = [
        resolved_chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={resolved_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
    ]

    if headless:
        args.append("--headless=new")

    if download_dir:
        # Ensure download directory exists
        Path(download_dir).mkdir(parents=True, exist_ok=True)

    # Launch Chrome process
    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for CDP to be ready
    cdp_ws_url = await _wait_for_cdp_ready(port, timeout=30)

    return LocalBrowserInfo(
        cdp_ws_url=cdp_ws_url,
        profile_dir=resolved_profile_dir,
        process=process,
    )


async def _wait_for_cdp_ready(port: int, timeout: int = 30) -> str:
    """Wait for Chrome's CDP server to be ready and return the WebSocket URL.

    Args:
        port: CDP port to poll.
        timeout: Max seconds to wait.

    Returns:
        CDP WebSocket URL for browser connection.

    Raises:
        TimeoutError: If CDP doesn't become ready within timeout.
    """
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = asyncio.get_event_loop().time() + timeout

    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                response = await client.get(url, timeout=2)
                if response.status_code == 200:
                    data = response.json()
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        return ws_url
            except (httpx.RequestError, httpx.TimeoutException):
                pass

            await asyncio.sleep(0.5)

    raise TimeoutError(f"Chrome CDP did not become ready on port {port} within {timeout}s")


def terminate_browser(browser_info: LocalBrowserInfo) -> None:
    """Terminate the browser process gracefully.

    Args:
        browser_info: The browser info containing the process to terminate.
    """
    process = browser_info.process

    if process.poll() is not None:
        # Process already exited
        return

    # Try graceful termination first
    process.terminate()

    try:
        # Wait up to 5 seconds for graceful shutdown
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # Force kill if it doesn't respond
        process.kill()
        process.wait(timeout=2)
