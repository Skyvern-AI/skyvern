import shutil
import subprocess
import time
from typing import Optional

from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

from skyvern.analytics import capture_setup_event

from .console import console


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_command(command: str, check: bool = True) -> tuple[Optional[str], Optional[int]]:
    try:
        result = subprocess.run(command, shell=True, check=check, capture_output=True, text=True)
        return result.stdout.strip(), result.returncode
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error executing command: [bold]{command}[/bold][/red]", style="red")
        console.print(f"[red]Stderr: {e.stderr.strip()}[/red]", style="red")
        return None, e.returncode


def is_postgres_running() -> bool:
    if command_exists("pg_isready"):
        with console.status("[bold green]Checking PostgreSQL status...") as status:
            result, _ = run_command("pg_isready", check=False)
            if result is not None and "accepting connections" in result:
                status.stop()
                return True
            status.stop()
            return False
    return False


def role_and_database_ready(user: str, dbname: str) -> bool:
    _, code = run_command(f'psql {dbname} -U {user} -c "\\q"', check=False)
    return code == 0


def _role_exists_via_catalog(user: str) -> bool:
    output, code = run_command(
        f"psql postgres -tAc \"SELECT 1 FROM pg_roles WHERE rolname='{user}'\"",
        check=False,
    )
    return code == 0 and output is not None and "1" in output


def _database_exists_via_catalog(dbname: str) -> bool:
    output, code = run_command(
        f"psql postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='{dbname}'\"",
        check=False,
    )
    return code == 0 and output is not None and "1" in output


def create_database_and_user() -> None:
    console.print("🚀 [bold green]Creating database user and database...[/bold green]")

    if _role_exists_via_catalog("skyvern"):
        console.print("✅ [green]Role 'skyvern' already exists.[/green]")
    else:
        console.print("  Creating role 'skyvern'...")
        _, code = run_command("createuser skyvern", check=False)
        if code != 0:
            console.print(
                "[red]Failed to create role 'skyvern'. "
                "You may need to create it manually:[/red]\n"
                "  [bold]createuser skyvern[/bold]"
            )
            raise SystemExit(1)
        console.print("  ✅ [green]Role 'skyvern' created.[/green]")

    if _database_exists_via_catalog("skyvern"):
        console.print("✅ [green]Database 'skyvern' already exists.[/green]")
    else:
        console.print("  Creating database 'skyvern'...")
        _, code = run_command("createdb skyvern -O skyvern", check=False)
        if code != 0:
            console.print(
                "[red]Failed to create database 'skyvern'. "
                "You may need to create it manually:[/red]\n"
                "  [bold]createdb skyvern -O skyvern[/bold]"
            )
            raise SystemExit(1)
        console.print("  ✅ [green]Database 'skyvern' created.[/green]")

    console.print("✅ [bold green]Database and user are ready.[/bold green]")


def is_docker_running() -> bool:
    if not command_exists("docker"):
        return False
    _, code = run_command("docker info", check=False)
    return code == 0


def is_postgres_running_in_docker() -> bool:
    _, code = run_command("docker ps | grep -q postgresql-container", check=False)
    return code == 0


def is_postgres_container_exists() -> bool:
    _, code = run_command("docker ps -a | grep -q postgresql-container", check=False)
    return code == 0


