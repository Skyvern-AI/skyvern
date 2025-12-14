import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Any, List, Optional

import psutil
import typer
import uvicorn
from dotenv import load_dotenv, set_key
from mcp.server.fastmcp import FastMCP
from rich.panel import Panel
from rich.prompt import Confirm

from skyvern.cli.console import console
from skyvern.cli.utils import start_services
from skyvern.client import SkyvernEnvironment
from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.forge_log import setup_logger
from skyvern.library.skyvern import Skyvern
from skyvern.services.script_service import run_script
from skyvern.utils import detect_os
from skyvern.utils.env_paths import resolve_backend_env_path, resolve_frontend_env_path

run_app = typer.Typer(help="Commands to run Skyvern services such as the API server or UI.")

mcp = FastMCP("Skyvern")


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> dict[str, Any]:
    """Use Skyvern to execute anything in the browser. Useful for accomplishing tasks that require browser automation.

    This tool uses Skyvern's browser automation to navigate websites and perform actions to achieve
    the user's intended outcome. It can handle tasks like form filling, clicking buttons, data extraction,
    and multi-step workflows.

    It can even help you find updated data on the internet if your model information is outdated.

    Args:
        prompt: A natural language description of what needs to be accomplished (e.g. "Book a flight from
               NYC to LA", "Sign up for the newsletter", "Find the price of item X", "Apply to a job")
        url: The starting URL of the website where the task should be performed
    """
    skyvern_agent = Skyvern(
        environment=SkyvernEnvironment.CLOUD,
        base_url=settings.SKYVERN_BASE_URL,
        api_key=settings.SKYVERN_API_KEY,
    )
    res = await skyvern_agent.run_task(prompt=prompt, url=url, user_agent="skyvern-mcp", wait_for_completion=True)

    output = res.model_dump()["output"]
    # Primary: use app_url from API response (handles both task and workflow run IDs correctly)
    if res.app_url:
        task_url = res.app_url
    else:
        # Fallback when app_url is not available (e.g., older API versions)
        # Determine route based on run_id prefix: 'wr_' for workflows, otherwise tasks
        if res.run_id and res.run_id.startswith("wr_"):
            task_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/runs/{res.run_id}/overview"
        else:
            task_url = f"{settings.SKYVERN_APP_URL.rstrip('/')}/tasks/{res.run_id}/actions"
    return {"output": output, "task_url": task_url, "run_id": res.run_id}


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
    load_dotenv(resolve_backend_env_path())
    from skyvern.config import settings  # noqa: PLC0415

    port = settings.PORT
    console.print(Panel(f"[bold green]Starting Skyvern API Server on port {port}...", border_style="green"))
    uvicorn.run(
        "skyvern.forge.api_app:create_api_app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        factory=True,
    )


@run_app.command(name="ui")
def run_ui() -> None:
    """Run the Skyvern UI server."""
    console.print(Panel("[bold blue]Starting Skyvern UI Server...[/bold blue]", border_style="blue"))
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


@run_app.command(name="all")
def run_all() -> None:
    """Run the Skyvern API server and UI server in parallel."""
    asyncio.run(start_services())


@run_app.command(name="mcp")
def run_mcp() -> None:
    """Run the MCP server."""
    # This breaks the MCP processing because it expects json output only
    # console.print(Panel("[bold green]Starting MCP Server...[/bold green]", border_style="green"))
    mcp.run(transport="stdio")


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
