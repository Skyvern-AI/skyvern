import os
import shutil
import subprocess
from pathlib import Path
from typing import List

import psutil
from skyvern.utils import detect_os

import typer
import uvicorn
from dotenv import load_dotenv, set_key
from mcp.server.fastmcp import FastMCP
from rich.panel import Panel
from rich.prompt import Confirm

from .console import console

run_app = typer.Typer()

mcp = FastMCP("Skyvern")


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
    except Exception as e:  # pragma: no cover - CLI safeguards
        console.print(f"[red]Error checking for process: {e}[/red]")

    current_dir = Path(__file__).parent.parent.parent
    frontend_dir = current_dir / "skyvern-frontend"
    if not frontend_dir.exists():
        console.print(
            f"[bold red]ERROR: Skyvern Frontend directory not found at [path]{frontend_dir}[/path]. Are you in the right repo?[/bold red]"
        )
        return

    if not (frontend_dir / ".env").exists():
        console.print("[bold blue]Setting up frontend .env file...[/bold blue]")
        shutil.copy(frontend_dir / ".env.example", frontend_dir / ".env")
        main_env_path = current_dir / ".env"
        if main_env_path.exists():
            load_dotenv(main_env_path)
            skyvern_api_key = os.getenv("SKYVERN_API_KEY")
            if skyvern_api_key:
                frontend_env_path = frontend_dir / ".env"
                set_key(str(frontend_env_path), "VITE_SKYVERN_API_KEY", skyvern_api_key)
            else:
                console.print("[red]ERROR: SKYVERN_API_KEY not found in .env file[/red]")
        else:
            console.print("[red]ERROR: .env file not found[/red]")

        console.print("✅ [green]Successfully set up frontend .env file[/green]")

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


@run_app.command(name="mcp")
def run_mcp() -> None:
    """Run the MCP server."""
    console.print(Panel("[bold green]Starting MCP Server...[/bold green]", border_style="green"))
    mcp.run(transport="stdio")
