import typer
from rich.panel import Panel

from .console import console

stop_app = typer.Typer(help="Commands to stop Skyvern services such as the API server or UI.")


@stop_app.command(name="ui")
def stop_ui() -> None:
    """Stop the Skyvern UI server."""
    # This function is assumed to be implemented in the PR
    # Implementation will be provided by the PR
    pass


@stop_app.command(name="server")
def stop_server() -> None:
    """Stop the Skyvern API server."""
    # This function is assumed to be implemented in the PR
    # Implementation will be provided by the PR
    pass


@stop_app.command(name="all")
def stop_all() -> None:
    """Stop both the Skyvern UI server and API server."""
    console.print(Panel("[bold yellow]Stopping all Skyvern services...[/bold yellow]", border_style="yellow"))
    
    # First stop the UI
    console.print("[bold blue]Stopping Skyvern UI...[/bold blue]")
    stop_ui()
    console.print("✅ [green]Skyvern UI stopped.[/green]")
    
    # Then stop the server
    console.print("[bold green]Stopping Skyvern API Server...[/bold green]")
    stop_server()
    console.print("✅ [green]Skyvern API Server stopped.[/green]")
    
    console.print("🎉 [bold green]All Skyvern services have been stopped![/bold green]")
