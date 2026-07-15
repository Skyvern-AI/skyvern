import _thread
import asyncio
import atexit
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Annotated, Any, List, Literal, Optional, cast

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

import psutil
import typer
import uvicorn
from dotenv import set_key
from rich.panel import Panel
from rich.prompt import Confirm
from starlette.middleware import Middleware
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.responses import Response as StarletteResponse

from skyvern._cli_bootstrap import prepare_cli_runtime
from skyvern.cli.commands._output import output_error
from skyvern.cli.commands._tty import is_interactive
from skyvern.cli.console import console
from skyvern.cli.core.result import set_concise_responses
from skyvern.utils import detect_os
from skyvern.utils.env_paths import (
    EnvIntent,
    resolve_backend_env_path,
    resolve_frontend_env_path,
)

run_app = typer.Typer(help="Commands to run Skyvern services such as the API server or UI.")
_mcp_cleanup_done = False
_mcp_cleanup_in_progress = False
_mcp_eof_shutdown_requested = False
_MCP_GRACEFUL_CLEANUP_TIMEOUT_SECONDS = 5.0
_MCP_PROCESS_KILL_TIMEOUT_SECONDS = 2.0
_MCP_NATIVE_EOF_GRACE_SECONDS = 0.25
# The EOF watcher's os._exit(0) preempts cleanup unconditionally, so this must exceed
# the cloud path's graceful join plus process-kill wait.
_MCP_EOF_SHUTDOWN_TIMEOUT_SECONDS = 10.0


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
    global _mcp_cleanup_done, _mcp_cleanup_in_progress
    # CPython runs signal handlers on the main thread, and all callers stay on that thread.
    if _mcp_cleanup_done or _mcp_cleanup_in_progress:
        return
    _mcp_cleanup_in_progress = True

    try:
        logger = logging.getLogger(__name__)
        try:
            local_browser_identity = _current_local_browser_identity()
        except Exception:
            logger.warning("Failed to identify the local MCP browser", exc_info=True)
            local_browser_identity = None
        # In stdio, this main-thread read and a cleanup thread both resolve _global_session because
        # ContextVars are not inherited. The exclusive mode means local identity cannot hide a cloud close.
        if local_browser_identity is not None:
            try:
                # Playwright objects belong to mcp.run's closed loop; a fresh-loop close hangs forever.
                # The owned profile is an anonymous mkdtemp throwaway, so clean it up directly.
                try:
                    _kill_local_browser_process_tree(local_browser_identity[0], local_browser_identity[1])
                finally:
                    if local_browser_identity[2] and local_browser_identity[1]:
                        shutil.rmtree(local_browser_identity[1], ignore_errors=True)
            except Exception:
                logger.warning("MCP local browser cleanup failed", exc_info=True)
        else:
            cleanup_errors: list[BaseException] = []

            def run_cleanup() -> None:
                try:
                    asyncio.run(_cleanup_mcp_resources())
                except BaseException as exc:
                    cleanup_errors.append(exc)

            cleanup_thread = threading.Thread(target=run_cleanup, name="skyvern-mcp-cleanup", daemon=True)
            cleanup_thread.start()
            cleanup_thread.join(_MCP_GRACEFUL_CLEANUP_TIMEOUT_SECONDS)
            if cleanup_thread.is_alive():
                logger.warning("MCP graceful cleanup timed out")
            elif cleanup_errors:
                error = cleanup_errors[0]
                logger.warning("MCP graceful cleanup failed", exc_info=(type(error), error, error.__traceback__))
    finally:
        _mcp_cleanup_done = True
        _mcp_cleanup_in_progress = False


def _cleanup_mcp_resources_sync() -> None:
    """Atexit callback for MCP cleanup."""
    _cleanup_mcp_resources_blocking()


def _current_local_browser_identity() -> tuple[int | None, str | None, bool] | None:
    from skyvern.cli.core.session_manager import get_current_session  # noqa: PLC0415

    current = get_current_session()
    if current.context is None or current.context.mode != "local" or current.browser is None:
        return None
    return (
        current.browser.local_cdp_port,
        current.browser.local_user_data_dir,
        current.browser.local_user_data_dir_owned,
    )


def _find_local_browser_processes(port: int | None, user_data_dir: str | None) -> list[psutil.Process]:
    if user_data_dir is None:
        return []

    processes: dict[int, psutil.Process] = {}
    user_data_arg = f"--user-data-dir={user_data_dir}"
    try:
        for process in psutil.process_iter(["pid", "cmdline"]):
            try:
                if user_data_arg in ((process.info or {}).get("cmdline") or []):
                    processes[process.pid] = process
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except Exception:
        logging.getLogger(__name__).warning("Failed to inspect local browser processes", exc_info=True)
    return list(processes.values())


