"""Quickstart command for Skyvern CLI."""

import asyncio
import importlib
import importlib.metadata
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt
from rich.text import Text

from skyvern.cli.console import console
from skyvern.utils.env_paths import EnvScope, parse_env_scope, resolve_frontend_env_path

quickstart_app = typer.Typer(help="Quickstart command to set up and run Skyvern with one command.")

_SKYVERN_COMPOSE_SERVICES = {"postgres", "skyvern", "skyvern-ui"}


def _server_extra_install_target() -> str:
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file() and (cwd / "skyvern").is_dir():
        return ".[server]"

    try:
        version = importlib.metadata.version("skyvern")
    except importlib.metadata.PackageNotFoundError:
        return "skyvern[server]"
    return f"skyvern[server]=={version}"


def _install_server_extra_for_quickstart(*, assume_yes: bool = False) -> bool:
    target = _server_extra_install_target()
    console.print(
        Panel(
            "Local quickstart needs the server dependencies.\n\n"
            f"Install [cyan]{escape(target)}[/cyan] into the current Python environment now?",
            title="Missing Local Server Dependency",
            border_style="yellow",
        )
    )
    if not assume_yes and not Confirm.ask("Install the missing server dependencies now?", default=True):
        _print_server_guidance()
        return False

    console.print(f"📦 [bold blue]Installing {escape(target)}...[/bold blue]")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--retries", "5", "--timeout", "60", target],
            check=True,
        )
    except subprocess.CalledProcessError as install_error:
        console.print(f"[bold red]Failed to install {target}: {install_error}[/bold red]")
        _print_server_guidance()
        return False

    importlib.invalidate_caches()
    return True


