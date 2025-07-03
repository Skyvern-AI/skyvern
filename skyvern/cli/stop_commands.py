from typing import List

import psutil
import typer
from rich.panel import Panel

from .console import console

stop_app = typer.Typer(help="Commands to stop Skyvern services.")


def get_pids_on_port(port: int) -> List[int]:
    """Return a list of PIDs listening on the given port."""
    pids = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.pid and conn.status == psutil.CONN_LISTEN:
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

            # Wait for the process to exit, use kill() as fallback
            process_stopped = False
            try:
                process.wait(timeout=3)
                process_stopped = True
            except psutil.TimeoutExpired:
                console.print(f"[yellow]Process {pid} didn't terminate gracefully, forcing kill...[/yellow]")
                process.kill()
                try:
                    process.wait(timeout=3)
                    process_stopped = True
                except psutil.TimeoutExpired:
                    console.print(f"[red]Process {pid} remains unresponsive even after force kill[/red]")

            if process_stopped:
                killed_any = True
                console.print(f"[green]✅ Stopped {service_name} process (PID: {pid})[/green]")
            else:
                console.print(f"[red]❌ Failed to stop {service_name} process (PID: {pid})[/red]")
        except psutil.NoSuchProcess:
            console.print(f"[yellow]Process {pid} was already stopped[/yellow]")
        except psutil.AccessDenied:
            console.print(f"[red]Access denied when trying to stop process {pid}[/red]")
        except Exception as e:
            console.print(f"[red]Failed to stop process {pid}: {e}[/red]")

    return killed_any


@stop_app.command(name="all")
def stop_all() -> None:
    """Stop all Skyvern services running on ports 8000, 8080, and 9090."""
    console.print(Panel("[bold red]Stopping All Skyvern Services...[/bold red]", border_style="red"))

    # Stop processes on port 8000 (API server)
    pids_8000 = get_pids_on_port(8000)
    killed_8000 = kill_pids(pids_8000, "Skyvern API server (port 8000)")

    # Stop processes on port 8080 (UI server)
    pids_8080 = get_pids_on_port(8080)
    killed_8080 = kill_pids(pids_8080, "Skyvern UI server (port 8080)")

    # Stop processes on port 9090 (UI server)
    pids_9090 = get_pids_on_port(9090)
    killed_9090 = kill_pids(pids_9090, "Skyvern UI server (port 9090)")

    if killed_8000 or killed_8080 or killed_9090:
        console.print("[green]🛑 All Skyvern services stopped successfully.[/green]")
    else:
        console.print("[yellow]No Skyvern services found running on ports 8000, 8080, or 9090.[/yellow]")


@stop_app.command(name="ui")
def stop_ui() -> None:
    """Stop the Skyvern UI servers running on ports 8080 and 9090."""
    console.print(Panel("[bold red]Stopping Skyvern UI Servers...[/bold red]", border_style="red"))

    # Stop processes on port 8080
    pids_8080 = get_pids_on_port(8080)
    killed_8080 = kill_pids(pids_8080, "Skyvern UI server (port 8080)")

    # Stop processes on port 9090
    pids_9090 = get_pids_on_port(9090)
    killed_9090 = kill_pids(pids_9090, "Skyvern UI server (port 9090)")

    if killed_8080 or killed_9090:
        console.print("[green]🛑 Skyvern UI servers stopped successfully.[/green]")
    else:
        console.print("[yellow]No Skyvern UI servers found running on ports 8080 or 9090.[/yellow]")


@stop_app.command(name="server")
def stop_server(port: int = typer.Option(8000, "--port", "-p", help="Port number for the Skyvern API server")) -> None:
    """Stop the Skyvern API server running on the specified port (default: 8000)."""
    console.print(Panel(f"[bold red]Stopping Skyvern API Server (port {port})...[/bold red]", border_style="red"))

    pids = get_pids_on_port(port)
    if kill_pids(pids, f"Skyvern API server (port {port})"):
        console.print(f"[green]🛑 Skyvern API server on port {port} stopped successfully.[/green]")
    else:
        console.print(f"[yellow]No Skyvern API server found running on port {port}.[/yellow]")
