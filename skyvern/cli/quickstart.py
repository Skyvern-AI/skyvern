"""Quickstart command for Skyvern CLI."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

from skyvern.analytics import capture_setup_error, capture_setup_event

# Import console after skyvern.cli to ensure proper initialization
from skyvern.cli.browser import _open_chrome_inspect
from skyvern.cli.console import console
from skyvern.cli.init_command import init_env  # init is used directly
from skyvern.cli.llm_setup import setup_llm_providers
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
        is_running = result.returncode == 0
        capture_setup_event(
            "docker-check",
            success=is_running,
            error_type=None if is_running else "docker_not_running",
            error_message=None if is_running else result.stderr.strip() if result.stderr else "Docker not running",
        )
        return is_running
    except FileNotFoundError:
        capture_setup_event(
            "docker-check",
            success=False,
            error_type="docker_not_installed",
            error_message="Docker command not found",
        )
        return False
    except subprocess.SubprocessError as e:
        capture_setup_error("docker-check", e, error_type="docker_subprocess_error")
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


_COMPOSE_DATABASE_STRING = "postgresql+psycopg://skyvern:skyvern@postgres:5432/skyvern"


def _bootstrap_compose_env_files() -> None:
    """Copy `.env.example` and `skyvern-frontend/.env.example` into place when missing, and rewrite a localhost-pointing `DATABASE_STRING` to the compose-network value."""
    bootstrapped: list[tuple[str, str]] = []

    backend_env = Path(".env")
    backend_example = Path(".env.example")
    if not backend_env.exists() and backend_example.exists():
        shutil.copyfile(backend_example, backend_env)
        bootstrapped.append((str(backend_env), "edit it to set your LLM API keys before continuing"))

    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")
    if not frontend_env.exists() and frontend_example.exists():
        shutil.copyfile(frontend_example, frontend_env)
        bootstrapped.append((str(frontend_env), "no edits required"))

    if bootstrapped:
        console.print(
            Panel(
                "[green]Created from .env.example:[/green]\n"
                + "\n".join(f"  • [cyan]{path}[/cyan] — {note}" for path, note in bootstrapped),
                border_style="green",
            )
        )

    if not backend_env.exists():
        return

    content = backend_env.read_text()
    if "DATABASE_STRING" not in content or "localhost" not in content:
        return

    bad_lines = [
        line for line in content.splitlines() if line.strip().startswith("DATABASE_STRING") and "localhost" in line
    ]
    if not bad_lines:
        return

    console.print(
        Panel(
            "[bold yellow]Warning:[/bold yellow] Your [cyan].env[/cyan] sets [bold]DATABASE_STRING[/bold] to a "
            "[yellow]localhost[/yellow] URL. Inside the docker compose network that points at the backend "
            "container, not the postgres service. Update it to:\n"
            f"  [green]{_COMPOSE_DATABASE_STRING}[/green]",
            border_style="yellow",
        )
    )
    if not Confirm.ask("Update DATABASE_STRING in .env now?", default=True):
        return

    replaced = False
    new_lines: list[str] = []
    for line in content.splitlines():
        if line.strip().startswith("DATABASE_STRING") and "localhost" in line:
            if not replaced:
                new_lines.append(f'DATABASE_STRING="{_COMPOSE_DATABASE_STRING}"')
                replaced = True
            continue
        new_lines.append(line)

    backend_env.write_text("\n".join(new_lines) + "\n")
    console.print("✅ [green]Updated DATABASE_STRING for the compose network.[/green]")


def run_docker_compose_setup() -> None:
    """Run the Docker Compose setup for Skyvern."""
    console.print("\n[bold blue]Setting up Skyvern with Docker Compose...[/bold blue]")
    capture_setup_event("docker-compose-start")

    # Check for postgres container conflict
    if check_postgres_container_conflict():
        capture_setup_event(
            "docker-port-conflict",
            success=False,
            error_type="port_conflict",
            error_message="PostgreSQL container 'postgresql-container' already exists on port 5432",
            extra_data={"port": 5432},
        )
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
            capture_setup_event(
                "docker-compose-abort",
                success=False,
                error_type="user_abort",
                error_message="User aborted due to port conflict",
            )
            raise typer.Exit(0)

    _bootstrap_compose_env_files()

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
            capture_setup_event("docker-compose-complete", success=True)
        except subprocess.CalledProcessError as e:
            capture_setup_event(
                "docker-compose-fail",
                success=False,
                error_type="docker_compose_error",
                error_message=e.stderr.strip() if e.stderr else str(e),
            )
            console.print(f"[bold red]Error starting Docker Compose: {e.stderr}[/bold red]")
            raise typer.Exit(1)

    console.print(
        Panel(
            "[bold green]Skyvern is now running![/bold green]\n\n"
            "Navigate to [link]http://localhost:8080[/link] to start using the UI.\n\n"
            "To stop Skyvern, run: [cyan]docker compose down[/cyan]",
            border_style="green",
        )
    )

    # Offer to set up "Control your own browser"
    use_own_browser = Confirm.ask(
        "\nWould you like to [bold yellow]control your own Chrome browser[/bold yellow] (use your cookies, logins, and extensions)?",
        default=False,
    )
    if use_own_browser:
        console.print(
            Panel(
                "[bold]Enable Remote Debugging in Chrome[/bold]\n\n"
                "1. We'll open [cyan]chrome://inspect/#remote-debugging[/cyan] in your browser\n"
                "2. Click [bold]Enable[/bold] to start the debugging server\n"
                "3. You should see: [green]Server running at: 127.0.0.1:9222[/green]",
                border_style="cyan",
            )
        )
        open_page = Confirm.ask("Open chrome://inspect/#remote-debugging now?", default=True)
        if open_page:
            _open_chrome_inspect()
        confirmed = Confirm.ask("Have you enabled remote debugging in Chrome?", default=False)
        if confirmed:
            from skyvern.cli.llm_setup import update_or_add_env_var

            update_or_add_env_var("BROWSER_TYPE", "cdp-connect")
            update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", "http://host.docker.internal:9222/")
            console.print("✅ [green]Browser debugging configured in .env. Restart with:[/green]")
            console.print("  [cyan]docker compose up -d[/cyan]")
        else:
            console.print(
                "[yellow]No problem — you can enable it later by navigating to chrome://inspect/#remote-debugging in Chrome.[/yellow]"
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
    """Quickstart command to set up and run Skyvern with one command."""
    # Run initialization
    console.print(Panel("[bold green]🚀 Starting Skyvern Quickstart[/bold green]", border_style="green"))

    # Check if Docker Compose option was explicitly requested or offer choice
    docker_compose_available = check_docker_compose_file()

    if docker_compose:
        if not check_docker():
            console.print(
                Panel(
                    "[bold red]Docker is not installed or not running.[/bold red]\n"
                    "Docker Compose requires Docker to be running.\n"
                    "Get Docker from: [link]https://www.docker.com/get-started[/link]",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        if not docker_compose_available:
            console.print(
                Panel(
                    "[bold red]docker-compose.yml not found in current directory.[/bold red]\n"
                    "Please clone the Skyvern repository first:\n"
                    "[cyan]git clone https://github.com/skyvern-ai/skyvern.git && cd skyvern[/cyan]",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        run_docker_compose_setup()
        return

    # If Docker Compose file exists, offer the choice
    if docker_compose_available and check_docker() and not database_string:
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
        # Initialize Skyvern (pip install path)
        console.print("\n[bold blue]Initializing Skyvern...[/bold blue]")
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
                    capture_setup_event("playwright-install-complete", success=True)
                except subprocess.CalledProcessError as e:
                    capture_setup_event(
                        "playwright-install-fail",
                        success=False,
                        error_type="playwright_install_error",
                        error_message=e.stderr.strip() if e.stderr else str(e),
                    )
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
        capture_setup_event(
            "quickstart-interrupt",
            success=False,
            error_type="user_interrupt",
            error_message="Quickstart interrupted by user",
        )
        console.print("\n[bold yellow]Quickstart process interrupted by user.[/bold yellow]")
        raise typer.Exit(0)
    except Exception as e:
        capture_setup_error("quickstart-fail", e, error_type="quickstart_error")
        console.print(f"[bold red]Error during quickstart: {str(e)}[/bold red]")
        raise typer.Exit(1)
