import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
import requests
import typer
import uvicorn
import yaml
from dotenv import load_dotenv, set_key
from mcp.server.fastmcp import FastMCP
from rich.panel import Panel
from rich.prompt import Confirm

from skyvern.cli.utils import start_services
from skyvern.config import settings
from skyvern.library.skyvern import Skyvern
from skyvern.utils import detect_os

from .console import console

run_app = typer.Typer(help="Commands to run Skyvern services such as the API server or UI.")

mcp = FastMCP("Skyvern")


@mcp.tool()
async def skyvern_run_task(prompt: str, url: str) -> dict[str, str]:
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
        base_url=settings.SKYVERN_BASE_URL,
        api_key=settings.SKYVERN_API_KEY,
    )
    res = await skyvern_agent.run_task(prompt=prompt, url=url, user_agent="skyvern-mcp", wait_for_completion=True)

    # TODO: It would be nice if we could return the task URL here
    output = res.model_dump()["output"]
    base_url = settings.SKYVERN_BASE_URL
    run_history_url = (
        "https://app.skyvern.com/history" if "skyvern.com" in base_url else "http://localhost:8080/history"
    )
    return {"output": output, "run_history_url": run_history_url}


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
    load_dotenv()
    load_dotenv(".env")
    from skyvern.config import settings

    port = settings.PORT
    console.print(Panel(f"[bold green]Starting Skyvern API Server on port {port}...", border_style="green"))
    uvicorn.run(
        "skyvern.forge.api_app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
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
    except Exception as e:
        console.print(f"[red]Error checking for process: {e}[/red]")

    current_dir = Path(__file__).parent.parent.parent
    frontend_dir = current_dir / "skyvern-frontend"
    if not frontend_dir.exists():
        console.print(
            f"[bold red]ERROR: Skyvern Frontend directory not found at [path]{frontend_dir}[/path]. Are you in the right repo?[/bold red]"
        )
        return

    frontend_env_path = frontend_dir / ".env"
    if not frontend_env_path.exists():
        console.print("[bold blue]Setting up frontend .env file...[/bold blue]")
        shutil.copy(frontend_dir / ".env.example", frontend_env_path)
        console.print("✅ [green]Successfully set up frontend .env file[/green]")

    main_env_path = current_dir / ".env"
    if main_env_path.exists():
        load_dotenv(main_env_path)
        skyvern_api_key = os.getenv("SKYVERN_API_KEY")
        if skyvern_api_key:
            set_key(str(frontend_env_path), "VITE_SKYVERN_API_KEY", skyvern_api_key)
        else:
            console.print("[red]ERROR: SKYVERN_API_KEY not found in .env file[/red]")
    else:
        console.print("[red]ERROR: .env file not found[/red]")

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
    console.print(Panel("[bold green]Starting MCP Server...[/bold green]", border_style="green"))
    mcp.run(transport="stdio")


@run_app.command(name="workflow")
def run_workflow(
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Path to local YAML workflow file"),
    workflow_id: Optional[str] = typer.Option(None, "--workflow-id", help="ID of stored workflow to run"),
    data: Optional[str] = typer.Option(None, "--data", help="JSON string of workflow data/parameters"),
    param: Optional[List[str]] = typer.Option(
        None, "--param", help="Workflow parameters in key=value format (can be used multiple times)"
    ),
    watch: bool = typer.Option(False, "--watch", help="Stream run logs until completion"),
    output: bool = typer.Option(False, "--output", help="Output the workflow run ID"),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Title for the workflow run"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Override the workflow max steps"),
    proxy_location: Optional[str] = typer.Option(
        None, "--proxy-location", help="Proxy location for the workflow execution"
    ),
) -> None:
    """
    Execute a workflow programmatically.

    Supports both local YAML workflow files and stored workflows.
    Brings feature parity with the Web UI and REST API for CI/automation use.
    Resolves GitHub issue #2220.

    Examples:

        # Run local workflow file with parameters
        skyvern run workflow -f workflow.yaml --param username=john --param env=prod

        # Run stored workflow with watching
        skyvern run workflow --workflow-id my-workflow --watch --output

        # Use JSON data
        skyvern run workflow -f workflow.yaml --data '{"user": "john", "pass": "secret"}'
    """
    # Input validation
    if not file and not workflow_id:
        console.print("❌ [red]Error: Either --file or --workflow-id must be specified[/red]")
        console.print("\n💡 Use --help for usage examples")
        raise typer.Exit(1)

    if file and workflow_id:
        console.print("❌ [red]Error: Cannot specify both --file and --workflow-id[/red]")
        raise typer.Exit(1)

    try:
        # Parse parameters
        workflow_params = {}
        if param:
            for p in param:
                if "=" not in p:
                    console.print(f"❌ [red]Error: Invalid parameter format '{p}'. Use key=value[/red]")
                    raise typer.Exit(1)
                key, value = p.split("=", 1)
                workflow_params[key.strip()] = value.strip()

        # Parse JSON data if provided
        if data:
            try:
                data_dict = json.loads(data)
                workflow_params.update(data_dict)
            except json.JSONDecodeError as e:
                console.print(f"❌ [red]Error: Invalid JSON in --data: {e}[/red]")
                raise typer.Exit(1)

        # Handle file-based workflow
        if file:
            console.print(f"📁 [blue]Loading workflow from file: {file}[/blue]")
            workflow_data = _load_workflow_file(file)
            file_workflow_id = workflow_data.get("workflow_id") or workflow_data.get("id")
            if not file_workflow_id:
                console.print("❌ [red]Error: Workflow file must contain 'workflow_id' field[/red]")
                console.print("💡 [blue]Add 'workflow_id: your-workflow-id' to your YAML file[/blue]")
                raise typer.Exit(1)
            workflow_id = file_workflow_id

        # Get API settings
        load_dotenv()
        load_dotenv(".env")
        api_key_value = api_key or os.getenv("SKYVERN_API_KEY", "skyvern-local-dev-key-123")
        base_url = "http://localhost:8000"  # Always use local server for CLI

        # Execute workflow via simple HTTP API
        console.print("🚀 [bold green]Starting workflow execution...[/bold green]")
        console.print(f"🔍 [blue]Running workflow: {workflow_id}[/blue]")
        console.print(f"🌐 [blue]Using server: {base_url}[/blue]")

        # Prepare request
        headers = {"Authorization": f"Bearer {api_key_value}", "Content-Type": "application/json"}

        payload: Dict[str, Any] = {"parameters": workflow_params}
        if title:
            payload["title"] = title
        if max_steps is not None:
            payload["max_steps_override"] = max_steps
        if proxy_location is not None:
            payload["proxy_location"] = proxy_location

        try:
            # Make API call
            response = requests.post(
                f"{base_url}/api/v1/workflows/{workflow_id}/run", headers=headers, json=payload, timeout=30
            )

            if response.status_code == 200:
                run_data = response.json()
                run_id = run_data.get("workflow_run_id") or run_data.get("run_id") or "unknown"

                if output:
                    console.print(run_id)
                else:
                    console.print(
                        Panel(
                            f"✅ Started workflow run [bold green]{run_id}[/bold green]",
                            border_style="green",
                        )
                    )

                if watch:
                    console.print("👀 [blue]Workflow started successfully![/blue]")
                    console.print(f"🔗 [blue]Monitor at: http://localhost:8080/workflows/runs/{run_id}[/blue]")

            elif response.status_code == 401:
                console.print("❌ [red]401 Unauthorized - Check your API key[/red]")
                console.print("💡 [blue]Set SKYVERN_API_KEY in .env file[/blue]")
                raise typer.Exit(1)
            elif response.status_code == 403:
                console.print("❌ [red]403 Forbidden - Check workflow permissions[/red]")
                raise typer.Exit(1)
            elif response.status_code == 404:
                console.print(f"❌ [red]404 Not Found - Workflow '{workflow_id}' does not exist[/red]")
                console.print("💡 [blue]Check available workflows: python -m skyvern workflow list[/blue]")
                raise typer.Exit(1)
            else:
                console.print(f"❌ [red]API Error {response.status_code}: {response.text}[/red]")
                raise typer.Exit(1)

        except requests.exceptions.ConnectionError:
            console.print("❌ [red]Cannot connect to Skyvern server[/red]")
            console.print("💡 [blue]Start server: python -m skyvern run server[/blue]")
            console.print("💡 [blue]Check if server is running: curl http://localhost:8000[/blue]")
            raise typer.Exit(1)
        except requests.exceptions.Timeout:
            console.print("❌ [red]Request timeout - server may be slow[/red]")
            raise typer.Exit(1)
        except Exception as e:
            console.print(f"❌ [red]Request error: {e}[/red]")
            raise typer.Exit(1)

    except FileNotFoundError as e:
        console.print(f"❌ [red]Workflow file not found: {e}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


def _load_workflow_file(file_path: str) -> Dict[str, Any]:
    """Load and validate a YAML workflow file."""
    file_path_obj = Path(file_path)

    if not file_path_obj.exists():
        raise FileNotFoundError(f"'{file_path}' not found")

    if file_path_obj.suffix.lower() not in [".yaml", ".yml"]:
        raise ValueError("File must be YAML (.yaml or .yml)")

    try:
        with open(file_path_obj, "r", encoding="utf-8") as f:
            workflow_data = yaml.safe_load(f)

        if not isinstance(workflow_data, dict):
            raise ValueError("YAML file must contain a dictionary")

        console.print("✅ [green]Workflow file loaded successfully[/green]")
        return workflow_data

    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML syntax: {e}")
