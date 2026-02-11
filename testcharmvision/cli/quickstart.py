"""Quickstart command for Testcharmvision CLI."""

import asyncio
import subprocess
from pathlib import Path

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

# Import console after testcharmvision.cli to ensure proper initialization
from testcharmvision.cli.console import console
from testcharmvision.cli.init_command import init_env  # init is used directly
from testcharmvision.cli.llm_setup import setup_llm_providers
from testcharmvision.cli.utils import start_services

quickstart_app = typer.Typer(help="Quickstart command to set up and run Testcharmvision with one command.")


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


def check_docker_compose_file() -> bool:
    """Check if docker-compose.yml exists in the current directory."""
    return Path("docker-compose.yml").exists() or Path("docker-compose.yaml").exists()


def check_postgres_container_conflict() -> bool:
    """Check if postgresql-container exists and is using port 5432."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=postgresql-container", "--format", "{{.Names}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return "postgresql-container" in result.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def run_docker_compose_setup() -> None:
    """Run the Docker Compose setup for Testcharmvision."""
    console.print("\n[bold blue]Setting up Testcharmvision with Docker Compose...[/bold blue]")

    # Check for postgres container conflict
    if check_postgres_container_conflict():
        console.print(
            Panel(
                "[bold yellow]Warning: Existing PostgreSQL container detected![/bold yellow]\n\n"
                "A container named 'postgresql-container' already exists, which may conflict\n"
                "with the PostgreSQL service in Docker Compose (both use port 5432).\n\n"
                "To avoid conflicts, remove the existing container first:\n"
                "[cyan]docker rm -f postgresql-container[/cyan]",
                border_style="yellow",
            )
        )
        proceed = Confirm.ask("Do you want to continue anyway?", default=False)
        if not proceed:
            console.print("[yellow]Aborting Docker Compose setup. Please remove the container and try again.[/yellow]")
            raise typer.Exit(0)

    # Configure LLM provider
    console.print("\n[bold blue]Step 1: Configure LLM Provider[/bold blue]")
    setup_llm_providers()

    # Run docker compose up
    console.print("\n[bold blue]Step 2: Starting Docker Compose...[/bold blue]")
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
    ) as progress:
        progress.add_task("[bold blue]Starting Docker containers...", total=None)
        try:
            subprocess.run(
                ["docker", "compose", "up", "-d"],
                check=True,
                capture_output=True,
                text=True,
            )
            console.print("✅ [green]Docker Compose started successfully.[/green]")
        except subprocess.CalledProcessError as e:
            console.print(f"[bold red]Error starting Docker Compose: {e.stderr}[/bold red]")
            raise typer.Exit(1)

    console.print(
        Panel(
            "[bold green]Testcharmvision is now running![/bold green]\n\n"
            "Navigate to [link]http://localhost:8080[/link] to start using the UI.\n\n"
            "To stop Testcharmvision, run: [cyan]docker compose down[/cyan]",
            border_style="green",
        )
    )


@quickstart_app.callback(invoke_without_command=True)
def quickstart(
    ctx: typer.Context,
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
    database_string: str = typer.Option(
        "",
        "--database-string",
        help="Custom database connection string (e.g., postgresql+psycopg://user:password@host:port/dbname). When provided, skips Docker PostgreSQL setup.",
    ),
    skip_browser_install: bool = typer.Option(
        False, "--skip-browser-install", help="Skip Chromium browser installation"
    ),
    server_only: bool = typer.Option(False, "--server-only", help="Only start the server, not the UI"),
    docker_compose: bool = typer.Option(False, "--docker-compose", help="Use Docker Compose for full setup"),
) -> None:
    """Quickstart command to set up and run Testcharmvision with one command."""
    # Check Docker
    with console.status("Checking Docker installation...") as status:
        docker_available = check_docker()
        if docker_available:
            status.update("✅ Docker is installed and running")
        else:
            if not database_string:
                console.print(
                    Panel(
                        "[bold red]Docker is not installed or not running.[/bold red]\n"
                        "Please install Docker and start it before running quickstart.\n"
                        "Get Docker from: [link]https://www.docker.com/get-started[/link]",
                        border_style="red",
                    )
                )
                raise typer.Exit(1)

    # Run initialization
    console.print(Panel("[bold green]🚀 Starting Testcharmvision Quickstart[/bold green]", border_style="green"))

    # Check if Docker Compose option was explicitly requested or offer choice
    docker_compose_available = check_docker_compose_file()

    if docker_compose:
        if not docker_compose_available:
            console.print(
                Panel(
                    "[bold red]docker-compose.yml not found in current directory.[/bold red]\n"
                    "Please clone the Testcharmvision repository first:\n"
                    "[cyan]git clone https://github.com/testcharmvision-ai/testcharmvision.git && cd testcharmvision[/cyan]",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        run_docker_compose_setup()
        return

    # If Docker Compose file exists, offer the choice
    if docker_compose_available and docker_available and not database_string:
        console.print("\n[bold blue]Setup Method[/bold blue]")
        console.print("Docker Compose file detected. Choose your setup method:\n")
        console.print("  [cyan]1.[/cyan] [green]Docker Compose (Recommended)[/green] - Full containerized setup")
        console.print("  [cyan]2.[/cyan] pip install - Local Python setup with Docker for PostgreSQL only\n")

        use_docker_compose = Confirm.ask(
            "Would you like to use Docker Compose for the full setup?",
            default=True,
        )

        if use_docker_compose:
            run_docker_compose_setup()
            return

    try:
        # Initialize Testcharmvision (pip install path)
        console.print("\n[bold blue]Initializing Testcharmvision...[/bold blue]")
        run_local = init_env(no_postgres=no_postgres, database_string=database_string)

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
            start_now = typer.confirm("\nDo you want to start Testcharmvision services now?", default=True)
            if start_now:
                console.print("\n[bold blue]Starting Testcharmvision services...[/bold blue]")
                asyncio.run(start_services(server_only=server_only))
            else:
                console.print(
                    "\n[yellow]Skipping service startup. You can start services later with 'testcharmvision run all'[/yellow]"
                )

    except KeyboardInterrupt:
        console.print("\n[bold yellow]Quickstart process interrupted by user.[/bold yellow]")
        raise typer.Exit(0)
    except Exception as e:
        console.print(f"[bold red]Error during quickstart: {str(e)}[/bold red]")
        raise typer.Exit(1)
