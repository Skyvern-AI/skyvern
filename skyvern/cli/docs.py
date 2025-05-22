"""Documentation-related CLI helpers."""

import typer

from .console import console

docs_app = typer.Typer()


@docs_app.command()
def placeholder() -> None:
    """Placeholder command for documentation actions."""
    console.print("Documentation commands are not yet implemented.")
