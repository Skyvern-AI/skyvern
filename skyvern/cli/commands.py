import typer
from dotenv import load_dotenv

from .docs import docs_app
from .init_command import init, init_browser
from .run_commands import run_app
from .status import status_app
from .tasks import tasks_app
from .workflow import workflow_app

cli_app = typer.Typer(
    help=("""[bold]Skyvern CLI[/bold]\nManage and run your local Skyvern environment."""),
    no_args_is_help=True,
    rich_markup_mode="rich",
)
cli_app.add_typer(
    run_app,
    name="run",
    help="Run Skyvern services like the API server, UI, and MCP.",
)
cli_app.add_typer(workflow_app, name="workflow", help="Workflow management commands.")
cli_app.add_typer(tasks_app, name="tasks", help="Task management commands.")
cli_app.add_typer(docs_app, name="docs", help="Open Skyvern documentation.")
cli_app.add_typer(status_app, name="status", help="Check if Skyvern services are running.")
init_app = typer.Typer(
    invoke_without_command=True,
    help="Interactively configure Skyvern and its dependencies.",
)
cli_app.add_typer(init_app, name="init")


@init_app.callback()
def init_callback(ctx: typer.Context) -> None:
    """Run full initialization when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        init()


@init_app.command(name="browser")
def init_browser_command() -> None:
    """Initialize only the browser configuration."""
    init_browser()


if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    load_dotenv()
    cli_app()
