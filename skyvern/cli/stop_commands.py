import subprocess
from typing import List

import psutil
import typer
from rich.panel import Panel

from .commands._output import emit_tool_result
from .console import console

stop_app = typer.Typer(help="Commands to stop Skyvern services.")


def _emit_stop_result(
    *,
    action: str,
    payload: dict,
    stopped: bool,
    not_found_message: str,
) -> None:
    emit_tool_result(
        {
            "ok": stopped,
            "action": action,
            "data": payload,
            "error": None if stopped else {"message": not_found_message, "hint": ""},
        },
        json_output=True,
    )


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


def kill_pids(pids: List[int], service_name: str, *, quiet: bool = False) -> bool:
    """Kill the given list of PIDs in a cross-platform way."""
    if not pids:
        if not quiet:
            console.print(f"[yellow]No {service_name} processes found.[/yellow]")
        return False

    killed_any = False
    for pid in pids:
        try:
            process = psutil.Process(pid)
            process.terminate()

            process_stopped = False
            try:
                process.wait(timeout=3)
                process_stopped = True
            except psutil.TimeoutExpired:
                if not quiet:
                    console.print(f"[yellow]Process {pid} didn't terminate gracefully, forcing kill...[/yellow]")
                process.kill()
                try:
                    process.wait(timeout=3)
                    process_stopped = True
                except psutil.TimeoutExpired:
                    if not quiet:
                        console.print(f"[red]Process {pid} remains unresponsive even after force kill[/red]")

            if process_stopped:
                killed_any = True
                if not quiet:
                    console.print(f"[green]Stopped {service_name} process (PID: {pid})[/green]")
            else:
                if not quiet:
                    console.print(f"[red]Failed to stop {service_name} process (PID: {pid})[/red]")
        except psutil.NoSuchProcess:
            if not quiet:
                console.print(f"[yellow]Process {pid} was already stopped[/yellow]")
        except psutil.AccessDenied:
            if not quiet:
                console.print(f"[red]Access denied when trying to stop process {pid}[/red]")
        except Exception as e:
            if not quiet:
                console.print(f"[red]Failed to stop process {pid}: {e}[/red]")

    return killed_any


@stop_app.command(name="all")
def stop_all(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Stop all Skyvern services running on ports 8000, 8080, and 9090.

    Examples:
      skyvern stop all
      skyvern stop all --json
    """
    if not json_output:
        console.print(Panel("[bold red]Stopping All Skyvern Services...[/bold red]", border_style="red"))

    pids_8000 = get_pids_on_port(8000)
    killed_8000 = kill_pids(pids_8000, "Skyvern API server (port 8000)", quiet=json_output)

    pids_8080 = get_pids_on_port(8080)
    killed_8080 = kill_pids(pids_8080, "Skyvern UI server (port 8080)", quiet=json_output)

    pids_9090 = get_pids_on_port(9090)
    killed_9090 = kill_pids(pids_9090, "Skyvern UI server (port 9090)", quiet=json_output)

    stopped = killed_8000 or killed_8080 or killed_9090
    not_found_message = "No Skyvern services found running on ports 8000, 8080, or 9090."
    if json_output:
        _emit_stop_result(
            action="stop.all",
            payload={"stopped": stopped, "services": ["api", "ui", "ui-dev"]},
            stopped=stopped,
            not_found_message=not_found_message,
        )
    elif stopped:
        console.print("[green]All Skyvern services stopped successfully.[/green]")
    else:
        console.print(f"[yellow]{not_found_message}[/yellow]")


@stop_app.command(name="ui")
def stop_ui(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Stop the Skyvern UI servers running on ports 8080 and 9090."""
    if not json_output:
        console.print(Panel("[bold red]Stopping Skyvern UI Servers...[/bold red]", border_style="red"))

    pids_8080 = get_pids_on_port(8080)
    killed_8080 = kill_pids(pids_8080, "Skyvern UI server (port 8080)", quiet=json_output)

    pids_9090 = get_pids_on_port(9090)
    killed_9090 = kill_pids(pids_9090, "Skyvern UI server (port 9090)", quiet=json_output)

    stopped = killed_8080 or killed_9090
    not_found_message = "No Skyvern UI servers found running on ports 8080 or 9090."
    if json_output:
        _emit_stop_result(
            action="stop.ui",
            payload={"stopped": stopped, "services": ["ui", "ui-dev"]},
            stopped=stopped,
            not_found_message=not_found_message,
        )
    elif stopped:
        console.print("[green]Skyvern UI servers stopped successfully.[/green]")
    else:
        console.print(f"[yellow]{not_found_message}[/yellow]")


@stop_app.command(name="server")
def stop_server(
    port: int = typer.Option(8000, "--port", "-p", help="Port number for the Skyvern API server"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Stop the Skyvern API server running on the specified port (default: 8000)."""
    if not json_output:
        console.print(Panel(f"[bold red]Stopping Skyvern API Server (port {port})...[/bold red]", border_style="red"))

    pids = get_pids_on_port(port)
    stopped = kill_pids(pids, f"Skyvern API server (port {port})", quiet=json_output)
    not_found_message = f"No Skyvern API server found running on port {port}."
    if json_output:
        _emit_stop_result(
            action="stop.server",
            payload={"stopped": stopped, "port": port},
            stopped=stopped,
            not_found_message=not_found_message,
        )
    elif stopped:
        console.print(f"[green]Skyvern API server on port {port} stopped successfully.[/green]")
    else:
        console.print(f"[yellow]{not_found_message}[/yellow]")


@stop_app.command(name="docker")
def stop_docker() -> None:
    """Stop Skyvern Docker Compose services.

    Examples:
      skyvern stop docker
    """
    console.print(Panel("[bold red]Stopping Skyvern Docker Compose...[/bold red]", border_style="red"))
    try:
        subprocess.run(["docker", "compose", "down"], check=True)
        console.print("[green]Skyvern Docker Compose services stopped.[/green]")
    except FileNotFoundError:
        console.print("[bold red]Docker is not installed.[/bold red]")
        raise typer.Exit(1)
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Failed to stop Docker Compose: {e}[/bold red]")
        raise typer.Exit(1)
