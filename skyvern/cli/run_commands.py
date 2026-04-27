import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Annotated, List, Literal, Optional

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

import psutil
import typer
import uvicorn
from dotenv import load_dotenv, set_key
from rich.panel import Panel
from rich.prompt import Confirm
from starlette.middleware import Middleware
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.responses import Response as StarletteResponse

from skyvern.cli.commands._output import output_error
from skyvern.cli.commands._tty import is_interactive
from skyvern.cli.console import console
from skyvern.cli.core.result import set_concise_responses
from skyvern.cli.utils import start_services
from skyvern.config import settings
from skyvern.utils import detect_os
from skyvern.utils.env_paths import resolve_backend_env_path, resolve_frontend_env_path

run_app = typer.Typer(help="Commands to run Skyvern services such as the API server or UI.")
_mcp_cleanup_done = False


def _default_host() -> str:
    """Return a safe default bind host. Windows quickstart fails to bind 0.0.0.0; use loopback instead."""
    return "127.0.0.1" if sys.platform == "win32" else "0.0.0.0"


async def _cleanup_mcp_resources() -> None:
    from skyvern.cli.core.client import close_skyvern  # noqa: PLC0415
    from skyvern.cli.core.mcp_http_auth import close_auth_db  # noqa: PLC0415
    from skyvern.cli.core.session_manager import close_current_session  # noqa: PLC0415

    try:
        await close_current_session()
    finally:
        try:
            await close_skyvern()
        finally:
            await close_auth_db()


def _cleanup_mcp_resources_blocking() -> None:
    global _mcp_cleanup_done
    if _mcp_cleanup_done:
        return

    try:
        asyncio.run(_cleanup_mcp_resources())
        _mcp_cleanup_done = True
    except Exception:
        logging.getLogger(__name__).warning("MCP cleanup failed", exc_info=True)


def _cleanup_mcp_resources_sync() -> None:
    """Atexit callback for MCP cleanup. Skips if an event loop is still running
    because asyncio.run() cannot be called inside a running loop. This means
    cleanup is best-effort for signal-based exits (e.g. SIGTERM) that fire atexit
    while the MCP server's loop is still alive -- the finally block in run_mcp()
    handles normal shutdown instead."""
    logger = logging.getLogger(__name__)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _cleanup_mcp_resources_blocking()
        return

    logger.debug("Skipping MCP cleanup because event loop is still running")


def get_pids_on_port(port: int) -> List[int]:
    """Return a list of PIDs listening on the given port."""
    pids = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                pids.append(conn.pid)
    except Exception:
        pass
    return list(set(pids))


def kill_pids(pids: List[int]) -> None:
    """Kill the given list of PIDs in a cross-platform way."""
    host_system = detect_os()
    for pid in pids:
        try:
            if host_system in {"windows", "wsl"}:
                subprocess.run(f"taskkill /PID {pid} /F", shell=True, check=False)
            else:
                os.kill(pid, 9)
        except Exception:
            console.print(f"[red]Failed to kill process {pid}[/red]")


@run_app.command(name="server")
def run_server() -> None:
    """Run the Skyvern API server."""
    try:
        import sqlalchemy  # noqa: F401, PLC0415
    except ImportError as exc:
        from skyvern.cli.lazy import _handle_missing_dep  # noqa: PLC0415

        _handle_missing_dep(exc)

    load_dotenv(resolve_backend_env_path())
    from skyvern.config import settings  # noqa: PLC0415

    port = settings.PORT
    console.print(Panel(f"[bold green]Starting Skyvern API Server on port {port}...", border_style="green"))
    uvicorn.run(
        "skyvern.forge.api_app:create_api_app",
        host=_default_host(),
        port=port,
        log_level="info",
        factory=True,
        ws="websockets-sansio",
    )


