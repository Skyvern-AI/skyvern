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


def run_cmd(cmd: list[str]) -> bool:
    """Run command and return success status."""
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
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
    # If a subcommand was invoked, don't run the main quickstart logic
    if ctx.invoked_subcommand is not None:
        return

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


@quickstart_app.command("contributors")
def contributors_setup(
    skip_run: bool = typer.Option(False, "--skip-run", help="Skip running pre-commit on all files"),
) -> None:
    """Set up development environment for contributors with pre-commit hooks."""
    console.print(Panel("[bold green]🚀 Setting up Skyvern Contributor Environment[/bold green]", border_style="green"))

    try:
        # Install pre-commit if needed
        if not run_cmd(["pre-commit", "--version"]):
            console.print("📦 [yellow]Installing pre-commit...[/yellow]")
            if not run_cmd([sys.executable, "-m", "pip", "install", "pre-commit"]):
                console.print(
                    "[bold red]Failed to install pre-commit. Please install manually: pip install pre-commit[/bold red]"
                )
                raise typer.Exit(1)
            console.print("✅ [green]pre-commit installed![/green]")
        else:
            console.print("✅ [green]pre-commit is already installed[/green]")

        # Fix git hooks path if needed
        check_result = subprocess.run(["git", "config", "--get-all", "core.hooksPath"], capture_output=True)
        if check_result.returncode == 0:
            console.print("🔧 [yellow]Removing conflicting git hooksPath configuration...[/yellow]")
            if not run_cmd(["git", "config", "--unset-all", "core.hooksPath"]):
                console.print("[bold red]Failed to fix git configuration.[/bold red]")
                raise typer.Exit(1)
            console.print("✅ [green]Git hooksPath configuration cleared![/green]")

        # Install pre-commit hooks
        console.print("🔧 [yellow]Installing pre-commit hooks...[/yellow]")
        if not run_cmd(["pre-commit", "install"]):
            console.print("[bold red]Failed to install pre-commit hooks.[/bold red]")
            raise typer.Exit(1)
        console.print("✅ [green]Pre-commit hooks installed![/green]")

        # Run pre-commit on all files
        if not skip_run:
            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
            ) as progress:
                progress.add_task("[bold blue]Running pre-commit on all files...", total=None)
                if run_cmd(["pre-commit", "run", "--all-files"]):
                    console.print("✅ [green]All pre-commit checks passed![/green]")
                else:
                    console.print("⚠️ [yellow]Some pre-commit checks failed, but hooks are installed.[/yellow]")
        else:
            console.print("⏭️ [yellow]Skipping pre-commit run as requested.[/yellow]")

        console.print(
            Panel(
                "[bold green]🎉 Contributor environment setup complete![/bold green]\n\n"
                "[bold]What's been set up:[/bold] Pre-commit hooks for automatic code formatting\n"
                "[bold]Next steps:[/bold] Make changes → Commit (pre-commit runs automatically)\n"
                "Run [cyan]pre-commit run --all-files[/cyan] to manually check all files",
                border_style="green",
                title="🛠️ Development Setup Complete",
            )
        )

    except KeyboardInterrupt:
        console.print("\n[bold yellow]Setup interrupted by user.[/bold yellow]")
        raise typer.Exit(0)
    except Exception as e:
        console.print(f"[bold red]Error during contributor setup: {str(e)}[/bold red]")
        raise typer.Exit(1)
