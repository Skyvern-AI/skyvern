import os
import socket

import typer
from rich.table import Table

from .console import console

status_app = typer.Typer(help="Check status of Skyvern components.", invoke_without_command=True)


def _check_port(port: int) -> bool:
    """Return True if a local port is accepting connections."""
    try:
        with socket.create_connection(("localhost", port), timeout=0.5):
            return True
    except OSError:
        return False


def _status_table() -> Table:
    api_port = int(os.getenv("PORT", 8000))
    ui_port = 8080
    db_port = 5432

    components = [
        ("API server", _check_port(api_port), "skyvern run server"),
        ("UI server", _check_port(ui_port), "skyvern run ui"),
        ("PostgreSQL", _check_port(db_port), "skyvern init --no-postgres false"),
    ]

    table = Table(title="Skyvern Component Status")
    table.add_column("Component", style="bold")
    table.add_column("Running")
    table.add_column("Start Command")

    for name, running, cmd in components:
        status = "[green]Yes[/green]" if running else "[red]No[/red]"
        table.add_row(name, status, cmd)

    return table


@status_app.callback(invoke_without_command=True)
def status_callback(ctx: typer.Context) -> None:
    """Show status for API server, UI, and database."""
    if ctx.invoked_subcommand is None:
        console.print(_status_table())