def _handle_port_conflict(port: int, *, force: bool, command_hint: str) -> bool:
    """Check for existing process on port and handle it.

    Returns True if the caller should proceed (port is free or was freed).
    Returns False if the user declined to kill the existing process.
    Exits with error if non-interactive and --force was not passed.
    """
    try:
        pids = get_pids_on_port(port)
        if not pids:
            return True
        if force:
            kill_pids(pids)
            console.print("[green]Process killed (--force).[/green]")
            return True
        if not is_interactive():
            output_error(
                f"Process already running on port {port}.",
                hint=command_hint,
            )
        response = Confirm.ask(f"Process already running on port {port}. [yellow]Kill it?[/yellow]")
        if response:
            kill_pids(pids)
            console.print("[green]Process killed.[/green]")
        else:
            console.print(f"[yellow]Server not started. Process already running on port {port}.[/yellow]")
            return False
    except Exception as e:  # pragma: no cover - CLI safeguards
        console.print(f"[red]Error checking for process on port {port}: {e}[/red]")
        return False
    return True


@run_app.command(name="ui")
def run_ui(
    force: bool = typer.Option(False, "--force", help="Kill existing process on port 8080 without prompting."),
) -> None:
    """Run the Skyvern UI server.

    Examples:
      skyvern run ui
      skyvern run ui --force
    """
    console.print(Panel("[bold blue]Starting Skyvern UI Server...[/bold blue]", border_style="blue"))
    if not _handle_port_conflict(8080, force=force, command_hint="skyvern run ui --force"):
        return

    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path is None:
        console.print("[bold red]ERROR: Skyvern Frontend directory not found.[/bold red]")
        return

    frontend_dir = frontend_env_path.parent
    if not frontend_env_path.exists():
        console.print("[bold blue]Setting up frontend .env file...[/bold blue]")
        shutil.copy(frontend_dir / ".env.example", frontend_env_path)
        console.print("✅ [green]Successfully set up frontend .env file[/green]")

    backend_env_path = resolve_backend_env_path()
    if backend_env_path.exists():
        load_dotenv(backend_env_path)
        skyvern_api_key = os.getenv("SKYVERN_API_KEY")
        if skyvern_api_key:
            set_key(frontend_env_path, "VITE_SKYVERN_API_KEY", skyvern_api_key)
        else:
            console.print("[red]ERROR: SKYVERN_API_KEY not found in .env file[/red]")
    else:
        console.print(f"[red]ERROR: Backend .env file not found at {backend_env_path}[/red]")

    os.chdir(frontend_dir)

    try:
        console.print("📦 [bold blue]Running npm install...[/bold blue]")
        subprocess.run("npm install --silent", shell=True, check=True)
        console.print("✅ [green]npm install complete.[/green]")
        console.print("🚀 [bold blue]Starting npm UI server...[/bold blue]")
        subprocess.run("npm run start", shell=True, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Error running UI server: {e}[/bold red]")
        return


@run_app.command(name="ui-dev")
def run_ui_dev(
    force: bool = typer.Option(False, "--force", help="Kill existing process on port 8080 without prompting."),
) -> None:
    """Run the Skyvern UI server in development mode (npm run start-local).

    Examples:
      skyvern run ui-dev
      skyvern run ui-dev --force
    """
    console.print(Panel("[bold blue]Starting Skyvern UI Server (dev mode)...[/bold blue]", border_style="blue"))
    if not _handle_port_conflict(8080, force=force, command_hint="skyvern run ui-dev --force"):
        return

    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path is None:
        console.print("[bold red]ERROR: Skyvern Frontend directory not found.[/bold red]")
        return

    frontend_dir = frontend_env_path.parent

    os.chdir(frontend_dir)

    try:
        console.print("📦 [bold blue]Running npm ci...[/bold blue]")
        subprocess.run("npm ci", shell=True, check=True)
        console.print("✅ [green]npm ci complete.[/green]")
        console.print("🚀 [bold blue]Starting npm UI server (start-local)...[/bold blue]")
        subprocess.run("npm run start-local", shell=True, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Error running UI server: {e}[/bold red]")
        return


@run_app.command(name="all")
def run_all() -> None:
    """Run the Skyvern API server and UI server in parallel."""
    asyncio.run(start_services())


@run_app.command(name="dev")
def run_dev() -> None:
    """Run the Skyvern API server and UI server in the background (detached).

    This command starts both services and immediately returns control to your terminal.
    Use 'skyvern stop all' to stop the services.
    """
    load_dotenv(resolve_backend_env_path())
    from skyvern.config import settings as skyvern_settings  # noqa: PLC0415

    console.print(Panel("[bold green]Starting Skyvern in development mode...[/bold green]", border_style="green"))

    # Start server in background (detached) - call uvicorn directly
    server_process = subprocess.Popen(
        [
            "uvicorn",
            "skyvern.forge.api_app:create_api_app",
            "--host",
            _default_host(),
            "--port",
            str(skyvern_settings.PORT),
            "--factory",
            "--ws",
            "websockets-sansio",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    console.print(f"✅ [green]Server started in background (PID: {server_process.pid})[/green]")

    # Start UI (dev mode) in background (detached) - call npm directly
    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path is None:
        console.print("[bold red]ERROR: Skyvern Frontend directory not found.[/bold red]")
        return
    frontend_dir = frontend_env_path.parent

    ui_process = subprocess.Popen(
        ["npm", "run", "start-local"],
        cwd=frontend_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    console.print(f"✅ [green]UI (dev mode) started in background (PID: {ui_process.pid})[/green]")

    console.print("\n🎉 [bold green]Skyvern is starting![/bold green]")
    console.print(f"🌐 [bold]API server:[/bold] [cyan]http://localhost:{skyvern_settings.PORT}[/cyan]")
    console.print("🖥️  [bold]UI:[/bold] [cyan]http://localhost:8080[/cyan]")
    console.print("\n[dim]Use 'skyvern stop all' to stop the services.[/dim]")


class _ServerCardMiddleware:
    """Serve /.well-known/mcp/server-card.json for HTTP MCP transports."""

    def __init__(self, app: "ASGIApp", transport_type: str, host: str, port: int, mcp_path: str = "/mcp") -> None:
        from skyvern.cli.core.server_card import build_server_card  # noqa: PLC0415

        self.app = app
        self.transport_type = transport_type
        card_host = "localhost" if host in ("0.0.0.0", "::") else host
        host_part = f"[{card_host}]" if ":" in card_host else card_host
        endpoint_url = os.environ.get("SKYVERN_MCP_PUBLIC_URL") or f"http://{host_part}:{port}{mcp_path}"
        self.card = build_server_card(self.transport_type, endpoint_url)

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] == "http" and scope["path"] == "/.well-known/mcp/server-card.json":
            cors_headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, x-api-key",
            }
            request_method = scope.get("method", "GET")
            if request_method == "OPTIONS":
                response = StarletteResponse(status_code=204, headers=cors_headers)
            else:
                response = StarletteJSONResponse(content=self.card, headers=cors_headers)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


@run_app.command(name="mcp")
def run_mcp(
    transport: Annotated[
        Literal["stdio", "sse", "streamable-http"],
        typer.Option(
            "--transport",
            help="MCP transport: stdio (default), sse, or streamable-http.",
        ),
    ] = "stdio",
    host: Annotated[
        str, typer.Option("--host", help="Host for HTTP transports.")
    ] = _default_host(),  # sys.platform is constant; safe at import time
    port: Annotated[int, typer.Option("--port", help="Port for HTTP transports.")] = 8000,
    path: Annotated[str, typer.Option("--path", help="HTTP endpoint path for MCP transport.")] = "/mcp",
    stateless_http: Annotated[
        bool,
        typer.Option(
            "--stateless-http/--no-stateless-http",
            help="Use stateless HTTP semantics for HTTP transports (ignored for stdio).",
        ),
    ] = True,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose/--no-verbose",
            help="Return full tool responses including sdk_equivalent, browser_context, and timing.",
        ),
    ] = False,
) -> None:
    """Run the MCP server with configurable transport for local or remote hosting."""
    from skyvern.cli.core.mcp_http_auth import MCPAPIKeyMiddleware  # noqa: PLC0415
    from skyvern.cli.core.session_manager import set_stateless_http_mode  # noqa: PLC0415
    from skyvern.cli.mcp_tools import mcp  # noqa: PLC0415
    from skyvern.cli.mcp_tools.telemetry import configure_mcp_telemetry_runtime  # noqa: PLC0415

    path = _normalize_mcp_path(path)
    stateless_http_enabled = transport != "stdio" and stateless_http
    configure_mcp_telemetry_runtime(server_mode="local_cli", transport=transport)
    # atexit covers signal-based exits (SIGTERM); finally covers normal
    # mcp.run() completion or unhandled exceptions. Both are needed because
    # atexit doesn't fire on normal return and finally doesn't fire on signals.
    atexit.register(_cleanup_mcp_resources_sync)
    set_stateless_http_mode(stateless_http_enabled)
    set_concise_responses(not verbose)
    try:
        if transport == "stdio":
            mcp.run(transport="stdio")
            return

        middleware = [
            Middleware(_ServerCardMiddleware, transport_type=transport, host=host, port=port, mcp_path=path),
            Middleware(MCPAPIKeyMiddleware),
        ]
        mcp.run(
            transport=transport,
            host=host,
            port=port,
            path=path,
            middleware=middleware,
            stateless_http=stateless_http_enabled,
        )
    finally:
        set_stateless_http_mode(False)
        set_concise_responses(False)
        _cleanup_mcp_resources_blocking()


def _normalize_mcp_path(path: str) -> str:
    path = path.strip()
    if not path:
        return "/mcp"
    if not path.startswith("/"):
        return f"/{path}"
    return path


@run_app.command(
    name="code",
    context_settings={"allow_interspersed_args": False},
)
def run_code(
    script_path: str = typer.Argument(..., help="Path to the Python script to run"),
    params: List[str] = typer.Option([], "-p", help="Parameters in format param=value (without leading dash)"),
    params_json: str = typer.Option(None, "--params", help="JSON string of parameters"),
    params_file: str = typer.Option(None, "--params-file", help="Path to JSON file with parameters"),
    ai: Optional[str] = typer.Option(
        "fallback", "--ai", help="AI mode to use for the script. Options: fallback, proactive or None"
    ),
) -> None:
    """Run a Python script with parameters.

    Supports three ways to pass parameters (in order of priority):

    1. JSON file (highest priority):
        skyvern run code main.py --params-file params.json

    2. JSON string:
        skyvern run code main.py --params '{"param1": "val1", "param2": "val2"}'

    3. Individual flags (lowest priority):
        skyvern run code main.py -p param1=val1 -p param2=val2

    Note: For backward compatibility, leading dashes in -p values are automatically stripped.
    """
    # Disable LiteLLM loggers
    os.environ["LITELLM_LOG"] = "CRITICAL"
    try:
        import litellm  # noqa: PLC0415
    except ImportError as exc:
        from skyvern.cli.lazy import _handle_missing_dep  # noqa: PLC0415

        _handle_missing_dep(exc)

    litellm.suppress_debug_info = True
    litellm.set_verbose = False

    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    logging.getLogger("LiteLLM Router").setLevel(logging.CRITICAL)
    logging.getLogger("LiteLLM Proxy").setLevel(logging.CRITICAL)
    settings.LOG_LEVEL = "CRITICAL"

    from skyvern.forge.sdk.forge_log import setup_logger  # noqa: PLC0415

    setup_logger()

    # Validate script path
    if not script_path:
        console.print("[red]❌ Error: No script path provided[/red]")
        console.print("[yellow]→ Action: Provide a path to your Python script[/yellow]")
        console.print("[blue]Example: skyvern run code main.py -p param1=value1[/blue]")
        raise typer.Exit(code=1)

    if not os.path.exists(script_path):
        console.print("[red]❌ Error: Cannot find script file[/red]")
        console.print(f"[yellow]→ Looked for: {script_path}[/yellow]")
        console.print("[yellow]→ Action: Check that the file exists and the path is correct[/yellow]")
        # Show current directory to help user understand relative paths
        console.print(f"[blue]Current directory: {os.getcwd()}[/blue]")
        raise typer.Exit(code=1)

    if not script_path.endswith(".py"):
        console.print("[red]❌ Error: Invalid file type[/red]")
        console.print(f"[yellow]→ Provided: {script_path}[/yellow]")
        console.print("[yellow]→ Action: Please provide a Python script file ending with .py[/yellow]")
        console.print("[blue]Example: skyvern run code my_script.py[/blue]")
        raise typer.Exit(code=1)

    parameters = {}

    # Priority: params_file > params_json > individual -p flags
    if params_file:
        try:
            with open(params_file) as f:
                parameters = json.load(f)
            console.print(f"[blue]✓ Loaded parameters from file: {params_file}[/blue]")
        except FileNotFoundError:
            console.print("[red]❌ Error: Cannot find parameters file[/red]")
            console.print(f"[yellow]→ Looked for: {params_file}[/yellow]")
            console.print("[yellow]→ Action: Check that the file exists and the path is correct[/yellow]")
            console.print(f"[blue]Current directory: {os.getcwd()}[/blue]")
            raise typer.Exit(code=1)
        except json.JSONDecodeError as e:
            console.print("[red]❌ Error: Invalid JSON format in parameters file[/red]")
            console.print(f"[yellow]→ File: {params_file}[/yellow]")
            console.print(f"[yellow]→ Details: {e}[/yellow]")
            console.print("[yellow]→ Action: Fix the JSON syntax in your parameters file[/yellow]")
            console.print('[blue]Expected format: {{"param1": "value1", "param2": "value2"}}[/blue]')
            raise typer.Exit(code=1)
    elif params_json:
        try:
            parameters = json.loads(params_json)
            console.print("[blue]✓ Loaded parameters from JSON string[/blue]")
        except json.JSONDecodeError as e:
            console.print("[red]❌ Error: Invalid JSON format in --params string[/red]")
            console.print(f"[yellow]→ Details: {e}[/yellow]")
            console.print("[yellow]→ Action: Check your JSON syntax (quotes, brackets, commas)[/yellow]")
            console.print('[blue]Example: --params \'{{"param1": "value1", "param2": "value2"}}\'[/blue]')
            raise typer.Exit(code=1)
    elif params:
        for param in params:
            # Remove leading dash if present (for backward compatibility)
            if param.startswith("-"):
                param = param[1:]

            if "=" in param:
                key, value = param.split("=", 1)
                parameters[key] = value
            else:
                console.print("[yellow]⚠️  Warning: Skipping invalid parameter format[/yellow]")
                console.print(f"[yellow]→ Invalid: {param}[/yellow]")
                console.print("[yellow]→ Expected format: -p param=value[/yellow]")
                console.print("[blue]Example: -p download_start_date=31/07/2025[/blue]")
        console.print("[blue]✓ Loaded parameters from command-line flags[/blue]")

    console.print(Panel(f"[bold green]Running script: {script_path}[/bold green]", border_style="green"))
    if parameters:
        console.print("[blue]📋 Parameters:[/blue]")
        console.print(f"[blue]{json.dumps(parameters, indent=2)}[/blue]")
    else:
        console.print("[blue]ℹ️  Running script without parameters[/blue]")
        console.print("[dim]Tip: Add parameters with -p, --params, or --params-file[/dim]")

    # set up skyvern context
    from skyvern.forge.sdk.core import skyvern_context  # noqa: PLC0415
    from skyvern.services.script_service import run_script  # noqa: PLC0415

    skyvern_context.set(skyvern_context.SkyvernContext(script_mode=True, ai_mode_override=ai))
    try:
        asyncio.run(run_script(path=script_path, parameters=parameters))
        console.print("✅ [green]Script execution completed successfully![/green]")
    except Exception as e:
        console.print("[red]❌ Error: Script execution failed[/red]")
        console.print(f"[yellow]→ Script: {script_path}[/yellow]")
        console.print(f"[yellow]→ Details: {e}[/yellow]")
        console.print("[yellow]→ Action: Check the error message above and fix any issues in your script[/yellow]")
        raise typer.Exit(code=1)