def _local_browser_process_tree(port: int | None, user_data_dir: str | None) -> list[psutil.Process]:
    processes: dict[int, psutil.Process] = {}
    # The Playwright node driver is Chromium's parent, not its descendant; stdin EOF reaps that driver.
    for root in _find_local_browser_processes(port, user_data_dir):
        processes[root.pid] = root
        try:
            processes.update({process.pid: process for process in root.children(recursive=True)})
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return list(processes.values())


def _kill_local_browser_process_tree(
    port: int | None,
    user_data_dir: str | None,
    *,
    known_processes: list[psutil.Process] | None = None,
) -> None:
    if user_data_dir is None:
        return

    processes = {process.pid: process for process in known_processes or []}
    processes.update({process.pid: process for process in _local_browser_process_tree(port, user_data_dir)})

    for process in processes.values():
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    if processes:
        psutil.wait_procs(list(processes.values()), timeout=_MCP_PROCESS_KILL_TIMEOUT_SECONDS)


def _watch_stdin_eof(
    stop: threading.Event,
    shutdown_complete: threading.Event,
    *,
    stdin_fd: int | None = None,
    request_shutdown: Any | None = None,
    force_exit: Any | None = None,
    native_eof_grace: float = _MCP_NATIVE_EOF_GRACE_SECONDS,
    shutdown_timeout: float = _MCP_EOF_SHUTDOWN_TIMEOUT_SECONDS,
) -> None:
    def request_bounded_shutdown() -> None:
        global _mcp_eof_shutdown_requested
        if shutdown_complete.wait(native_eof_grace) or stop.is_set():
            return
        _mcp_eof_shutdown_requested = True
        (request_shutdown or _thread.interrupt_main)()
        if shutdown_complete.wait(shutdown_timeout):
            return
        try:
            local_browser_identity = _current_local_browser_identity()
            if local_browser_identity is not None:
                try:
                    _kill_local_browser_process_tree(local_browser_identity[0], local_browser_identity[1])
                finally:
                    if local_browser_identity[2] and local_browser_identity[1]:
                        shutil.rmtree(local_browser_identity[1], ignore_errors=True)
        except Exception:
            logging.getLogger(__name__).warning("MCP EOF fallback cleanup failed", exc_info=True)
        (force_exit or os._exit)(0)

    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            shutdown_events = select.POLLHUP | select.POLLERR | select.POLLNVAL
            poller.register(sys.stdin.fileno() if stdin_fd is None else stdin_fd, shutdown_events)
            while not stop.is_set():
                if any(event & shutdown_events for _fd, event in poller.poll(100)):
                    if not stop.is_set():
                        request_bounded_shutdown()
                    return
            return

        peek = cast(Any, sys.stdin.buffer).peek
        while not stop.is_set():  # pragma: no cover - Windows pipe fallback
            if not peek(1):
                if not stop.is_set():
                    request_bounded_shutdown()
                return
            stop.wait(0.05)
    except (AttributeError, OSError, ValueError):
        logging.getLogger(__name__).warning("MCP stdin EOF watcher failed", exc_info=True)


def _start_stdin_eof_watcher() -> tuple[threading.Event, threading.Event]:
    stop, shutdown_complete = threading.Event(), threading.Event()
    threading.Thread(
        target=_watch_stdin_eof,
        args=(stop, shutdown_complete),
        name="skyvern-mcp-stdin-eof",
        daemon=True,
    ).start()
    return stop, shutdown_complete


