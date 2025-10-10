"""Documentation-related CLI helpers."""

import webbrowser

import typer
from rich.panel import Panel

from .console import console

DOCS_URL = "https://www.skyvern.com/docs"

docs_app = typer.Typer(
    invoke_without_command=True,
    help="Open Skyvern documentation in your browser.",
)


@docs_app.callback()
def docs_callback(ctx: typer.Context) -> None:
    """Open the Skyvern documentation in a browser."""
    if ctx.invoked_subcommand is None:
        console.print(
            Panel(
                f"[bold blue]Opening Skyvern docs at [link={DOCS_URL}]{DOCS_URL}[/link][/bold blue]",
                border_style="cyan",
            )
        )
        try:
            webbrowser.open(DOCS_URL)
        except Exception as exc:  # pragma: no cover - CLI safeguard
            console.print(f"[red]Failed to open documentation: {exc}[/red]")
