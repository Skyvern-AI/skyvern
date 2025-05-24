import shutil
import subprocess
import time
from typing import Optional

from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

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
            result, _ = run_command("pg_isready")
            if result is not None and "accepting connections" in result:
                status.stop()
                return True
            status.stop()
            return False
    return False


def database_exists(dbname: str, user: str) -> bool:
    check_db_command = f'psql {dbname} -U {user} -c "\\q"'
    output, _ = run_command(check_db_command, check=False)
    return output is not None


def create_database_and_user() -> None:
    console.print("🚀 [bold green]Creating database user and database...[/bold green]")
    run_command("createuser skyvern")
    run_command("createdb skyvern -O skyvern")
    console.print("✅ [bold green]Database and user created successfully.[/bold green]")


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

    if command_exists("psql") and is_postgres_running():
        console.print("✨ [green]PostgreSQL is already running locally.[/green]")
        if database_exists("skyvern", "skyvern"):
            console.print("✅ [green]Database and user exist.[/green]")
        else:
            create_database_and_user()
        return

    if no_postgres:
        console.print("[yellow]Skipping PostgreSQL container setup as requested.[/yellow]")
        console.print(
            "[italic]If you plan to use Docker Compose, its Postgres service will start automatically.[/italic]"
        )
        return

    if not is_docker_running():
        console.print(
            "[red]Docker is not running or not installed. Please install or start Docker and try again.[/red]"
        )
        raise SystemExit(1)

    if is_postgres_running_in_docker():
        console.print("🐳 [green]PostgreSQL is already running in a Docker container.[/green]")
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
                return

        console.print("🚀 [bold green]Attempting to install PostgreSQL via Docker...[/bold green]")
        if not is_postgres_container_exists():
            with console.status("[bold blue]Pulling and starting PostgreSQL container...[/bold blue]"):
                run_command(
                    "docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14"
                )
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
            run_command("docker exec postgresql-container createuser -U postgres skyvern")
            console.print("✅ [green]Database user created.[/green]")

    with console.status("[bold green]Checking database...[/bold green]"):
        _, code = run_command(
            "docker exec postgresql-container psql -U postgres -lqt | cut -d | -f 1 | grep -qw skyvern",
            check=False,
        )
        if code == 0:
            console.print("✅ [green]Database exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database...[/bold green]")
            run_command("docker exec postgresql-container createdb -U postgres skyvern -O skyvern")
            console.print("✅ [green]Database and user created successfully.[/green]")
