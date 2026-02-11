import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import List, Optional

import psutil
import typer
import uvicorn
from dotenv import load_dotenv, set_key
from rich.panel import Panel
from rich.prompt import Confirm

from testcharmvision.cli.console import console
from testcharmvision.cli.utils import start_services
from testcharmvision.config import settings
from testcharmvision.forge.sdk.core import testcharmvision_context
from testcharmvision.forge.sdk.forge_log import setup_logger
from testcharmvision.services.script_service import run_script
from testcharmvision.utils import detect_os
from testcharmvision.utils.env_paths import resolve_backend_env_path, resolve_frontend_env_path

run_app = typer.Typer(help="Commands to run Testcharmvision services such as the API server or UI.")


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
    """Run the Testcharmvision API server."""
    load_dotenv(resolve_backend_env_path())
    from testcharmvision.config import settings  # noqa: PLC0415

    port = settings.PORT
    console.print(Panel(f"[bold green]Starting Testcharmvision API Server on port {port}...", border_style="green"))
    uvicorn.run(
        "testcharmvision.forge.api_app:create_api_app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        factory=True,
    )


@run_app.command(name="ui")
def run_ui() -> None:
    """Run the Testcharmvision UI server."""
    console.print(Panel("[bold blue]Starting Testcharmvision UI Server...[/bold blue]", border_style="blue"))
    try:
        with console.status("[bold green]Checking for existing process on port 8080...") as status:
            pids = get_pids_on_port(8080)
            if pids:
                status.stop()
                response = Confirm.ask("Process already running on port 8080. [yellow]Kill it?[/yellow]")
                if response:
                    kill_pids(pids)
                    console.print("✅ [green]Process killed.[/green]")
                else:
                    console.print("[yellow]UI server not started. Process already running on port 8080.[/yellow]")
                    return
            status.stop()
    except Exception as e:  # pragma: no cover - CLI safeguards
        console.print(f"[red]Error checking for process: {e}[/red]")

    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path is None:
        console.print("[bold red]ERROR: Testcharmvision Frontend directory not found.[/bold red]")
        return

    frontend_dir = frontend_env_path.parent
    if not frontend_env_path.exists():
        console.print("[bold blue]Setting up frontend .env file...[/bold blue]")
        shutil.copy(frontend_dir / ".env.example", frontend_env_path)
        console.print("✅ [green]Successfully set up frontend .env file[/green]")

    backend_env_path = resolve_backend_env_path()
    if backend_env_path.exists():
        load_dotenv(backend_env_path)
        testcharmvision_api_key = os.getenv("TESTCHARMVISION_API_KEY")
        if testcharmvision_api_key:
            set_key(frontend_env_path, "VITE_TESTCHARMVISION_API_KEY", testcharmvision_api_key)
        else:
            console.print("[red]ERROR: TESTCHARMVISION_API_KEY not found in .env file[/red]")
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
def run_ui_dev() -> None:
    """Run the Testcharmvision UI server in development mode (npm run start-local)."""
    console.print(Panel("[bold blue]Starting Testcharmvision UI Server (dev mode)...[/bold blue]", border_style="blue"))
    try:
        with console.status("[bold green]Checking for existing process on port 8080...") as status:
            pids = get_pids_on_port(8080)
            if pids:
                status.stop()
                response = Confirm.ask("Process already running on port 8080. [yellow]Kill it?[/yellow]")
                if response:
                    kill_pids(pids)
                    console.print("✅ [green]Process killed.[/green]")
                else:
                    console.print("[yellow]UI server not started. Process already running on port 8080.[/yellow]")
                    return
            status.stop()
    except Exception as e:  # pragma: no cover - CLI safeguards
        console.print(f"[red]Error checking for process: {e}[/red]")

    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path is None:
        console.print("[bold red]ERROR: Testcharmvision Frontend directory not found.[/bold red]")
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
    """Run the Testcharmvision API server and UI server in parallel."""
    asyncio.run(start_services())


@run_app.command(name="dev")
def run_dev() -> None:
    """Run the Testcharmvision API server and UI server in the background (detached).

    This command starts both services and immediately returns control to your terminal.
    Use 'testcharmvision stop all' to stop the services.
    """
    load_dotenv(resolve_backend_env_path())
    from testcharmvision.config import settings as testcharmvision_settings  # noqa: PLC0415

    console.print(Panel("[bold green]Starting Testcharmvision in development mode...[/bold green]", border_style="green"))

    # Start server in background (detached) - call uvicorn directly
    server_process = subprocess.Popen(
        [
            "uvicorn",
            "testcharmvision.forge.api_app:create_api_app",
            "--host",
            "0.0.0.0",
            "--port",
            str(testcharmvision_settings.PORT),
            "--factory",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    console.print(f"✅ [green]Server started in background (PID: {server_process.pid})[/green]")

    # Start UI (dev mode) in background (detached) - call npm directly
    frontend_env_path = resolve_frontend_env_path()
    if frontend_env_path is None:
        console.print("[bold red]ERROR: Testcharmvision Frontend directory not found.[/bold red]")
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

    console.print("\n🎉 [bold green]Testcharmvision is starting![/bold green]")
    console.print(f"🌐 [bold]API server:[/bold] [cyan]http://localhost:{testcharmvision_settings.PORT}[/cyan]")
    console.print("🖥️  [bold]UI:[/bold] [cyan]http://localhost:8080[/cyan]")
    console.print("\n[dim]Use 'testcharmvision stop all' to stop the services.[/dim]")


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
        testcharmvision run code main.py --params-file params.json

    2. JSON string:
        testcharmvision run code main.py --params '{"param1": "val1", "param2": "val2"}'

    3. Individual flags (lowest priority):
        testcharmvision run code main.py -p param1=val1 -p param2=val2

    Note: For backward compatibility, leading dashes in -p values are automatically stripped.
    """
    # Disable LiteLLM loggers
    os.environ["LITELLM_LOG"] = "CRITICAL"
    import litellm  # noqa: PLC0415

    litellm.suppress_debug_info = True
    litellm.set_verbose = False

    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    logging.getLogger("LiteLLM Router").setLevel(logging.CRITICAL)
    logging.getLogger("LiteLLM Proxy").setLevel(logging.CRITICAL)
    settings.LOG_LEVEL = "CRITICAL"
    setup_logger()

    # Validate script path
    if not script_path:
        console.print("[red]❌ Error: No script path provided[/red]")
        console.print("[yellow]→ Action: Provide a path to your Python script[/yellow]")
        console.print("[blue]Example: testcharmvision run code main.py -p param1=value1[/blue]")
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
        console.print("[blue]Example: testcharmvision run code my_script.py[/blue]")
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

    # set up testcharmvision context

    testcharmvision_context.set(testcharmvision_context.TestcharmvisionContext(script_mode=True, ai_mode_override=ai))
    try:
        asyncio.run(run_script(path=script_path, parameters=parameters))
        console.print("✅ [green]Script execution completed successfully![/green]")
    except Exception as e:
        console.print("[red]❌ Error: Script execution failed[/red]")
        console.print(f"[yellow]→ Script: {script_path}[/yellow]")
        console.print(f"[yellow]→ Details: {e}[/yellow]")
        console.print("[yellow]→ Action: Check the error message above and fix any issues in your script[/yellow]")
        raise typer.Exit(code=1)
