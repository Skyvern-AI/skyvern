import logging

import typer
from dotenv import load_dotenv

from skyvern.forge.sdk.forge_log import setup_logger as _setup_logger
from skyvern.utils.env_paths import resolve_backend_env_path

from ..block import block_app
from ..credential import credential_app
from ..credentials import credentials_app
from ..docs import docs_app
from ..init_command import init_browser, init_env
from ..quickstart import quickstart_app
from ..run_commands import run_app
from ..status import status_app
from ..stop_commands import stop_app
from ..tasks import tasks_app
from ..workflow import workflow_app
from .browser import browser_app

_cli_logging_configured = False


def configure_cli_logging() -> None:
    """Configure CLI log levels once at runtime (not at import time)."""
    global _cli_logging_configured
    if _cli_logging_configured:
        return
    _cli_logging_configured = True

    # Suppress noisy SDK/third-party logs for CLI execution only.
    for logger_name in ("skyvern", "httpx", "litellm", "playwright", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    _setup_logger()


cli_app = typer.Typer(
    help=("""[bold]Skyvern CLI[/bold]\nManage and run your local Skyvern environment."""),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@cli_app.callback()
def cli_callback() -> None:
    """Configure CLI logging before command execution."""
    configure_cli_logging()


cli_app.add_typer(
    run_app,
    name="run",
    help="Run Skyvern services like the API server, UI, and MCP.",
)
cli_app.add_typer(block_app, name="block", help="Inspect and validate workflow block schemas.")
cli_app.add_typer(
    credential_app,
    name="credential",
    help="MCP-parity credential commands (list/get/delete).",
)
cli_app.add_typer(workflow_app, name="workflow", help="Workflow management commands.")
cli_app.add_typer(tasks_app, name="tasks", help="Task management commands.")
cli_app.add_typer(
    credentials_app,
    name="credentials",
    help="Secure credential management (use this for interactive `add`).",
)
cli_app.add_typer(docs_app, name="docs", help="Open Skyvern documentation.")
cli_app.add_typer(status_app, name="status", help="Check if Skyvern services are running.")
cli_app.add_typer(stop_app, name="stop", help="Stop Skyvern services.")
init_app = typer.Typer(
    invoke_without_command=True,
    help="Interactively configure Skyvern and its dependencies.",
)
cli_app.add_typer(init_app, name="init")

# Add quickstart command
cli_app.add_typer(
    quickstart_app, name="quickstart", help="One-command setup and start for Skyvern (combines init and run)."
)

# Browser automation commands
cli_app.add_typer(browser_app, name="browser", help="Browser automation commands.")


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
