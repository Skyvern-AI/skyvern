import asyncio
import sys
import subprocess
import typer

from skyvern.cli.console import console

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
        console.print("🔑 [bold]Your API key is in your .env file as SKYVERN_API_KEY[/bold]")

        # Wait for processes to complete (they won't unless killed)
        if not server_only:
            await asyncio.gather(server_process.wait(), ui_process.wait())
        else:
            await server_process.wait()

    except Exception as e:
        console.print(f"[bold red]Error starting services: {str(e)}[/bold red]")
        raise typer.Exit(1)


def start_services_sync(server_only: bool = False) -> None:
    """
    Start Skyvern services (server and optionally UI) using synchronous subprocesses.
    This does not require any event loop policy changes and is fully compatible with psycopg.
    """

    try:
        # Start server
        server_proc = subprocess.Popen([sys.executable, "-m", "skyvern.cli.commands", "run", "server"])
        console.print("[green]Skyvern API server started.[/green]")

        if not server_only:
            # Start UI
            ui_proc = subprocess.Popen([sys.executable, "-m", "skyvern.cli.commands", "run", "ui"])
            console.print("[green]Skyvern UI started.[/green]")

        console.print("\n🎉 [bold green]Skyvern is now running![/bold green]")
        console.print("🌐 [bold]Access the UI at:[/bold] [cyan]http://localhost:8080[/cyan]")
        console.print("🔑 [bold]Your API key is in your .env file as SKYVERN_API_KEY[/bold]")

        # Wait for processes to complete (they won't unless killed)
        if not server_only:
            server_proc.wait()
            ui_proc.wait()
        else:
            server_proc.wait()

    except Exception as e:
        console.print(f"[bold red]Error starting services: {str(e)}[/bold red]")
        raise


def set_asyncio_event_loop_policy():
    """
    Set the event loop policy to WindowsSelectorEventLoopPolicy if running on Windows.
    This should be called at the top of any entry point that needs psycopg compatibility.
    """
    if sys.platform == "win32" and asyncio.get_event_loop_policy() != asyncio.WindowsProactorEventLoopPolicy():
        print("Updating event loop policy for Windows")
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
