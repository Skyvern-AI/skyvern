import os
import subprocess
import typer
from typing import List

import psutil
from rich.panel import Panel

from .console import console

stop_app = typer.Typer(help="Commands to stop Skyvern services.")


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


def kill_pids(pids: List[int], service_name: str) -> bool:
    """Kill the given list of PIDs in a cross-platform way."""
    if not pids:
        console.print(f"[yellow]No {service_name} processes found.[/yellow]")
        return False
    
    killed_any = False
    for pid in pids:
        try:
            # Use psutil for cross-platform process killing
            process = psutil.Process(pid)
            process.terminate()
            killed_any = True
            console.print(f"[green]✅ Stopped {service_name} process (PID: {pid})[/green]")
        except psutil.NoSuchProcess:
            console.print(f"[yellow]Process {pid} was already stopped[/yellow]")
        except psutil.AccessDenied:
            console.print(f"[red]Access denied when trying to stop process {pid}[/red]")
        except Exception as e:
            console.print(f"[red]Failed to stop process {pid}: {e}[/red]")
    
    return killed_any


@stop_app.command(name="server")
def stop_server() -> None:
    """Stop the Skyvern API server running on port 8000."""
    console.print(Panel("[bold red]Stopping Skyvern API Server...[/bold red]", border_style="red"))
    
    pids = get_pids_on_port(8000)
    if kill_pids(pids, "Skyvern API server"):
        console.print("[green]🛑 Skyvern API server stopped successfully.[/green]")
    else:
        console.print("[yellow]No Skyvern API server found running on port 8000.[/yellow]") 