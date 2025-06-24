import typer
from rich.panel import Panel

from .console import console
from .utils import get_pids_on_port, kill_pids

stop_app = typer.Typer(help="Stop Skyvern services.")


@stop_app.command(name="ui")
def stop_ui() -> None:
    """Stop whatever is running on port 8000."""
    port = 8000
    pids = get_pids_on_port(port)
    if not pids:
        console.print(Panel(f"No process found on port {port}", border_style="yellow"))
        return
    kill_pids(pids)
    console.print(Panel(f"Stopped process(es) on port {port}", border_style="red"))
