import typer
from rich.panel import Panel

from .console import console
from .run_commands import get_pids_on_port

status_app = typer.Typer(help="Check status of Skyvern components")


def _print_status(is_running: bool, name: str, start_cmd: str) -> None:
    if is_running:
        console.print(f"✅ [green]{name} is running.[/green]")
    else:
        console.print(
            Panel(
                f"[red]{name} is not running.[/red]\nRun [bold]{start_cmd}[/bold] to start it.",
                border_style="red",
            )
        )


def _check_port(port: int) -> bool:
    return bool(get_pids_on_port(port))


@status_app.command()
def all() -> None:
    """Show status for API server, UI, and database."""
    console.print("[bold blue]Skyvern component status:[/bold blue]")
    database()
    server()
    ui()


@status_app.command()
def server() -> None:
    """Check if the Skyvern API server is running."""
    from skyvern.config import settings

    is_running = _check_port(settings.PORT)
    _print_status(is_running, "API server", "skyvern run server")


@status_app.command()
def ui() -> None:
    """Check if the Skyvern UI server is running."""
    is_running = _check_port(8080)
    _print_status(is_running, "UI server", "skyvern run ui")


@status_app.command()
def database() -> None:
    """Check if PostgreSQL is running."""
    is_running = _check_port(5432)
    _print_status(is_running, "PostgreSQL", "skyvern init --no-postgres false")
