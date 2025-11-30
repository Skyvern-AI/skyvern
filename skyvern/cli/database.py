import shutil
import subprocess
import time
from typing import Optional

from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

from .console import console
from .container import ContainerRuntimeFactory


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


def is_container_runtime_running() -> bool:
    """Check if a container runtime (Docker or Podman) is available and running."""
    try:
        runtime = ContainerRuntimeFactory.get_runtime()
        return runtime.is_running()
    except RuntimeError:
        return False


def is_postgres_running_in_container() -> bool:
    """Check if the PostgreSQL container is running."""
    try:
        runtime = ContainerRuntimeFactory.get_runtime()
        return runtime.is_container_running("postgresql-container")
    except RuntimeError:
        return False


def is_postgres_container_exists() -> bool:
    """Check if the PostgreSQL container exists (running or stopped)."""
    try:
        runtime = ContainerRuntimeFactory.get_runtime()
        return runtime.container_exists("postgresql-container")
    except RuntimeError:
        return False


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

    if not is_container_runtime_running():
        console.print(
            "[red]No container runtime available. Please install and start Docker or Podman and try again.[/red]"
        )
        raise SystemExit(1)

    runtime = ContainerRuntimeFactory.get_runtime()
    runtime_name = runtime.display_name

    if is_postgres_running_in_container():
        console.print(f"🐳 [green]PostgreSQL is already running in a {runtime_name} container.[/green]")
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

        console.print(f"🚀 [bold green]Attempting to install PostgreSQL via {runtime_name}...[/bold green]")
        if not is_postgres_container_exists():
            with console.status("[bold blue]Pulling and starting PostgreSQL container...[/bold blue]"):
                runtime.run_container(
                    image="postgres:14",
                    name="postgresql-container",
                    ports={"5432": "5432"},
                    environment={"POSTGRES_HOST_AUTH_METHOD": "trust"},
                )
            console.print(f"✅ [green]PostgreSQL has been installed and started using {runtime_name}.[/green]")
        else:
            with console.status("[bold blue]Starting existing PostgreSQL container...[/bold blue]"):
                runtime.start_container("postgresql-container")
            console.print("✅ [green]Existing PostgreSQL container started.[/green]")

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
        ) as progress:
            progress.add_task("[bold blue]Waiting for PostgreSQL to become ready...", total=None)
            time.sleep(20)

        console.print("✅ [green]PostgreSQL container ready.[/green]")

    with console.status("[bold green]Checking database user...[/bold green]"):
        result = runtime.exec_in_container(
            "postgresql-container",
            ["psql", "-U", "postgres", "-c", "\\du"],
        )
        if result.success and "skyvern" in result.stdout:
            console.print("✅ [green]Database user exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database user...[/bold green]")
            runtime.exec_in_container("postgresql-container", ["createuser", "-U", "postgres", "skyvern"])
            console.print("✅ [green]Database user created.[/green]")

    with console.status("[bold green]Checking database...[/bold green]"):
        result = runtime.exec_in_container(
            "postgresql-container",
            ["psql", "-U", "postgres", "-lqt"],
        )
        if result.success and "skyvern" in result.stdout:
            console.print("✅ [green]Database exists.[/green]")
        else:
            console.print("🚀 [bold green]Creating database...[/bold green]")
            runtime.exec_in_container(
                "postgresql-container", ["createdb", "-U", "postgres", "skyvern", "-O", "skyvern"]
            )
            console.print("✅ [green]Database and user created successfully.[/green]")
