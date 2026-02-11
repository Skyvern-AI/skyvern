import typer
from dotenv import load_dotenv

from testcharmvision.utils.env_paths import resolve_backend_env_path

from .docs import docs_app
from .init_command import init_browser, init_env
from .quickstart import quickstart_app
from .run_commands import run_app
from .status import status_app
from .stop_commands import stop_app
from .tasks import tasks_app
from .workflow import workflow_app

cli_app = typer.Typer(
    help=("""[bold]Testcharmvision CLI[/bold]\nManage and run your local Testcharmvision environment."""),
    no_args_is_help=True,
    rich_markup_mode="rich",
)
cli_app.add_typer(
    run_app,
    name="run",
    help="Run Testcharmvision services (API server and UI).",
)
cli_app.add_typer(workflow_app, name="workflow", help="Workflow management commands.")
cli_app.add_typer(tasks_app, name="tasks", help="Task management commands.")
cli_app.add_typer(docs_app, name="docs", help="Open Testcharmvision documentation.")
cli_app.add_typer(status_app, name="status", help="Check if Testcharmvision services are running.")
cli_app.add_typer(stop_app, name="stop", help="Stop Testcharmvision services.")
init_app = typer.Typer(
    invoke_without_command=True,
    help="Interactively configure Testcharmvision and its dependencies.",
)
cli_app.add_typer(init_app, name="init")

# Add quickstart command
cli_app.add_typer(
    quickstart_app, name="quickstart", help="One-command setup and start for Testcharmvision (combines init and run)."
)


@init_app.callback()
def init_callback(
    ctx: typer.Context,
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
) -> None:
    """Run full initialization when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        init_env(no_postgres=no_postgres)


@init_app.command(name="browser")
def init_browser_command() -> None:
    """Initialize only the browser configuration."""
    init_browser()


if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    load_dotenv(resolve_backend_env_path())
    cli_app()
