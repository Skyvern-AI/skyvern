import asyncio
import logging
import sys

import typer

from skyvern.cli.console import console
from skyvern.utils.env_paths import resolve_backend_env_path


async def start_services(server_only: bool = False) -> None:
    """Start Skyvern services in the background.

    Args:
        server_only: If True, only start the server, not the UI.
    """
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

        console.print("\n🎉 [bold green]Skyvern is now running![/bold green]")
        console.print("🌐 [bold]Access the UI at:[/bold] [cyan]http://localhost:8080[/cyan]")
        console.print(f"🔑 [bold]Your API key is in {resolve_backend_env_path()} as SKYVERN_API_KEY[/bold]")

        # Wait for processes to complete (they won't unless killed)
        if not server_only:
            await asyncio.gather(server_process.wait(), ui_process.wait())
        else:
            await server_process.wait()

    except Exception as e:
        console.print(f"[bold red]Error starting services: {str(e)}[/bold red]")
        logging.error("Startup failed", exc_info=True)
        raise typer.Exit(1)
