from typing import Any

import click
import typer
from dotenv import load_dotenv

from skyvern._cli_bootstrap import configure_cli_bootstrap_logging as _configure_cli_bootstrap_logging
from skyvern.utils.env_paths import resolve_backend_env_path

from ..auth_command import login as login_command
from ..auth_command import signup as signup_command
from ..block import block_app
from ..credential import credential_app
from ..credentials import credentials_app
from ..docs import docs_app
from ..init_command import init_browser, init_env
from ..mcp_commands import mcp_app
from ..quickstart import quickstart_app
from ..run_commands import run_app
from ..setup_commands import setup_app
from ..skill_commands import skill_app
from ..status import status_app
from ..stop_commands import stop_app
from ..tasks import tasks_app
from ..workflow import workflow_app
from ._output import output, output_error
from .browser import browser_app

_cli_logging_configured = False


def configure_cli_logging() -> None:
    """Configure CLI log levels once at runtime (not at import time)."""
    global _cli_logging_configured
    if _cli_logging_configured:
        return
    _cli_logging_configured = True

    # Keep callback-time execution aligned with the entrypoint bootstrap.
    _configure_cli_bootstrap_logging()


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

cli_app.command(name="login")(login_command)
cli_app.command(name="signup", hidden=True)(signup_command)  # backwards compat

# Browser automation commands
cli_app.add_typer(browser_app, name="browser", help="Browser automation commands.")
cli_app.add_typer(mcp_app, name="mcp", help="Switch local MCP client configs and manage optional saved profiles.")
cli_app.add_typer(skill_app, name="skill", help="Manage bundled skill reference files.")
cli_app.add_typer(setup_app, name="setup", help="Register Skyvern MCP with AI coding tools.")


@init_app.callback()
def init_callback(
    ctx: typer.Context,
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
    database_string: str = typer.Option(
        "",
        "--database-string",
        help="Custom database connection string (e.g., postgresql+psycopg://user:password@host:port/dbname). When provided, skips Docker PostgreSQL setup.",
    ),
) -> None:
    """Run full initialization when no subcommand is provided."""
    if ctx.invoked_subcommand is None:
        init_env(no_postgres=no_postgres, database_string=database_string)


@init_app.command(name="browser")
def init_browser_command() -> None:
    """Initialize only the browser configuration."""
    init_browser()


@cli_app.command("capabilities")
def capabilities(
    subcommand: str = typer.Argument(None, help="Show capabilities for a specific subcommand (e.g., 'workflow')."),
    depth: int = typer.Option(
        1, "--depth", min=0, max=5, help="Recursion depth (0=top-level names only, 1=subcommands, 2+=full tree)."
    ),
    # Intentionally defaults True (agent-first) — unlike other commands which default False.
    json_output: bool = typer.Option(True, "--json/--no-json", help="Output as JSON.", show_default=True),
) -> None:
    """Return the CLI command tree for agent discovery.

    Uses progressive disclosure: default depth=1 returns top-level commands
    with their immediate subcommands (~2K tokens). Drill deeper with --depth
    or filter to a specific subcommand.

    Examples:
      skyvern capabilities                      # top-level + subcommands (~2K tokens)
      skyvern capabilities workflow              # just workflow commands
      skyvern capabilities --depth 0            # command names only (~500 tokens)
      skyvern capabilities --depth 3            # full tree (~20K tokens)
      skyvern capabilities --no-json            # human-readable
    """
    click_app = typer.main.get_command(cli_app)

    if subcommand:
        if not isinstance(click_app, click.Group):
            output_error(f"Unknown subcommand: {subcommand}", json_mode=json_output)
        ctx = click.Context(click_app, info_name="skyvern")
        child_cmd = click_app.get_command(ctx, subcommand)
        if child_cmd is None or child_cmd.hidden:
            output_error(
                f"Unknown subcommand: {subcommand}",
                hint="Run 'skyvern capabilities --depth 0' to see available commands.",
                json_mode=json_output,
            )
        tree = _walk_command_tree(child_cmd, prefix=subcommand, max_depth=depth)
    else:
        tree = _walk_command_tree(click_app, max_depth=depth)

    output(tree, action="capabilities", json_mode=json_output)


def _walk_command_tree(cmd: Any, prefix: str = "", max_depth: int = 1, _current_depth: int = 0) -> dict:
    """Recursively walk the Click/Typer command tree and return structured metadata.

    Uses progressive disclosure: max_depth controls how deep to recurse.
    depth=0 returns names only, depth=1 includes immediate subcommands, etc.
    """
    name = prefix or cmd.name or "skyvern"
    info: dict = {"name": name, "help": (cmd.help or "").strip()}

    options = []
    arguments = []
    for param in getattr(cmd, "params", []):
        if isinstance(param, click.Argument):
            arg_info: dict = {"name": param.name, "required": param.required}
            if param.type and param.type.name != "TEXT":
                arg_info["type"] = param.type.name
            arguments.append(arg_info)
        elif isinstance(param, click.Option) and param.name not in ("help",):
            opt_info: dict = {
                "name": param.name,
                "flags": list(param.opts),
                "required": param.required,
            }
            if param.help:
                opt_info["help"] = param.help
            options.append(opt_info)
    if arguments:
        info["arguments"] = arguments
    if options:
        info["options"] = options

    if isinstance(cmd, click.Group):
        if _current_depth < max_depth:
            children = []
            ctx = click.Context(cmd, info_name=name)
            for child_name in cmd.list_commands(ctx):
                child_cmd = cmd.get_command(ctx, child_name)
                if child_cmd is None or child_cmd.hidden:
                    continue
                child_prefix = f"{name} {child_name}" if prefix else child_name
                children.append(
                    _walk_command_tree(
                        child_cmd, prefix=child_prefix, max_depth=max_depth, _current_depth=_current_depth + 1
                    )
                )
            if children:
                info["subcommands"] = children
        else:
            ctx = click.Context(cmd, info_name=name)
            child_names = []
            for n in cmd.list_commands(ctx):
                c = cmd.get_command(ctx, n)
                if c is not None and not c.hidden:
                    child_names.append(n)
            if child_names:
                info["subcommand_names"] = child_names

    return info


if __name__ == "__main__":  # pragma: no cover - manual CLI invocation
    load_dotenv(resolve_backend_env_path())
    cli_app()