def capture_setup_event(
    event_name: str,
    success: bool = True,
    error_type: str | None = None,
    error_message: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> None:
    from skyvern.analytics import capture_setup_event as _capture_setup_event  # noqa: PLC0415

    _capture_setup_event(event_name, success, error_type, error_message, extra_data)


def capture_setup_error(
    event_name: str,
    error: Exception,
    error_type: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> None:
    from skyvern.analytics import capture_setup_error as _capture_setup_error  # noqa: PLC0415

    _capture_setup_error(event_name, error, error_type, extra_data)


class QuickstartPath(str, Enum):
    CLOUD = "cloud"
    LOCAL = "local"
    SERVER = "server"


def _has_server_quickstart_extra() -> bool:
    from skyvern.exceptions import SkyvernExtraNotInstalled, require_server_extra_modules  # noqa: PLC0415

    try:
        require_server_extra_modules("skyvern quickstart", ("uvicorn", "fastmcp"))
    except SkyvernExtraNotInstalled:
        return False
    return True


def _has_local_quickstart_extra() -> bool:
    from skyvern.exceptions import SkyvernExtraNotInstalled, require_local_extra_modules  # noqa: PLC0415

    try:
        require_local_extra_modules("skyvern quickstart")
    except SkyvernExtraNotInstalled:
        return False
    return True


def _is_interactive_input() -> bool:
    return sys.stdin.isatty()


def _default_quickstart_path(*, has_local_extra: bool, has_server_extra: bool) -> QuickstartPath:
    if has_server_extra:
        return QuickstartPath.SERVER
    if has_local_extra:
        return QuickstartPath.LOCAL
    return QuickstartPath.CLOUD


_QUICKSTART_PATH_CHOICES = {
    "1": QuickstartPath.CLOUD,
    "cloud": QuickstartPath.CLOUD,
    "api": QuickstartPath.CLOUD,
    "2": QuickstartPath.LOCAL,
    "local": QuickstartPath.LOCAL,
    "embedded": QuickstartPath.LOCAL,
    "3": QuickstartPath.SERVER,
    "server": QuickstartPath.SERVER,
    "self-hosted": QuickstartPath.SERVER,
    "selfhosted": QuickstartPath.SERVER,
}


def _parse_quickstart_path(value: str) -> QuickstartPath:
    choice = _QUICKSTART_PATH_CHOICES.get(value.strip().lower())
    if choice is None:
        raise typer.BadParameter("Choose one of: cloud/api, local/embedded, server/self-hosted, 1, 2, or 3.")
    return choice


def _validate_install_type(value: str | None) -> str | None:
    if value is not None:
        _parse_quickstart_path(value)
    return value


def _print_quickstart_selector(
    *,
    default_path: QuickstartPath,
    has_local_extra: bool,
    has_server_extra: bool,
) -> None:
    local_status = "installed" if has_local_extra else 'requires `pip install "skyvern[local]"`'
    server_status = "installed" if has_server_extra else 'requires `pip install "skyvern[server]"`'
    message = f"""Choose how you want to use Skyvern:

1. Cloud/API SDK usage
   Status: installed with `pip install skyvern`

2. Embedded local Python SDK via skyvern[local]
   Status: {local_status}

3. Self-hosted local server via skyvern[server]
   Status: {server_status}

Default: {default_path.value}
"""
    console.print(Panel(Text(message), title="Skyvern Quickstart", border_style="cyan"))


def _select_quickstart_path(
    install_type: str | None,
    *,
    has_local_extra: bool,
    has_server_extra: bool,
) -> QuickstartPath:
    default_path = _default_quickstart_path(has_local_extra=has_local_extra, has_server_extra=has_server_extra)
    if install_type is not None:
        return _parse_quickstart_path(install_type)
    _print_quickstart_selector(
        default_path=default_path,
        has_local_extra=has_local_extra,
        has_server_extra=has_server_extra,
    )
    if not _is_interactive_input():
        return default_path

    default_choice = {
        QuickstartPath.CLOUD: "1",
        QuickstartPath.LOCAL: "2",
        QuickstartPath.SERVER: "3",
    }[default_path]
    while True:
        try:
            selected = Prompt.ask(
                "Choose a quickstart path (1/cloud, 2/local, 3/server)",
                default=default_choice,
            )
        except EOFError:
            return default_path
        try:
            return _parse_quickstart_path(selected)
        except typer.BadParameter as exc:
            console.print(f"[red]{exc.message}[/red]")


def _server_quickstart_flags_requested(
    *,
    no_postgres: bool,
    database_string: str,
    skip_browser_install: bool,
    server_only: bool,
) -> bool:
    """Return whether the user supplied options for the Python server setup path."""
    return no_postgres or bool(database_string) or skip_browser_install or server_only


def _parse_quickstart_env_scope(value: str) -> EnvScope:
    try:
        return parse_env_scope(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _print_cloud_guidance() -> None:
    message = """Cloud/API SDK usage

Installed with: pip install skyvern
Next command: skyvern setup

Python SDK:
  from skyvern import Skyvern
  skyvern = Skyvern(api_key="YOUR_API_KEY")

No local browser, Postgres, Docker, migrations, or server startup is required.
"""
    console.print(Panel(Text(message), title="Cloud/API SDK", border_style="cyan"))


def _print_local_guidance(*, has_local_extra: bool) -> None:
    install_line = "Installed: skyvern[local]" if has_local_extra else 'Install: pip install "skyvern[local]"'
    message = f"""Embedded local Python SDK

{install_line}
Next command: python -m playwright install chromium

Use:
  Skyvern.local(use_in_memory_db=True)

This path does not require Postgres, Docker, migrations, or `skyvern run server`.
"""
    console.print(Panel(Text(message), title="Embedded Local SDK", border_style="cyan"))


def _print_server_guidance() -> None:
    message = """Self-hosted local server

Install: pip install "skyvern[server]"
Next: python -m skyvern quickstart

This path sets up the local server, database, local API key, and MCP.
Wheel installs run the backend only; use a source checkout or Docker Compose for the local UI.
"""
    console.print(Panel(Text(message), title="Self-Hosted Server", border_style="cyan"))


def _browser_install_blocks_startup(init_result: object) -> bool:
    browser_install = getattr(init_result, "browser_install", None)
    return bool(
        getattr(init_result, "run_local", False)
        and getattr(browser_install, "required", False)
        and not getattr(browser_install, "ready", True)
    )


def _run_server_quickstart(
    *,
    no_postgres: bool,
    database_string: str,
    skip_browser_install: bool,
    server_only: bool,
    skip_llm_setup: bool = False,
    configure_mcp: bool | None = None,
    browser_type: Literal["chromium-headful", "chromium-headless", "cdp-connect"] | None = None,
    browser_location: str | None = None,
    remote_debugging_url: str | None = None,
    analytics_id: str | None = None,
    start_services_now: bool | None = None,
) -> None:
    try:
        from skyvern.cli.init_command import init_env  # noqa: PLC0415
        from skyvern.cli.utils import start_services  # noqa: PLC0415

        # Initialize Skyvern (pip install path)
        console.print("\n[bold blue]Initializing Skyvern...[/bold blue]")
        init_result = init_env(
            no_postgres=no_postgres,
            database_string=database_string,
            skip_browser_install=skip_browser_install,
            mode="local",
            skip_llm_setup=skip_llm_setup,
            configure_mcp=configure_mcp,
            browser_type=browser_type,
            browser_location=browser_location,
            remote_debugging_url=remote_debugging_url,
            analytics_id=analytics_id,
            return_result=True,
        )
        run_local = bool(init_result)
        if run_local:
            _configure_local_browser_streaming_defaults()

        # Start services
        if run_local:
            if _browser_install_blocks_startup(init_result):
                console.print(
                    Panel(
                        "[bold yellow]Skyvern setup is saved, but the browser install is incomplete.[/bold yellow]\n\n"
                        "Quickstart will not start services yet because the selected browser mode launches "
                        "Playwright-managed Chromium.\n\n"
                        "Finish the browser install, then start Skyvern:\n"
                        "[cyan]playwright install chromium[/cyan]\n"
                        "[cyan]skyvern run server[/cyan]\n\n"
                        "If you want to use an existing Chrome over CDP instead, rerun quickstart and choose "
                        "Local browser -> Use actual browser, or pass [cyan]--skip-browser-install[/cyan] "
                        "after configuring CDP.",
                        border_style="yellow",
                    )
                )
                return

            start_now = (
                start_services_now
                if start_services_now is not None
                else typer.confirm("\nDo you want to start Skyvern services now?", default=True)
            )
            if start_now:
                console.print("\n[bold blue]Starting Skyvern services...[/bold blue]")
                asyncio.run(start_services(server_only=server_only))
            else:
                start_command = (
                    "skyvern run server" if server_only or resolve_frontend_env_path() is None else "skyvern run all"
                )
                console.print(
                    f"\n[yellow]Skipping service startup. You can start services later with '{start_command}'[/yellow]"
                )

    except KeyboardInterrupt:
        capture_setup_event(
            "quickstart-interrupt",
            success=False,
            error_type="user_interrupt",
            error_message="Quickstart interrupted by user",
        )
        console.print("\n[bold yellow]Quickstart process interrupted by user.[/bold yellow]")
        raise typer.Exit(0)
    except typer.Exit:
        raise
    except Exception as e:
        capture_setup_error("quickstart-fail", e, error_type="quickstart_error")
        console.print(f"[bold red]Error during quickstart: {str(e)}[/bold red]")
        raise typer.Exit(1)


def check_docker() -> bool:
    """Check if Docker is installed and running."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        is_running = result.returncode == 0
        capture_setup_event(
            "docker-check",
            success=is_running,
            error_type=None if is_running else "docker_not_running",
            error_message=None if is_running else result.stderr.strip() if result.stderr else "Docker not running",
        )
        return is_running
    except FileNotFoundError:
        capture_setup_event(
            "docker-check",
            success=False,
            error_type="docker_not_installed",
            error_message="Docker command not found",
        )
        return False
    except subprocess.SubprocessError as e:
        capture_setup_error("docker-check", e, error_type="docker_subprocess_error")
        return False


def check_docker_compose_file() -> bool:
    """Check if docker-compose.yml exists in the current directory."""
    return Path("docker-compose.yml").exists() or Path("docker-compose.yaml").exists()


def _run_docker_command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def get_postgres_container_state() -> str | None:
    """Return the standalone quickstart Postgres container state when it exists."""
    result = _run_docker_command(["docker", "inspect", "--format", "{{.State.Status}}", "postgresql-container"])
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or "unknown"


def check_postgres_container_conflict() -> bool:
    """Check if the standalone quickstart Postgres container exists."""
    return get_postgres_container_state() is not None


def _running_skyvern_compose_services() -> list[str]:
    """Return running services from the current Skyvern Docker Compose project."""
    result = _run_docker_command(["docker", "compose", "ps", "--services", "--status", "running"])
    if result is None or result.returncode != 0:
        return []

    services = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [service for service in services if service in _SKYVERN_COMPOSE_SERVICES]


def _handle_running_compose_stack() -> None:
    services = _running_skyvern_compose_services()
    if not services:
        return

    capture_setup_event(
        "docker-compose-existing-detected",
        success=True,
        extra_data={"services": services},
    )
    console.print(
        Panel(
            "[bold yellow]Skyvern Docker Compose is already running.[/bold yellow]\n\n"
            f"Running services: [cyan]{', '.join(services)}[/cyan]\n\n"
            "If you changed configuration during quickstart, restarting the compose stack "
            "avoids stale containers and port conflicts.",
            border_style="yellow",
        )
    )
    if not Confirm.ask("Run [cyan]docker compose down[/cyan] before continuing?", default=True):
        console.print("[yellow]Continuing with the existing Docker Compose stack.[/yellow]")
        capture_setup_event(
            "docker-compose-existing-keep",
            success=True,
            extra_data={"services": services},
        )
        return

    try:
        subprocess.run(
            ["docker", "compose", "down"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        capture_setup_event(
            "docker-compose-down-fail",
            success=False,
            error_type="docker_compose_down_error",
            error_message=e.stderr.strip() if e.stderr else str(e),
        )
        console.print(f"[bold red]Error stopping Docker Compose: {e.stderr}[/bold red]")
        raise typer.Exit(1)

    console.print("✅ [green]Stopped existing Skyvern Docker Compose services.[/green]")
    capture_setup_event(
        "docker-compose-existing-stopped",
        success=True,
        extra_data={"services": services},
    )


def _handle_postgres_container_conflict() -> None:
    container_state = get_postgres_container_state()
    if container_state is None:
        return

    capture_setup_event(
        "docker-postgres-container-detected",
        success=True,
        extra_data={"state": container_state},
    )
    console.print(
        Panel(
            "[bold yellow]Standalone PostgreSQL container detected.[/bold yellow]\n\n"
            "A container named [cyan]postgresql-container[/cyan] already exists "
            f"([cyan]{container_state}[/cyan]). This is the standalone Postgres container "
            "created by the local quickstart path and can conflict with Docker Compose "
            "setups that bind Postgres to the host.\n\n"
            "Docker Compose will create and manage its own [cyan]postgres[/cyan] service.",
            border_style="yellow",
        )
    )
    remove_container = Confirm.ask(
        "Remove [cyan]postgresql-container[/cyan] before continuing?",
        default=False,
    )
    if not remove_container:
        console.print(
            "[yellow]Continuing without removing postgresql-container. "
            "If Docker Compose reports a Postgres conflict, rerun quickstart and remove it.[/yellow]"
        )
        capture_setup_event(
            "docker-postgres-container-keep",
            success=True,
            extra_data={"state": container_state},
        )
        return

    try:
        subprocess.run(
            ["docker", "rm", "-f", "postgresql-container"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        capture_setup_event(
            "docker-postgres-container-remove-fail",
            success=False,
            error_type="docker_rm_error",
            error_message=e.stderr.strip() if e.stderr else str(e),
        )
        console.print(f"[bold red]Failed to remove postgresql-container: {e.stderr}[/bold red]")
        raise typer.Exit(1)

    console.print("✅ [green]Removed standalone postgresql-container.[/green]")
    capture_setup_event(
        "docker-postgres-container-removed",
        success=True,
        extra_data={"state": container_state},
    )


def _configure_local_browser_streaming_defaults() -> None:
    """Enable local browser streaming for self-hosted quickstart paths."""
    from dotenv import set_key

    from skyvern.cli.llm_setup import update_or_add_env_var

    update_or_add_env_var("BROWSER_STREAMING_MODE", "cdp")

    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")
    if not frontend_env.exists() and frontend_example.exists():
        import shutil

        frontend_env.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(frontend_example, frontend_env)
        console.print("✅ [green]Created skyvern-frontend/.env from .env.example[/green]")

    if frontend_env.exists():
        set_key(str(frontend_env), "VITE_BROWSER_STREAMING_MODE", "cdp", quote_mode="never")


def run_docker_compose_setup() -> None:
    """Run the Docker Compose setup for Skyvern."""
    from skyvern.cli.llm_setup import setup_llm_providers  # noqa: PLC0415

    console.print("\n[bold blue]Setting up Skyvern with Docker Compose...[/bold blue]")
    capture_setup_event("docker-compose-start")

    _handle_running_compose_stack()
    _handle_postgres_container_conflict()

    # Configure LLM provider
    console.print("\n[bold blue]Step 1: Configure LLM Provider[/bold blue]")
    setup_llm_providers()
    _configure_local_browser_streaming_defaults()

    # Run docker compose up
    console.print("\n[bold blue]Step 2: Starting Docker Compose...[/bold blue]")
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True, console=console
    ) as progress:
        progress.add_task("[bold blue]Starting Docker containers...", total=None)
        try:
            subprocess.run(
                ["docker", "compose", "up", "-d"],
                check=True,
                capture_output=True,
                text=True,
            )
            console.print("✅ [green]Docker Compose started successfully.[/green]")
            capture_setup_event("docker-compose-complete", success=True)
        except subprocess.CalledProcessError as e:
            capture_setup_event(
                "docker-compose-fail",
                success=False,
                error_type="docker_compose_error",
                error_message=e.stderr.strip() if e.stderr else str(e),
            )
            console.print(f"[bold red]Error starting Docker Compose: {e.stderr}[/bold red]")
            raise typer.Exit(1)

    from skyvern.cli.utils import wait_for_docker_services  # noqa: PLC0415

    if wait_for_docker_services():
        console.print(
            Panel(
                "[bold green]Skyvern is ready![/bold green]\n\n"
                "Navigate to [link]http://localhost:8080[/link] to start using the UI.\n\n"
                "To stop Skyvern, run: [cyan]docker compose down[/cyan]",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                "[yellow]Services are still starting up.[/yellow]\n\n"
                "Navigate to [link]http://localhost:8080[/link] once ready.\n"
                "Run [cyan]docker compose logs -f[/cyan] to monitor progress.\n\n"
                "To stop Skyvern, run: [cyan]docker compose down[/cyan]",
                border_style="yellow",
            )
        )

    # Offer to set up "Control your own browser"
    use_own_browser = Confirm.ask(
        "\nWould you like to [bold yellow]control your own Chrome browser[/bold yellow] (use your cookies, logins, and extensions)?",
        default=False,
    )
    if use_own_browser:
        from skyvern.cli.browser import _print_classic_cdp_instructions  # noqa: PLC0415

        _print_classic_cdp_instructions()
        confirmed = Confirm.ask("Have you enabled remote debugging in Chrome?", default=False)
        if confirmed:
            from skyvern.cli.llm_setup import update_or_add_env_var

            update_or_add_env_var("BROWSER_TYPE", "cdp-connect")
            update_or_add_env_var("BROWSER_REMOTE_DEBUGGING_URL", "http://host.docker.internal:9222/")
            console.print("✅ [green]Browser debugging configured in .env. Restart with:[/green]")
            console.print("  [cyan]docker compose up -d[/cyan]")
        else:
            console.print(
                "[yellow]No problem - you can configure it later by setting "
                "BROWSER_TYPE=cdp-connect and starting Chrome with a Docker-reachable "
                "remote debugging address.[/yellow]"
            )


@quickstart_app.callback(invoke_without_command=True)
def quickstart(
    ctx: typer.Context,
    no_postgres: bool = typer.Option(False, "--no-postgres", help="Skip starting PostgreSQL container"),
    database_string: str = typer.Option(
        "",
        "--database-string",
        help="Custom database connection string (e.g., postgresql+psycopg://user:password@host:port/dbname). When provided, skips Docker PostgreSQL setup.",
    ),
    skip_browser_install: bool = typer.Option(
        False, "--skip-browser-install", help="Skip Chromium browser installation"
    ),
    server_only: bool = typer.Option(False, "--server-only", help="Only start the server, not the UI"),
    docker_compose: bool = typer.Option(False, "--docker-compose", help="Use Docker Compose for full setup"),
    install_type: str | None = typer.Option(
        None,
        "--install-type",
        callback=_validate_install_type,
        help="Choose quickstart path: cloud, local, or server.",
    ),
    env_scope: str | None = typer.Option(
        None,
        "--env-scope",
        help="Backend env location for setup writes: legacy/current, project, or global.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Automatically approve installing missing Skyvern server dependencies.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help=(
            "Run the self-hosted server quickstart path without prompts. Installs missing server dependencies, "
            "skips LLM/MCP prompts, uses headless Chromium, anonymous analytics, and does not start services."
        ),
    ),
    skip_llm_setup: bool = typer.Option(
        False,
        "--skip-llm-setup",
        help="Skip interactive LLM provider setup and keep/default environment values.",
    ),
    skip_mcp: bool = typer.Option(False, "--skip-mcp", help="Skip interactive MCP server configuration."),
    browser_type: Literal["chromium-headful", "chromium-headless", "cdp-connect"] | None = typer.Option(
        None,
        "--browser-type",
        help="Browser type to write without prompting.",
    ),
    browser_location: str | None = typer.Option(
        None,
        "--browser-location",
        help="Chrome executable path to write when using a custom browser location.",
    ),
    remote_debugging_url: str | None = typer.Option(
        None,
        "--remote-debugging-url",
        help="CDP URL to write when using --browser-type cdp-connect.",
    ),
    analytics_id: str | None = typer.Option(
        None,
        "--analytics-id",
        help="Analytics identifier to write without prompting. Use 'anonymous' for agent tests.",
    ),
    start_services_now: bool | None = typer.Option(
        None,
        "--start/--no-start",
        help="Start Skyvern services after setup. Omit to prompt.",
    ),
) -> None:
    """Quickstart command to set up and run Skyvern with one command."""
    # Run initialization
    console.print(Panel("[bold green]🚀 Starting Skyvern Quickstart[/bold green]", border_style="green"))
    install_type_value = install_type if isinstance(install_type, str) else None
    env_scope_value = env_scope if isinstance(env_scope, str) else None
    if non_interactive:
        install_type_value = install_type_value or QuickstartPath.SERVER.value
        yes = True
        skip_llm_setup = True
        skip_mcp = True
        browser_type = browser_type or "chromium-headless"
        analytics_id = analytics_id or "anonymous"
        if start_services_now is None:
            start_services_now = False
        if not database_string:
            no_postgres = True
            console.print(
                Panel(
                    "[bold yellow]Non-interactive quickstart will not prompt to start PostgreSQL.[/bold yellow]\n\n"
                    "No [cyan]--database-string[/cyan] was provided, so Skyvern will use any existing "
                    "[cyan]DATABASE_STRING[/cyan] from your environment or .env file.\n\n"
                    "For unattended agent tests, pass "
                    "[cyan]--database-string postgresql+psycopg://user:pass@host:5432/dbname[/cyan].",
                    border_style="yellow",
                )
            )

    has_server_extra = _has_server_quickstart_extra()
    # The server extra is a superset of the local embedded runtime dependencies.
    has_local_extra = has_server_extra or _has_local_quickstart_extra()
    server_flags_requested = _server_quickstart_flags_requested(
        no_postgres=no_postgres,
        database_string=database_string,
        skip_browser_install=skip_browser_install,
        server_only=server_only,
    )
    if docker_compose:
        if non_interactive:
            console.print(
                Panel(
                    "[bold red]Non-interactive Docker Compose quickstart is not supported yet.[/bold red]\n"
                    "Use [cyan]--install-type server --database-string postgresql+psycopg://...[/cyan] "
                    "for unattended agent tests.",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        selected_path = (
            QuickstartPath.SERVER if install_type_value is None else _parse_quickstart_path(install_type_value)
        )
        if selected_path is not QuickstartPath.SERVER:
            console.print(
                Panel(
                    "[bold red]Conflicting quickstart options.[/bold red]\n"
                    "`--docker-compose` starts the self-hosted server stack. "
                    "Use `--install-type server` or omit `--install-type`.",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        if env_scope_value is not None and _parse_quickstart_env_scope(env_scope_value) is not EnvScope.LEGACY:
            console.print(
                Panel(
                    "[bold red]Conflicting quickstart options.[/bold red]\n"
                    "Docker Compose uses the source checkout `.env` file. "
                    "Use `--env-scope legacy` or omit `--env-scope`.",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
    elif install_type_value is None and server_flags_requested:
        selected_path = QuickstartPath.SERVER
    else:
        selected_path = _select_quickstart_path(
            install_type_value,
            has_local_extra=has_local_extra,
            has_server_extra=has_server_extra,
        )
    if selected_path is QuickstartPath.CLOUD:
        _print_cloud_guidance()
        raise typer.Exit(0)
    if selected_path is QuickstartPath.LOCAL:
        _print_local_guidance(has_local_extra=has_local_extra)
        raise typer.Exit(0)
    if env_scope_value is not None and _parse_quickstart_env_scope(env_scope_value) is not EnvScope.LEGACY:
        console.print(
            Panel(
                "[bold red]Conflicting quickstart options.[/bold red]\n"
                "Self-hosted local server setup writes ./.env. "
                "Project/global scopes are for cloud/API config.",
                border_style="red",
            )
        )
        raise typer.Exit(1)

    # Check if Docker Compose option was explicitly requested or offer choice
    docker_compose_available = check_docker_compose_file()

    if docker_compose:
        if not check_docker():
            console.print(
                Panel(
                    "[bold red]Docker is not installed or not running.[/bold red]\n"
                    "Docker Compose requires Docker to be running.\n"
                    "Get Docker from: [link]https://www.docker.com/get-started[/link]",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        if not docker_compose_available:
            console.print(
                Panel(
                    "[bold red]docker-compose.yml not found in current directory.[/bold red]\n"
                    "Please clone the Skyvern repository first:\n"
                    "[cyan]git clone https://github.com/skyvern-ai/skyvern.git && cd skyvern[/cyan]",
                    border_style="red",
                )
            )
            raise typer.Exit(1)
        run_docker_compose_setup()
        return

    # If Docker Compose file exists, offer the choice
    if not non_interactive and docker_compose_available and check_docker() and not server_flags_requested:
        console.print("\n[bold blue]Setup Method[/bold blue]")
        console.print("Docker Compose file detected. Choose your setup method:\n")
        console.print("  [cyan]1.[/cyan] [green]Docker Compose (Recommended)[/green] - Full containerized setup")
        console.print("  [cyan]2.[/cyan] pip install - Local Python setup with Docker for PostgreSQL only\n")

        use_docker_compose = Confirm.ask(
            "Would you like to use Docker Compose for the full setup?",
            default=True,
        )

        if use_docker_compose:
            run_docker_compose_setup()
            return

    if not _has_server_quickstart_extra():
        if not yes and not _is_interactive_input():
            _print_server_guidance()
            raise typer.Exit(0)
        if not _install_server_extra_for_quickstart(assume_yes=yes) or not _has_server_quickstart_extra():
            raise typer.Exit(1)

    _run_server_quickstart(
        no_postgres=no_postgres,
        database_string=database_string,
        skip_browser_install=skip_browser_install,
        server_only=server_only,
        skip_llm_setup=skip_llm_setup,
        configure_mcp=False if skip_mcp else None,
        browser_type=browser_type,
        browser_location=browser_location,
        remote_debugging_url=remote_debugging_url,
        analytics_id=analytics_id,
        start_services_now=start_services_now,
    )
