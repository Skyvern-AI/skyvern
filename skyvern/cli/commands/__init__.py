import typer
from dotenv import load_dotenv

from skyvern._cli_bootstrap import configure_cli_bootstrap_logging as _configure_cli_bootstrap_logging
from skyvern.cli.lazy import LazyTyperGroup, register_lazy_command
from skyvern.utils.env_paths import resolve_backend_env_path

_cli_logging_configured = False


def configure_cli_logging() -> None:
    """Configure CLI log levels once at runtime (not at import time)."""
    global _cli_logging_configured
    if _cli_logging_configured:
        return
    _cli_logging_configured = True

    # Keep callback-time execution aligned with the entrypoint bootstrap.
    _configure_cli_bootstrap_logging()


# ---------------------------------------------------------------------------
# Register lazy sub-commands (no module imports at definition time)
# ---------------------------------------------------------------------------

register_lazy_command(
    "run", "skyvern.cli.run_commands", "run_app", "Run Skyvern services like the API server, UI, and MCP."
)
register_lazy_command("block", "skyvern.cli.block", "block_app", "Inspect and validate workflow block schemas.")
register_lazy_command(
    "credential", "skyvern.cli.credential", "credential_app", "MCP-parity credential commands (list/get/delete)."
)
register_lazy_command(
    "config",
    "skyvern.cli.config_command",
    "config_app",
    "Read and update organization settings (max_steps_per_run, webhook URL, retries, artifact URL expiry).",
)
register_lazy_command("workflow", "skyvern.cli.workflow", "workflow_app", "Workflow management commands.")
register_lazy_command(
    "schedule",
    "skyvern.cli.schedule_command",
    "schedule_app",
    "Manage workflow schedules (list, create, update, enable/disable, delete).",
)
register_lazy_command("tasks", "skyvern.cli.tasks", "tasks_app", "Task management commands.")
register_lazy_command(
    "credentials",
    "skyvern.cli.credentials",
    "credentials_app",
    "Secure credential management (use this for interactive `add`).",
)
register_lazy_command("docs", "skyvern.cli.docs", "docs_app", "Open Skyvern documentation.")
register_lazy_command("status", "skyvern.cli.status", "status_app", "Check if Skyvern services are running.")
register_lazy_command("stop", "skyvern.cli.stop_commands", "stop_app", "Stop Skyvern services.")
register_lazy_command(
    "quickstart",
    "skyvern.cli.quickstart",
    "quickstart_app",
    "One-command setup and start for Skyvern (combines init and run).",
)
register_lazy_command("browser", "skyvern.cli.commands.browser", "browser_app", "Browser automation commands.")
register_lazy_command(
    "mcp", "skyvern.cli.mcp_commands", "mcp_app", "Switch local MCP client configs and manage optional saved profiles."
)
register_lazy_command("skill", "skyvern.cli.skill_commands", "skill_app", "Manage bundled skill reference files.")
register_lazy_command("setup", "skyvern.cli.setup_commands", "setup_app", "Register Skyvern MCP with AI coding tools.")
register_lazy_command(
    "init", "skyvern.cli.init_command", "init_app_factory", "Interactively configure Skyvern and its dependencies."
)
register_lazy_command("doctor", "skyvern.cli.doctor", "doctor_app", "Check Skyvern installation health.")

# ---------------------------------------------------------------------------
# Main CLI app
# ---------------------------------------------------------------------------

cli_app = typer.Typer(
    cls=LazyTyperGroup,
    help=("""[bold]Skyvern CLI[/bold]\nManage and run your local Skyvern environment."""),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@cli_app.callback()
def cli_callback() -> None:
    """Configure CLI logging before command execution."""
    configure_cli_logging()


# ---------------------------------------------------------------------------
# Eagerly-defined commands (lightweight, base-deps only)
# ---------------------------------------------------------------------------


@cli_app.command(name="login")
def login_command(
    base_url: str = typer.Option(
        "https://app.skyvern.com",
        "--base-url",
        help="Frontend URL (e.g. http://localhost:8080 for local dev)",
    ),
    timeout: int = typer.Option(
        300,
        "--timeout",
        help="Timeout in seconds waiting for browser authentication",
    ),
) -> None:
    """Authenticate with Skyvern Cloud and save your API key."""
    from skyvern.cli.auth_command import login as _login  # noqa: PLC0415

    _login(base_url=base_url, timeout=timeout)


@cli_app.command(name="signup", hidden=True)
def signup_command(
    base_url: str = typer.Option(
        "https://app.skyvern.com",
        "--base-url",
        help="Frontend URL (e.g. http://localhost:8080 for local dev)",
    ),
    timeout: int = typer.Option(
        300,
        "--timeout",
        help="Timeout in seconds waiting for browser authentication",
    ),
) -> None:
    """Authenticate with Skyvern Cloud (backwards-compat alias)."""
    from skyvern.cli.auth_command import signup as _signup  # noqa: PLC0415

    _signup(base_url=base_url, timeout=timeout)


@cli_app.command("capabilities")
def capabilities(
    subcommand: str = typer.Argument(None, help="Show capabilities for a specific subcommand (e.g., 'workflow')."),
    depth: int = typer.Option(
        1, "--depth", min=0, max=5, help="Recursion depth (0=top-level names only, 1=subcommands, 2+=full tree)."
    ),
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
    import click as _click  # noqa: PLC0415

    from skyvern.cli.commands._output import output, output_error  # noqa: PLC0415

    click_app = typer.main.get_group(cli_app)

    if subcommand:
        if not isinstance(click_app, _click.Group):
            output_error(f"Unknown subcommand: {subcommand}", json_mode=json_output)
        ctx = _click.Context(click_app, info_name="skyvern")
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


def _walk_command_tree(cmd: object, prefix: str = "", max_depth: int = 1, _current_depth: int = 0) -> dict:
    """Recursively walk the Click/Typer command tree and return structured metadata."""
    import click as _click  # noqa: PLC0415

    name = prefix or getattr(cmd, "name", None) or "skyvern"
    info: dict = {"name": name, "help": (getattr(cmd, "help", "") or "").strip()}

    options = []
    arguments = []
    for param in getattr(cmd, "params", []):
        if isinstance(param, _click.Argument):
            arg_info: dict = {"name": param.name, "required": param.required}
            if param.type and param.type.name != "TEXT":
                arg_info["type"] = param.type.name
            arguments.append(arg_info)
        elif isinstance(param, _click.Option) and param.name not in ("help",):
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

    if isinstance(cmd, _click.Group):
        if _current_depth < max_depth:
            children = []
            ctx = _click.Context(cmd, info_name=name)
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
            ctx = _click.Context(cmd, info_name=name)
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
