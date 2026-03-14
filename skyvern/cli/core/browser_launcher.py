"""Chrome browser launcher for CDP-based browser serve command."""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import psutil

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


def get_local_chrome_profile_dir() -> Path:
    """Return the platform-specific Chrome user data directory."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    elif system == "Windows":
        local_app = Path.home() / "AppData" / "Local"
        return local_app / "Google" / "Chrome" / "User Data"
    else:
        return Path.home() / ".config" / "google-chrome"


_CHROME_MAIN_NAMES = {"chrome", "google-chrome", "google-chrome-stable", "google chrome", "chromium"}
_CHROME_SKIP_NAMES = {"chrome_crashpad_handler", "chromedriver", "chrome helper", "google chrome helper"}


def is_chrome_running() -> bool:
    """Check if Chrome is already running (main browser process only).

    Uses exact name matching to avoid false positives from helper processes
    (renderer, GPU, utility) and unrelated tools like chromedriver.
    """
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info["name"] or "").lower()
            if name in _CHROME_SKIP_NAMES:
                continue
            if name in _CHROME_MAIN_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return False


_PROFILE_COPY_IGNORE = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "Service Worker",
    "blob_storage",
    "BudgetDatabase",
    "coupon_db",
    "Download Service",
    "GCM Store",
    "optimization_guide_model_metadata",
    "optimization_guide_prediction_model_downloads",
    "Extensions",
    "IndexedDB",
    "File System",
    "Session Storage",
}

_LOCK_FILES = {"SingletonLock", "SingletonSocket", "SingletonCookie"}


def clone_local_chrome_profile(chrome_profile_name: str, dest_user_data_dir: Path, *, full: bool = False) -> None:
    """Copy the user's local Chrome profile into *dest_user_data_dir*.

    Args:
        chrome_profile_name: Profile subdirectory name (e.g. ``"Default"``).
        dest_user_data_dir: Destination path that will be used as ``--user-data-dir``.
        full: When ``True``, copy the **entire** Chrome user-data directory
              (requires Chrome to be closed). When ``False`` (default), copy
              only the target profile subdir (skipping caches) plus ``Local State``,
              which is much faster (~50-200 MB vs 2-10+ GB).
    """
    source_user_data_dir = get_local_chrome_profile_dir()
    if not source_user_data_dir.is_dir():
        raise FileNotFoundError(
            f"Chrome user data directory not found at {source_user_data_dir}. Is Google Chrome installed?"
        )

    source_profile = source_user_data_dir / chrome_profile_name
    if not source_profile.resolve().is_relative_to(source_user_data_dir.resolve()):
        raise ValueError(f"Profile name '{chrome_profile_name}' resolves outside the Chrome data directory.")

    if not source_profile.is_dir():
        available = [d.name for d in source_user_data_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        raise FileNotFoundError(
            f"Chrome profile '{chrome_profile_name}' not found in {source_user_data_dir}. "
            f"Available profiles: {', '.join(sorted(available)[:10])}"
        )

    resolved_dest = dest_user_data_dir.resolve()
    skyvern_data = SKYVERN_DATA_DIR.resolve()
    if not resolved_dest.is_relative_to(skyvern_data):
        raise ValueError(
            f"Refusing to overwrite {dest_user_data_dir} — it is outside Skyvern's data directory ({SKYVERN_DATA_DIR}). "
            "Remove it manually or use the default profile directory."
        )

    if dest_user_data_dir.exists():
        shutil.rmtree(dest_user_data_dir)

    if full:
        shutil.copytree(source_user_data_dir, dest_user_data_dir, ignore_dangling_symlinks=True)
    else:
        _selective_copy(source_user_data_dir, source_profile, chrome_profile_name, dest_user_data_dir)

    # Remove lock files so Chrome can open the copied profile
    for lock_name in _LOCK_FILES:
        lock_file = dest_user_data_dir / lock_name
        if lock_file.exists():
            lock_file.unlink()


def _selective_copy(
    source_user_data_dir: Path,
    source_profile: Path,
    chrome_profile_name: str,
    dest_user_data_dir: Path,
) -> None:
    """Copy only auth-relevant files from the Chrome profile."""
    dest_user_data_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy the profile subdir, skipping cache directories
    def _ignore(directory: str, contents: list[str]) -> set[str]:
        return {c for c in contents if c in _PROFILE_COPY_IGNORE}

    shutil.copytree(
        source_profile,
        dest_user_data_dir / chrome_profile_name,
        ignore=_ignore,
        ignore_dangling_symlinks=True,
    )

    # 2. Copy Local State (needed for cookie decryption on macOS/Linux)
    local_state = source_user_data_dir / "Local State"
    if local_state.is_file():
        shutil.copy2(local_state, dest_user_data_dir / "Local State")


async def launch_chrome_with_cdp(
    port: int,
    profile_dir: str | None = None,
    headless: bool = False,
    chrome_path: str | None = None,
    download_dir: str | None = None,
    profile_name: str | None = None,
) -> LocalBrowserInfo:
    """Launch Chrome with CDP enabled and wait for it to be ready.

    Args:
        port: The port for Chrome's CDP server.
        profile_dir: Chrome user data directory. Uses default if not specified.
        headless: Whether to run in headless mode.
        chrome_path: Path to Chrome executable. Auto-detects if not specified.
        download_dir: Directory for downloads. Uses default if not specified.
        profile_name: Chrome profile subdirectory name (e.g. "Profile 1").
            When set, passes ``--profile-directory`` so Chrome loads the
            correct profile instead of defaulting to ``Default/``.

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
        "--hide-crash-restore-bubble",
    ]

    if profile_name:
        args.append(f"--profile-directory={profile_name}")

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
