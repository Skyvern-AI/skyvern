import json
import platform
import time
from typing import Optional
from urllib.parse import urlparse

import requests  # type: ignore
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from skyvern.analytics import capture_setup_event

from .console import console
from .core.browser_launcher import (
    SKYVERN_DATA_DIR,
    clone_local_chrome_profile,
    get_local_chrome_profile_dir,
)

# Ports to scan when auto-discovering a CDP debugging server
_CDP_SCAN_PORTS = [9222, 9223, 9224, 9225, 9226, 9229]


def _check_cdp_ws(port: int) -> Optional[dict]:
    """Try a WebSocket CDP handshake on the given port.

    Some CDP-compatible servers expose a WebSocket endpoint without the
    /json/version HTTP discovery endpoint. This sends Browser.getVersion over
    WS to detect those endpoints.

    Returns the version info dict if successful, else None.
    """
    # Lazy import — websockets is only needed for CDP WS probing and may not
    # be installed in all environments.
    try:
        import websockets.sync.client as ws_sync
    except ImportError:
        return None

    url = f"ws://127.0.0.1:{port}/devtools/browser"
    try:
        with ws_sync.connect(url, close_timeout=2, open_timeout=1) as ws:
            ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            raw = ws.recv(timeout=3)
            data = json.loads(raw)
            result = data.get("result", {})
            if result.get("product"):
                return result
    except Exception:
        pass
    return None


def _discover_cdp_server() -> Optional[tuple[str, dict | None]]:
    """Scan common ports for a running Chrome CDP server.

    Tries the HTTP /json/version endpoint first (standard CDP), then falls back
    to a WebSocket probe (chrome://inspect WS-only mode).

    Returns (url, version_info) where url is suitable for Playwright connect_over_cdp:
    - "http://127.0.0.1:{port}" if HTTP API is available
    - "ws://127.0.0.1:{port}/devtools/browser" if only WS is available
    version_info is cached to avoid a redundant probe in _print_cdp_info.
    """
    for port in _CDP_SCAN_PORTS:
        http_url = f"http://127.0.0.1:{port}"
        # Try HTTP first (standard --remote-debugging-port)
        try:
            response = requests.get(f"{http_url}/json/version", timeout=1)
            if response.status_code == 200:
                data = response.json()
                if "webSocketDebuggerUrl" in data or "Browser" in data:
                    return http_url, data
        except (requests.RequestException, ValueError):
            pass

        # Try WS probe (chrome://inspect WS-only mode)
        ws_info = _check_cdp_ws(port)
        if ws_info:
            return f"ws://127.0.0.1:{port}/devtools/browser", ws_info

    return None


def _print_cdp_info(url: str, cached_info: dict | None = None) -> None:
    """Print details about a discovered CDP server."""
    if url.startswith("ws://"):
        info = cached_info
        if not info:
            parsed = urlparse(url)
            port = parsed.port
            if not port:
                return
            info = _check_cdp_ws(port)
        if info:
            console.print(f"  Browser: [bold]{info.get('product', 'Unknown')}[/bold]")
            console.print(f"  WebSocket URL: [dim]{url}[/dim]")
        return

    # Standard HTTP CDP server
    info = cached_info
    if not info:
        try:
            response = requests.get(f"{url}/json/version", timeout=2)
            if response.status_code == 200:
                info = response.json()
        except (requests.RequestException, ValueError):
            pass
    if info:
        if "Browser" in info:
            console.print(f"  Browser: [bold]{info['Browser']}[/bold]")
        if "webSocketDebuggerUrl" in info:
            console.print(f"  WebSocket URL: [dim]{info['webSocketDebuggerUrl']}[/dim]")


def _classic_cdp_launch_command() -> str:
    system = platform.system()
    if system == "Darwin":
        return (
            "/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\\n"
            "  --remote-debugging-port=9222 \\\n"
            "  --remote-debugging-address=0.0.0.0 \\\n"
            '  --user-data-dir="$HOME/skyvern-chrome-cdp" \\\n'
            "  --no-first-run \\\n"
            "  --no-default-browser-check"
        )
    if system == "Windows":
        return (
            "taskkill /F /IM chrome.exe\n\n"
            '& "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `\n'
            "  --remote-debugging-port=9222 `\n"
            "  --remote-debugging-address=0.0.0.0 `\n"
            '  --user-data-dir="$env:TEMP\\skyvern-chrome-cdp" `\n'
            "  --no-first-run `\n"
            "  --no-default-browser-check"
        )
    return (
        "google-chrome \\\n"
        "  --remote-debugging-port=9222 \\\n"
        "  --remote-debugging-address=0.0.0.0 \\\n"
        '  --user-data-dir="$HOME/skyvern-chrome-cdp" \\\n'
        "  --no-first-run \\\n"
        "  --no-default-browser-check"
    )


