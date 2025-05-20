import asyncio

import typer
from dotenv import load_dotenv
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from skyvern.config import settings
from skyvern.library import Skyvern

from .common import console


def list_workflows() -> None:
    """List Skyvern workflows."""
    console.print(Panel.fit("[bold blue]Skyvern Workflows[/]", subtitle="Retrieving available workflows"))
    try:
        load_dotenv()
        skyvern_agent = Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=settings.SKYVERN_API_KEY)
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            task = progress.add_task("[green]Fetching workflows...", total=1)
            workflows = asyncio.run(skyvern_agent.get_workflows())
            progress.update(task, completed=1)
        if not workflows:
            console.print("[yellow]No workflows found[/]")
            return
        table = Table(title=f"Workflows ({len(workflows)} found)")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="blue")
        for workflow in workflows:
            table.add_row(
                str(workflow.id),
                workflow.title or "Untitled",
                workflow.status or "Unknown",
                workflow.created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(workflow, "created_at") and workflow.created_at else "Unknown",
            )
        console.print(table)
        console.print("\n[bold]Next steps:[/]")
        console.print("• View workflow details:    [yellow]skyvern workflows show <workflow_id>[/]")
        console.print("• Run a workflow:           [yellow]skyvern workflows run <workflow_id>[/]")
    except Exception as e:
        console.print(f"[bold red]Error listing workflows:[/] {str(e)}")
        console.print("[yellow]Make sure your API key is set correctly in .env[/]")

