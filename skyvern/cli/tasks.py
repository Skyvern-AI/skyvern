"""Task-related CLI helpers."""

import typer

from .console import console

tasks_app = typer.Typer(help="Manage Skyvern tasks and operations.")


@tasks_app.command()
def placeholder() -> None:
    """Placeholder command for task management."""
    console.print("Task operations are not yet implemented.")
