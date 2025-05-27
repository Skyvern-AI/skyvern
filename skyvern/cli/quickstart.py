"""Quickstart command for Skyvern CLI."""

import asyncio
import subprocess
import sys

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# Import console after skyvern.cli to ensure proper initialization
from skyvern.cli.console import console
from skyvern.cli.init_command import init  # init is used directly

quickstart_app = typer.Typer(help="Quickstart command to set up and run Skyvern with one command.")


def check_docker() -> bool:
    """Check if Docker is installed and running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


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


@quickstart_app.callback(invoke_without_command=True)
def quickstart(
    ctx: typer.Context,
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
    skip_browser_install: bool = typer.Option(
        False, "--skip-browser-install", help="Skip Chromium browser installation"
    ),
    server_only: bool = typer.Option(False, "--server-only", help="Only start the server, not the UI"),
) -> None:
    """Quickstart command to set up and run Skyvern with one command."""
    # Check Docker
    with console.status("Checking Docker installation...") as status:
        if not check_docker():
            console.print(
                Panel(
                    "[bold red]Docker is not installed or not running.[/bold red]\n"
                    "Please install Docker and start it before running quickstart.\n"
                    "Get Docker from: [link]https://www.docker.com/get-started[/link]",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        status.update("✅ Docker is installed and running")

    # Run initialization
    console.print(Panel("[bold green]🚀 Starting Skyvern Quickstart[/bold green]", border_style="green"))

    try:
        # Initialize Skyvern
        console.print("\n[bold blue]Initializing Skyvern...[/bold blue]")
        init(no_postgres=no_postgres)

        # Skip browser installation if requested
        if not skip_browser_install:
            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
            ) as progress:
                progress.add_task("[bold blue]Installing Chromium browser...", total=None)
                try:
                    subprocess.run(["playwright", "install", "chromium"], check=True, capture_output=True, text=True)
                    console.print("✅ [green]Chromium installation complete.[/green]")
                except subprocess.CalledProcessError as e:
                    console.print(f"[yellow]Warning: Failed to install Chromium: {e.stderr}[/yellow]")
        else:
            console.print("⏭️ [yellow]Skipping Chromium installation as requested.[/yellow]")

        # Start services
        console.print("\n[bold blue]Starting Skyvern services...[/bold blue]")
        asyncio.run(start_services(server_only=server_only))

    except KeyboardInterrupt:
        console.print("\n[bold yellow]Quickstart process interrupted by user.[/bold yellow]")
        raise typer.Exit(0)
    except Exception as e:
        console.print(f"[bold red]Error during quickstart: {str(e)}[/bold red]")
        raise typer.Exit(1)
