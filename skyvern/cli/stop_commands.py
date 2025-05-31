import typer
from rich.panel import Panel
from rich.prompt import Confirm

from skyvern.config import settings

from .console import console
from .run_commands import get_pids_on_port, kill_pids

stop_app = typer.Typer(help="Stop Skyvern services like the API server.")


@stop_app.command(name="server")
def stop_server(
    force: bool = typer.Option(False, "--force", "-f", help="Force kill without confirmation"),
) -> None:
    """Stop the Skyvern API server running on the configured port."""
    port = settings.PORT
    with console.status(f"[bold green]Checking for process on port {port}...") as status:
        pids = get_pids_on_port(port)
        status.stop()
    if not pids:
        console.print(f"[yellow]No process found running on port {port}.[/yellow]")
        return
    if not force:
        prompt = f"Process{'es' if len(pids) > 1 else ''} running on port {port}. Kill them?"
        confirm = Confirm.ask(prompt)
        if not confirm:
            console.print("[yellow]Server not stopped.[/yellow]")
            return
    kill_pids(pids)
    console.print(Panel(f"Stopped server on port {port}", border_style="red"))
