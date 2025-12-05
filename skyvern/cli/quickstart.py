"""Quickstart command for Skyvern CLI."""

import asyncio
import subprocess

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# Import console after skyvern.cli to ensure proper initialization
from skyvern.cli.console import console
from skyvern.cli.init_command import init_env  # init is used directly
from skyvern.cli.utils import start_services

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
        run_local = init_env(no_postgres=no_postgres)

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
        if run_local:
            start_now = typer.confirm("\nDo you want to start Skyvern services now?", default=True)
            if start_now:
                console.print("\n[bold blue]Starting Skyvern services...[/bold blue]")
                asyncio.run(start_services(server_only=server_only))
            else:
                console.print(
                    "\n[yellow]Skipping service startup. You can start services later with 'skyvern run all'[/yellow]"
                )

    except KeyboardInterrupt:
        console.print("\n[bold yellow]Quickstart process interrupted by user.[/bold yellow]")
        raise typer.Exit(0)
    except Exception as e:
        console.print(f"[bold red]Error during quickstart: {str(e)}[/bold red]")
        raise typer.Exit(1)
