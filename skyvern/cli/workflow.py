"""Workflow-related CLI helpers."""

import typer

from .console import console

workflow_app = typer.Typer()


@workflow_app.command()
def placeholder() -> None:
    """Placeholder command for workflow operations."""
    console.print("Workflow operations are not yet implemented.")