def setup_postgresql(no_postgres: bool = False) -> None:
    """Set up PostgreSQL database for Skyvern."""
    console.print(Panel("[bold cyan]PostgreSQL Setup[/bold cyan]", border_style="blue"))
    capture_setup_event("database-start")

    if command_exists("psql") and is_postgres_running():
        console.print("✨ [green]PostgreSQL is already running locally.[/green]")
        capture_setup_event("database-local-detected", success=True, extra_data={"source": "local"})
        if role_and_database_ready("skyvern", "skyvern"):
            console.print("✅ [green]Database and user exist.[/green]")
        else:
            create_database_and_user()
        capture_setup_event("database-complete", success=True, extra_data={"source": "local"})
        return

    if no_postgres:
        console.print("[yellow]Skipping PostgreSQL container setup as requested.[/yellow]")
        console.print(
            "[italic]If you plan to use Docker Compose, its Postgres service will start automatically.[/italic]"
        )
        capture_setup_event("database-skip", success=True, extra_data={"reason": "no_postgres_flag"})
        return

    if not is_docker_running():
        capture_setup_event(
            "database-fail",
            success=False,
            error_type="docker_not_running",
            error_message="Docker is not running or not installed",
        )
        console.print(
            "[red]Docker is not running or not installed. Please install or start Docker and try again.[/red]"
        )
        raise SystemExit(1)

    if is_postgres_running_in_docker():
        console.print("🐳 [green]PostgreSQL is already running in a Docker container.[/green]")
        capture_setup_event("database-docker-detected", success=True, extra_data={"source": "docker_existing"})
    else:
        if not no_postgres:
            start_postgres = Confirm.ask(
                "[yellow]No local Postgres detected. Start a disposable container now?[/yellow]\n"
                '[tip: choose "n" if you plan to run Skyvern via Docker Compose instead of `skyvern run server`]'
            )
            if not start_postgres:
                console.print("[yellow]Skipping PostgreSQL container setup.[/yellow]")
                console.print(
                    "[italic]If you plan to use Docker Compose, its Postgres service will start automatically.[/italic]"
                )
                capture_setup_event("database-skip", success=True, extra_data={"reason": "user_declined"})
                return

        console.print("🚀 [bold green]Attempting to install PostgreSQL via Docker...[/bold green]")
        if not is_postgres_container_exists():
            with console.status("[bold blue]Pulling and starting PostgreSQL container...[/bold blue]"):
                output, code = run_command(
                    "docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14"
                )
                if code != 0:
                    capture_setup_event(
                        "database-container-fail",
                        success=False,
                        error_type="docker_run_error",
                        error_message=output or "Failed to start PostgreSQL container",
                    )
                    console.print(
                        "[red]Warning: Failed to start PostgreSQL container. Check Docker logs for details.[/red]"
                    )
                else:
                    console.print("✅ [green]PostgreSQL has been installed and started using Docker.[/green]")
        else:
            with console.status("[bold blue]Starting existing PostgreSQL container...[/bold blue]"):
                run_command("docker start postgresql-container")
            console.print("✅ [green]Existing PostgreSQL container started.[/green]")

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
        ) as progress:
            progress.add_task("[bold blue]Waiting for PostgreSQL to become ready...", total=None)
            time.sleep(20)

        console.print("✅ [green]PostgreSQL container ready.[/green]")

    with console.status("[bold green]Checking database user...[/bold green]"):
        _, code = run_command(
            'docker exec postgresql-container psql -U postgres -c "\\du" | grep -q skyvern', check=False
        )
        if code == 0:
            console.print("✅ [green]Database user exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database user...[/bold green]")
            output, user_code = run_command("docker exec postgresql-container createuser -U postgres skyvern")
            if user_code != 0:
                capture_setup_event(
                    "database-user-create-fail",
                    success=False,
                    error_type="createuser_error",
                    error_message=output or "Failed to create database user",
                )
                console.print("[red]Warning: Failed to create database user.[/red]")
            else:
                console.print("✅ [green]Database user created.[/green]")

    with console.status("[bold green]Checking database...[/bold green]"):
        _, code = run_command(
            'docker exec postgresql-container psql -U postgres -lqt | cut -d "|" -f 1 | grep -qw skyvern',
            check=False,
        )
        if code == 0:
            console.print("✅ [green]Database exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database...[/bold green]")
            output, db_code = run_command("docker exec postgresql-container createdb -U postgres skyvern -O skyvern")
            if db_code != 0:
                capture_setup_event(
                    "database-create-fail",
                    success=False,
                    error_type="createdb_error",
                    error_message=output or "Failed to create database",
                )
                console.print("[red]Warning: Failed to create database.[/red]")
            else:
                console.print("✅ [green]Database and user created successfully.[/green]")

    capture_setup_event("database-complete", success=True, extra_data={"source": "docker"})