def _handle_mcp_shutdown_signal(_signum: int, _frame: Any) -> None:
    if _mcp_cleanup_in_progress:
        return
    try:
        _cleanup_mcp_resources_blocking()
    finally:
        # Exit 0 only for the EOF watcher's synthetic SIGINT; a real SIGTERM keeps 143.
        eof_initiated = _mcp_eof_shutdown_requested and _signum == signal.SIGINT
        os._exit(0 if eof_initiated else 128 + _signum)


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

    prepare_cli_runtime(intent=EnvIntent.SERVER)
    from skyvern.config import settings  # noqa: PLC0415

    port = settings.PORT
    console.print(Panel(f"[bold green]Starting Skyvern API Server on port {port}...", border_style="green"))
    uvicorn.run(
        "skyvern.forge.api_app:create_api_app",
        host=_default_host(),
        port=port,
        # Omit log_level= so uvicorn.Config.configure_logging() doesn't reset setup_logger()'s uvicorn.error WARNING.
        access_log=False,
        log_config={"version": 1, "disable_existing_loggers": False},
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

    backend_env_path = resolve_backend_env_path(intent=EnvIntent.SERVER)
    if backend_env_path.exists():
        prepare_cli_runtime(intent=EnvIntent.SERVER)
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


@run_app.command(name="docker")
def run_docker() -> None:
    """Start Skyvern via Docker Compose.

    Runs 'docker compose up -d' using the docker-compose.yml in the current
    directory (or the Skyvern package root). Use 'skyvern stop docker' or
    'docker compose down' to stop.

    Examples:
      skyvern run docker
    """
    from pathlib import Path  # noqa: PLC0415

    compose_file = None
    for name in ("docker-compose.yml", "docker-compose.yaml"):
        if Path(name).exists():
            compose_file = name
            break

    if compose_file is None:
        console.print(
            Panel(
                "[bold red]docker-compose.yml not found in current directory.[/bold red]\n"
                "Please run this command from the Skyvern repository root, or clone it first:\n"
                "[cyan]git clone https://github.com/skyvern-ai/skyvern.git && cd skyvern[/cyan]",
                border_style="red",
            )
        )
        raise typer.Exit(1)

    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        console.print("[bold red]Docker is not running.[/bold red] Please start Docker Desktop and try again.")
        raise typer.Exit(1)

    # Ensure frontend .env exists (docker-compose.yml references it via env_file)
    frontend_env = Path("skyvern-frontend/.env")
    frontend_example = Path("skyvern-frontend/.env.example")
    if not frontend_env.exists() and frontend_example.exists():
        import shutil  # noqa: PLC0415

        shutil.copy(frontend_example, frontend_env)
        console.print("✅ [green]Created skyvern-frontend/.env from .env.example[/green]")

    console.print(Panel("[bold green]Starting Skyvern via Docker Compose...[/bold green]", border_style="green"))
    try:
        subprocess.run(["docker", "compose", "up", "-d"], check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Docker Compose failed: {e}[/bold red]")
        raise typer.Exit(1)

    from skyvern.cli.utils import wait_for_docker_services  # noqa: PLC0415

    if wait_for_docker_services():
        console.print(
            Panel(
                "[bold green]Skyvern is ready![/bold green]\n\n"
                "🌐 [bold]UI:[/bold] [cyan]http://localhost:8080[/cyan]\n"
                "🔌 [bold]API:[/bold] [cyan]http://localhost:8000[/cyan]\n\n"
                "To stop: [cyan]skyvern stop docker[/cyan] or [cyan]docker compose down[/cyan]",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                "[yellow]Services are still starting up.[/yellow]\n\n"
                "🌐 [bold]UI:[/bold] [cyan]http://localhost:8080[/cyan] (check back shortly)\n"
                "🔌 [bold]API:[/bold] [cyan]http://localhost:8000[/cyan]\n\n"
                "Run [cyan]docker compose logs -f[/cyan] to monitor progress.\n"
                "To stop: [cyan]skyvern stop docker[/cyan] or [cyan]docker compose down[/cyan]",
                border_style="yellow",
            )
        )


@run_app.command(name="all")
def run_all() -> None:
    """Run the Skyvern API server and UI server in parallel."""
    from skyvern.cli.utils import start_services  # noqa: PLC0415

    asyncio.run(start_services())


@run_app.command(name="dev")
def run_dev() -> None:
    """Run the Skyvern API server and UI server in the background (detached).

    This command starts both services and immediately returns control to your terminal.
    Use 'skyvern stop all' to stop the services.
    """
    prepare_cli_runtime(intent=EnvIntent.SERVER)
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
    global _mcp_eof_shutdown_requested
    _mcp_eof_shutdown_requested = False
    prepare_cli_runtime(intent=EnvIntent.CLOUD)
    from skyvern.cli.core.mcp_http_auth import MCPAPIKeyMiddleware  # noqa: PLC0415
    from skyvern.cli.core.session_manager import set_stateless_http_mode  # noqa: PLC0415
    from skyvern.cli.mcp_tools import mcp  # noqa: PLC0415
    from skyvern.cli.mcp_tools.telemetry import configure_mcp_telemetry_runtime  # noqa: PLC0415

    path = _normalize_mcp_path(path)
    stateless_http_enabled = transport != "stdio" and stateless_http
    configure_mcp_telemetry_runtime(server_mode="local_cli", transport=transport)
    # EOF dispatches the SIGINT cleanup handler; finally covers normal returns, with atexit as the last backstop.
    atexit.register(_cleanup_mcp_resources_sync)
    set_stateless_http_mode(stateless_http_enabled)
    set_concise_responses(not verbose)
    eof_watcher_stop: threading.Event | None = None
    shutdown_complete: threading.Event | None = None
    original_signal_handlers: dict[signal.Signals, Any] = {}
    try:
        if transport == "stdio":
            original_signal_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, _handle_mcp_shutdown_signal)
            original_signal_handlers[signal.SIGTERM] = signal.signal(signal.SIGTERM, _handle_mcp_shutdown_signal)
            eof_watcher_stop, shutdown_complete = _start_stdin_eof_watcher()
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
        if eof_watcher_stop is not None:
            eof_watcher_stop.set()
        try:
            set_stateless_http_mode(False)
            set_concise_responses(False)
            _cleanup_mcp_resources_blocking()
        finally:
            for handled_signal, original_handler in original_signal_handlers.items():
                signal.signal(handled_signal, original_handler)
            if shutdown_complete is not None:
                shutdown_complete.set()


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

    from skyvern.config import settings  # noqa: PLC0415

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
