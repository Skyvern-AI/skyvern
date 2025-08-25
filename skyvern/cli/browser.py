import os
import subprocess
from typing import Optional
from urllib.parse import urlparse

import requests  # type: ignore
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from skyvern.utils import detect_os

from .console import console


def get_default_chrome_location(host_system: str) -> str:
    """Get the default Chrome/Chromium location based on OS."""
    if host_system == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if host_system == "linux":
        chrome_paths = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]
        for path in chrome_paths:
            if os.path.exists(path):
                return path
        return "/usr/bin/google-chrome"
    if host_system == "wsl":
        return "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe"
    return "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"


def setup_browser_config() -> tuple[str, Optional[str], Optional[str]]:
    """Configure browser settings for Skyvern."""
    console.print(Panel("\n[bold blue]Configuring web browser for scraping...[/bold blue]", border_style="cyan"))
    browser_types = ["chromium-headless", "chromium-headful", "cdp-connect"]

    for i, browser_type in enumerate(browser_types, 1):
        console.print(f"[cyan]{i}.[/cyan] [bold]{browser_type}[/bold]")
        if browser_type == "chromium-headless":
            console.print("   - Runs Chrome in [italic]headless[/italic] mode (no visible window)")
        elif browser_type == "chromium-headful":
            console.print("   - Runs Chrome with [italic]visible window[/italic]")
        elif browser_type == "cdp-connect":
            console.print("   - Connects to an [italic]existing Chrome instance[/italic]")
            console.print("   - [yellow]Requires Chrome to be running with remote debugging enabled[/yellow]")

    selected_browser_idx = Prompt.ask(
        "\nChoose browser type", choices=[str(i) for i in range(1, len(browser_types) + 1)]
    )
    selected_browser = browser_types[int(selected_browser_idx) - 1]
    console.print(f"Selected browser: [bold green]{selected_browser}[/bold green]")

    browser_location = None
    remote_debugging_url = None

    if selected_browser == "cdp-connect":
        host_system = detect_os()
        default_location = get_default_chrome_location(host_system)
        console.print(f"\n[italic]Default Chrome location for your system:[/italic] [cyan]{default_location}[/cyan]")
        browser_location = Prompt.ask(
            "Enter Chrome executable location (press Enter to use default)", default=default_location
        )
        if not browser_location:
            browser_location = default_location

        if not os.path.exists(browser_location):
            console.print(
                f"[yellow]Warning: Chrome not found at {browser_location}. Please verify the location is correct.[/yellow]"
            )

        console.print("\n[bold]To use CDP connection, Chrome must be running with remote debugging enabled.[/bold]")
        console.print("Example: [code]chrome --remote-debugging-port=9222[/code]")
        console.print("[italic]Default debugging URL: [cyan]http://localhost:9222[/cyan][/italic]")

        default_port = "9222"
        if remote_debugging_url is None:
            remote_debugging_url = "http://localhost:9222"
        elif urlparse(remote_debugging_url).port is not None:
            default_port = remote_debugging_url.split(":")[-1].split("/")[0]

        parsed_url = urlparse(remote_debugging_url)
        version_url = f"{parsed_url.scheme}://{parsed_url.netloc}/json/version"

        with console.status(
            f"[bold green]Checking if Chrome is already running with remote debugging on port {default_port}..."
        ) as status:
            try:
                response = requests.get(version_url, timeout=2)
                if response.status_code == 200:
                    try:
                        browser_info = response.json()
                        console.print("✅ [green]Chrome is already running with remote debugging![/green]")
                        if "Browser" in browser_info:
                            console.print(f"  Browser: [bold]{browser_info['Browser']}[/bold]")
                        if "webSocketDebuggerUrl" in browser_info:
                            console.print(f"  WebSocket URL: [link]{browser_info['webSocketDebuggerUrl']}[/link]")
                        console.print(f"  Connected to [link]{remote_debugging_url}[/link]")
                        return selected_browser, browser_location, remote_debugging_url
                    except ValueError:
                        console.print(
                            "[yellow]Port is in use, but doesn't appear to be Chrome with remote debugging.[/yellow]"
                        )
                else:
                    console.print(f"[yellow]Chrome responded with status code {response.status_code}.[/yellow]")
            except requests.RequestException:
                console.print(f"[red]No Chrome instance detected on {remote_debugging_url}[/red]")
        status.stop()

        console.print("\n[bold]Executing Chrome with remote debugging enabled:[/bold]")

        if host_system == "darwin" or host_system == "linux":
            chrome_cmd = f'{browser_location} --remote-debugging-port={default_port} --user-data-dir="$HOME/chrome-cdp-profile" --no-first-run --no-default-browser-check'
            console.print(f"    [code]{chrome_cmd}[/code]")
        elif host_system == "windows" or host_system == "wsl":
            chrome_cmd = f'"{browser_location}" --remote-debugging-port={default_port} --user-data-dir="C:\\chrome-cdp-profile" --no-first-run --no-default-browser-check'
            console.print(f"    [code]{chrome_cmd}[/code]")
        else:
            console.print("[red]Unsupported OS for Chrome configuration. Please set it up manually.[/red]")

        execute_browser = Confirm.ask("\nWould you like to start Chrome with remote debugging now?")
        if execute_browser:
            console.print(
                f"🚀 [bold green]Starting Chrome with remote debugging on port {default_port}...\n[/bold green]"
            )
            try:
                if host_system in ["darwin", "linux"]:
                    subprocess.Popen(f"nohup {chrome_cmd} > /dev/null 2>&1 &", shell=True)
                elif host_system == "windows":
                    subprocess.Popen(f"start {chrome_cmd}", shell=True)
                elif host_system == "wsl":
                    subprocess.Popen(f"cmd.exe /c start {chrome_cmd}", shell=True)
            except Exception as e:  # pragma: no cover - CLI safeguards
                console.print(f"[red]Error starting Chrome: {e}[/red]")
                console.print("[italic]Please start Chrome manually using the command above.[/italic]")

        remote_debugging_url = Prompt.ask(
            "Enter remote debugging URL (press Enter for default)", default="http://localhost:9222"
        )
        if not remote_debugging_url:
            remote_debugging_url = "http://localhost:9222"

    return selected_browser, browser_location, remote_debugging_url
