import asyncio
import json
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from skyvern.config import settings
from skyvern.library import Skyvern

from .common import console


def list_tasks() -> None:
    """List recent Skyvern tasks."""
    console.print(Panel.fit("[bold blue]Recent Skyvern Tasks[/]", subtitle="Retrieving task history"))
    try:
        load_dotenv()
        skyvern_agent = Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=settings.SKYVERN_API_KEY)
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            task = progress.add_task("[green]Fetching recent tasks...", total=1)
            tasks = asyncio.run(skyvern_agent.get_tasks())
            progress.update(task, completed=1)
        if not tasks:
            console.print("[yellow]No tasks found[/]")
            return
        table = Table(title=f"Tasks ({len(tasks)} found)")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="blue")
        for task in tasks:
            table.add_row(
                str(task.id),
                task.title or "Untitled",
                task.status or "Unknown",
                task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else "Unknown",
            )
        console.print(table)
        console.print("\n[bold]Next steps:[/]")
        console.print("• View task details:    [yellow]skyvern tasks show <task_id>[/]")
        console.print("• Retry a task:         [yellow]skyvern tasks retry <task_id>[/]")
    except Exception as e:
        console.print(f"[bold red]Error listing tasks:[/] {str(e)}")
        console.print("[yellow]Make sure your API key is set correctly in .env[/]")


def create_task(
    prompt: str = typer.Option(..., "--prompt", "-p", help="Task prompt"),
    url: str = typer.Option(..., "--url", "-u", help="Starting URL"),
    schema: Optional[str] = typer.Option(None, "--schema", "-s", help="Data extraction schema (JSON)"),
    output_json: bool = typer.Option(False, "--json", help="Output results as JSON"),
) -> None:
    """Create and run a new Skyvern task."""
    console.print(Panel.fit("[bold blue]Creating New Skyvern Task[/]", subtitle="Running browser automation"))
    console.print(f"[bold]Prompt:[/] {prompt}")
    console.print(f"[bold]URL:[/] {url}")
    if schema:
        console.print(f"[bold]Schema:[/] {schema}")
    try:
        load_dotenv()
        skyvern_agent = Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=settings.SKYVERN_API_KEY)
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as progress:
            task = progress.add_task("[green]Running task...", total=1)
            result = asyncio.run(
                skyvern_agent.run_task(
                    prompt=prompt,
                    url=url,
                    data_extraction_schema=schema,
                    user_agent="skyvern-cli",
                )
            )
            progress.update(task, completed=1)
        if output_json:
            console.print_json(json.dumps(result.model_dump()))
        else:
            console.print("\n[bold green]Task completed successfully![/]")
            console.print(f"\n[bold]Output:[/] {result.model_dump()['output']}")
            base_url = settings.SKYVERN_BASE_URL
            run_history_url = (
                "https://app.skyvern.com/history" if "skyvern.com" in base_url else "http://localhost:8080/history"
            )
            console.print(f"\nView details at: [link={run_history_url}]{run_history_url}[/link]")
    except Exception as e:
        console.print(f"[bold red]Error creating task:[/] {str(e)}")
        console.print("[yellow]Make sure your API key is set correctly in .env[/]")

