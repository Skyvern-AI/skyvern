"""ngrok detection, installation, and auth token setup helpers.

Used by ``_maybe_start_ngrok_tunnel`` in ``skyvern.cli.commands.browser`` to
provide an interactive guided setup when ngrok is missing or unconfigured.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import webbrowser

from rich.prompt import Confirm, Prompt

from skyvern.cli.console import console


def detect_ngrok() -> str | None:
    """Return the path to the ngrok binary, or ``None`` if not found."""
    return shutil.which("ngrok")


def detect_os() -> str:
    """Return ``'macos'``, ``'linux'``, or ``'windows'``."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return "windows"


def offer_install_ngrok(*, interactive: bool = True) -> str | None:
    """Interactively offer to install ngrok. Returns the path on success, else ``None``.

    When *interactive* is ``False`` (e.g. ``--tunnel`` in CI), skip prompts
    and just print the error message.
    """
    if not interactive:
        console.print()
        console.print("  [bold red]ngrok not found.[/bold red]")
        console.print("  Install from: [cyan]https://ngrok.com/download[/cyan]")
        console.print("  Then re-run:  [green]skyvern browser serve --tunnel[/green]")
        console.print()
        return None

    os_type = detect_os()

    # macOS: try Homebrew first
    if os_type == "macos" and shutil.which("brew"):
        want = Confirm.ask(
            "[bold red]ngrok not found.[/bold red] Install via Homebrew?",
            default=True,
        )
        if want:
            try:
                console.print("  Installing ngrok via Homebrew...")
                subprocess.run(["brew", "install", "ngrok"], check=True)
                path = shutil.which("ngrok")
                if path:
                    console.print("  [green]ngrok installed successfully.[/green]")
                    return path
            except subprocess.CalledProcessError as exc:
                console.print(f"  [red]Homebrew install failed (exit {exc.returncode}).[/red]")
                # Fall through to download page
        else:
            # User declined brew — skip the redundant "ngrok not found" message
            console.print()
            console.print("  Install manually from: [cyan]https://ngrok.com/download[/cyan]")
            console.print("  Then re-run:  [green]skyvern browser serve --tunnel[/green]")
            console.print()
            want_open = Confirm.ask("Open ngrok download page in browser?", default=True)
            if want_open:
                open_url("https://ngrok.com/download")
            return None

    # Fallback: direct the user to the download page
    console.print()
    console.print("  [bold red]ngrok not found.[/bold red]")
    console.print("  Install from: [cyan]https://ngrok.com/download[/cyan]")
    console.print("  Then re-run:  [green]skyvern browser serve --tunnel[/green]")
    console.print()

    want = Confirm.ask("Open ngrok download page in browser?", default=True)
    if want:
        open_url("https://ngrok.com/download")

    return None


def check_ngrok_auth(ngrok_path: str) -> bool:
    """Return ``True`` if ngrok has a valid configuration with an auth token.

    Note: ``ngrok config check`` validates config file syntax, not the presence
    of an auth token specifically.  Some ngrok versions exit 0 with no token.
    This is a best-effort check — the real validation happens when ngrok tries
    to start a tunnel.
    """
    try:
        result = subprocess.run(
            [ngrok_path, "config", "check"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def offer_setup_auth(ngrok_path: str, *, interactive: bool = True) -> bool:
    """Guide the user through configuring their ngrok auth token. Returns ``True`` on success.

    When *interactive* is ``False`` (e.g. ``--tunnel`` in CI), skip prompts
    and just print the error message.
    """
    console.print()
    console.print("  [bold yellow]ngrok auth token not configured.[/bold yellow]")

    if not interactive:
        console.print("  Run: [green]ngrok config add-authtoken <your-token>[/green]")
        console.print("  Get a token at: [cyan]https://dashboard.ngrok.com/get-started/your-authtoken[/cyan]")
        console.print()
        return False

    console.print()

    want = Confirm.ask("Open ngrok dashboard to get a free auth token?", default=True)
    if want:
        open_url("https://dashboard.ngrok.com/get-started/your-authtoken")

    token = Prompt.ask("Paste your ngrok auth token (or press Enter to skip)", default="")
    if not token.strip():
        return False

    result = subprocess.run(
        [ngrok_path, "config", "add-authtoken", token.strip()],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode == 0:
        console.print("  [green]Auth token configured successfully.[/green]")
        return True

    console.print(f"  [red]Failed to set auth token: {result.stderr.strip()}[/red]")
    return False


def open_url(url: str) -> None:
    """Open *url* in the user's default browser."""
    webbrowser.open(url)