def _print_classic_cdp_instructions() -> None:
    console.print(
        Panel(
            "[bold]Enable Chrome remote debugging[/bold]\n\n"
            "1. Open [cyan]chrome://inspect/#remote-debugging[/cyan] in Chrome\n"
            "2. Turn on [bold]Allow remote debugging for this browser instance[/bold]\n"
            "3. Confirm Chrome shows [green]Server running at: 127.0.0.1:9222[/green]\n\n"
            "If that page is unavailable in your Chrome version, start Chrome manually:\n\n"
            f"[cyan]{_classic_cdp_launch_command()}[/cyan]",
            border_style="cyan",
        )
    )


def _setup_local_browser_clone() -> tuple[str, Optional[str], Optional[str]]:
    """Set up a new browser with the user's Chrome profile cloned."""
    chrome_profile_dir = get_local_chrome_profile_dir()
    if not chrome_profile_dir.is_dir():
        console.print(
            f"[red]Chrome profile directory not found at {chrome_profile_dir}. Is Google Chrome installed?[/red]"
        )
        capture_setup_event(
            "browser-clone-profile",
            success=False,
            error_type="profile_not_found",
            error_message=f"Chrome profile dir not found at {chrome_profile_dir}",
        )
        return "chromium-headful", None, None

    # List available profiles
    profiles = sorted(
        [d.name for d in chrome_profile_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
    )
    # Filter to actual Chrome profile directories (contain a Preferences file)
    profile_dirs = [p for p in profiles if (chrome_profile_dir / p / "Preferences").exists()]
    if not profile_dirs:
        # Fallback to name-based heuristic
        profile_dirs = [p for p in profiles if p == "Default" or p.startswith("Profile ")]
    if not profile_dirs:
        console.print("[red]No Chrome profiles found. Falling back to a fresh browser profile.[/red]")
        return "chromium-headful", None, None

    if len(profile_dirs) == 1:
        chosen_profile = profile_dirs[0]
        console.print(f"  Found Chrome profile: [bold]{chosen_profile}[/bold]")
    else:
        console.print("\n[bold]Available Chrome profiles:[/bold]")
        for i, p in enumerate(profile_dirs, 1):
            console.print(f"  [cyan]{i}.[/cyan] {p}")
        idx = Prompt.ask(
            "Choose a profile to clone",
            choices=[str(i) for i in range(1, len(profile_dirs) + 1)],
            default="1",
        )
        chosen_profile = profile_dirs[int(idx) - 1]

    dest = SKYVERN_DATA_DIR / "chrome-profile"
    console.print(f"\n  Cloning [bold]{chosen_profile}[/bold] profile to [dim]{dest}[/dim]...")

    with console.status("[bold green]Copying profile (this may take a moment)..."):
        try:
            clone_local_chrome_profile(chosen_profile, dest, full=False)
            capture_setup_event(
                "browser-clone-profile",
                success=True,
                extra_data={"profile": chosen_profile, "dest": str(dest)},
            )
            console.print("  ✅ [green]Profile cloned successfully.[/green]")
            console.print("  [dim]Your cookies, logins, and extensions from this profile will be available.[/dim]")
        except Exception as e:
            capture_setup_event(
                "browser-clone-profile",
                success=False,
                error_type="clone_failed",
                error_message=str(e),
            )
            console.print(f"  [red]Failed to clone profile: {e}[/red]")
            use_fresh = Confirm.ask(
                "Continue with a fresh browser profile instead?",
                default=True,
            )
            if not use_fresh:
                console.print("[yellow]Browser setup cancelled. Please fix the issue and try again.[/yellow]")
                raise SystemExit(1)

    return "chromium-headful", None, None


def _setup_local_browser_actual() -> tuple[str, Optional[str], Optional[str]]:
    """Connect to the user's actual running Chrome browser via CDP."""
    # Step 1: Check if debugging is already enabled
    console.print("\n  Checking for an existing remote debugging server...")
    result = _discover_cdp_server()
    if result:
        existing, info = result
        console.print(f"  ✅ [green]Found Chrome debugging server at {existing}[/green]")
        _print_cdp_info(existing, cached_info=info)
        capture_setup_event(
            "browser-actual-connect",
            success=True,
            extra_data={"url": existing, "method": "auto-discovered"},
        )
        return "cdp-connect", None, existing

    # Step 2: Guide the user to enable Chrome remote debugging.
    _print_classic_cdp_instructions()

    console.print("\n[bold yellow]Enable remote debugging in Chrome, then press Enter to continue...[/bold yellow]")
    Prompt.ask("Press Enter when ready", default="")

    # Step 3: Auto-discover the debugging server with retries
    console.print()
    with console.status("[bold green]Scanning for Chrome debugging server...") as status:
        for attempt in range(6):
            result = _discover_cdp_server()
            if result:
                found, info = result
                status.stop()
                console.print(f"  ✅ [green]Found Chrome debugging server at {found}[/green]")
                _print_cdp_info(found, cached_info=info)
                capture_setup_event(
                    "browser-actual-connect",
                    success=True,
                    extra_data={"url": found, "method": "user-enabled", "attempts": attempt + 1},
                )
                return "cdp-connect", None, found
            time.sleep(1)
        status.stop()

    # Step 4: Fallback — ask for manual URL
    console.print("[yellow]Could not auto-detect the debugging server.[/yellow]")
    console.print("[dim]Make sure /json/version returns JSON from the URL you enter.[/dim]")
    manual_url = Prompt.ask(
        "Enter the debugging URL manually (e.g. http://127.0.0.1:9222)",
        default="http://127.0.0.1:9222",
    )

    # Verify the manual URL
    try:
        response = requests.get(f"{manual_url}/json/version", timeout=2)
        if response.status_code == 200:
            console.print(f"  ✅ [green]Connected to {manual_url}[/green]")
            _print_cdp_info(manual_url)
            capture_setup_event(
                "browser-actual-connect",
                success=True,
                extra_data={"url": manual_url, "method": "manual"},
            )
        else:
            console.print(f"[yellow]Warning: Server responded with status {response.status_code}[/yellow]")
            capture_setup_event(
                "browser-actual-connect",
                success=False,
                error_type="bad_status",
                error_message=f"Status {response.status_code}",
            )
    except requests.RequestException:
        console.print(f"[yellow]Warning: Could not connect to {manual_url}. Make sure debugging is enabled.[/yellow]")
        capture_setup_event(
            "browser-actual-connect",
            success=False,
            error_type="connection_failed",
            error_message=f"Could not connect to {manual_url}",
        )

    return "cdp-connect", None, manual_url


def setup_browser_config() -> tuple[str, Optional[str], Optional[str]]:
    """Configure browser settings for Skyvern.

    Returns:
        (browser_type, browser_location, remote_debugging_url)
    """
    console.print(Panel("[bold blue]Configuring web browser...[/bold blue]", border_style="cyan"))

    console.print("[cyan]1.[/cyan] [bold]Local browser[/bold]")
    console.print("   - Use your existing Chrome with your cookies, logins, and extensions")
    console.print("[cyan]2.[/cyan] [bold]New browser (headful)[/bold]")
    console.print("   - Launch a fresh Chrome window (visible)")
    console.print("[cyan]3.[/cyan] [bold]New browser (headless)[/bold]")
    console.print("   - Launch Chrome in the background (no visible window)")

    selected_idx = Prompt.ask("\nChoose browser type", choices=["1", "2", "3"])

    if selected_idx == "2":
        console.print("Selected: [bold green]New browser (headful)[/bold green]")
        capture_setup_event("browser-config-select", success=True, extra_data={"type": "chromium-headful"})
        return "chromium-headful", None, None

    if selected_idx == "3":
        console.print("Selected: [bold green]New browser (headless)[/bold green]")
        capture_setup_event("browser-config-select", success=True, extra_data={"type": "chromium-headless"})
        return "chromium-headless", None, None

    # Local browser selected
    console.print("Selected: [bold green]Local browser[/bold green]")
    capture_setup_event("browser-config-select", success=True, extra_data={"type": "local-browser"})

    console.print("\n[cyan]a.[/cyan] [bold]Clone Chrome profile[/bold]")
    console.print("   - Copy your cookies and logins into a separate browser (Chrome can stay open)")
    console.print("[cyan]b.[/cyan] [bold]Use actual browser[/bold]")
    console.print("   - Connect directly to your running Chrome instance")

    local_choice = Prompt.ask("\nChoose local browser mode", choices=["a", "b"])

    if local_choice == "a":
        return _setup_local_browser_clone()

    return _setup_local_browser_actual()
