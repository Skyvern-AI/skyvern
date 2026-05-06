import asyncio
import logging
import socket
import sys
import time

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn

from skyvern.analytics import capture_setup_error, capture_setup_event
from skyvern.cli.console import console
from skyvern.utils.env_paths import resolve_backend_env_path


def wait_for_docker_services(ui_port: int = 8080, api_port: int = 8000, timeout: int = 120) -> bool:
    """Poll until Docker Compose services are reachable. Returns True if ready."""
    start = time.monotonic()
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
    ) as progress:
        progress.add_task("[bold blue]Waiting for services to start (this may take a minute)...", total=None)
        while time.monotonic() - start < timeout:
            api_up = _port_open(api_port)
            ui_up = _port_open(ui_port)
            if api_up and ui_up:
                return True
            time.sleep(3)
    return False


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect((host, port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


async def start_services(server_only: bool = False) -> None:
    """Start Skyvern services in the background.

    Args:
        server_only: If True, only start the server, not the UI.
    """
    capture_setup_event("services-start", extra_data={"server_only": server_only})
    try:
        # Start server in the background
        server_process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "skyvern.cli.commands", "run", "server"
        )

        # Give server a moment to start
        await asyncio.sleep(2)

        if not server_only:
            # Start UI in the background
            ui_process = await asyncio.create_subprocess_exec(sys.executable, "-m", "skyvern.cli.commands", "run", "ui")

        capture_setup_event("services-running", success=True, extra_data={"server_only": server_only})
        console.print("\n🎉 [bold green]Skyvern is now running![/bold green]")
        console.print("🌐 [bold]Access the UI at:[/bold] [cyan]http://localhost:8080[/cyan]")
        console.print(f"🔑 [bold]Your API key is in {resolve_backend_env_path()} as SKYVERN_API_KEY[/bold]")

        # Wait for processes to complete (they won't unless killed)
        if not server_only:
            await asyncio.gather(server_process.wait(), ui_process.wait())
        else:
            await server_process.wait()

    except Exception as e:
        capture_setup_error("services-start-fail", e, error_type="service_startup_error")
        console.print(f"[bold red]Error starting services: {str(e)}[/bold red]")
        logging.error("Startup failed", exc_info=True)
        raise typer.Exit(1)
