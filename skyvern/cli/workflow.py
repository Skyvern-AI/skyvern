"""Workflow-related CLI helpers."""

import typer

from .console import console

workflow_app = typer.Typer(help="Manage Skyvern workflows.")


@workflow_app.command()
def placeholder() -> None:
    """Placeholder command for workflow operations."""
    console.print("Workflow operations are not yet implemented.")
