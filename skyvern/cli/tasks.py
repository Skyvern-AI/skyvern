"""Task-related CLI helpers."""

import typer

from .console import console

tasks_app = typer.Typer()


@tasks_app.command()
def placeholder() -> None:
    """Placeholder command for task management."""
    console.print("Task operations are not yet implemented.")
