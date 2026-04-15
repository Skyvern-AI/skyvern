import os
import socket

import typer
from rich.table import Table

from .commands._output import output
from .console import console

status_app = typer.Typer(help="Check status of Skyvern components.", invoke_without_command=True)


def _check_port(port: int) -> bool:
    """Return True if a local port is accepting connections."""
    try:
        with socket.create_connection(("localhost", port), timeout=0.5):
            return True
    except OSError:
        return False


def _status_data() -> list[dict]:
    api_port = int(os.getenv("PORT", 8000))
    ui_port = 8080
    db_port = 5432

    return [
        {"component": "API server", "running": _check_port(api_port), "start_command": "skyvern run server"},
        {"component": "UI server", "running": _check_port(ui_port), "start_command": "skyvern run ui"},
        {
            "component": "PostgreSQL",
            "running": _check_port(db_port),
            "start_command": "skyvern init --no-postgres false",
        },
    ]


def _status_table(data: list[dict]) -> Table:
    table = Table(title="Skyvern Component Status")
    table.add_column("Component", style="bold")
    table.add_column("Running")
    table.add_column("Start Command")

    for item in data:
        status = "[green]Yes[/green]" if item["running"] else "[red]No[/red]"
        table.add_row(item["component"], status, item["start_command"])

    return table


@status_app.callback(invoke_without_command=True)
def status_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show status for API server, UI, and database.

    Examples:
      skyvern status
      skyvern status --json
    """
    if ctx.invoked_subcommand is None:
        data = _status_data()
        if json_output:
            output(data, action="status", json_mode=True)
        else:
            console.print(_status_table(data))
