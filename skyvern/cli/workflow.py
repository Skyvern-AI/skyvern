"""Workflow-related CLI helpers."""

from __future__ import annotations

import json
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.panel import Panel

from skyvern.client import Skyvern
from skyvern.config import settings

from .console import console


workflow_app = typer.Typer(help="Manage Skyvern workflows.")


def _get_client() -> Skyvern:
    """Instantiate a Skyvern SDK client using environment variables."""
    load_dotenv()
    load_dotenv(".env")
    return Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=settings.SKYVERN_API_KEY)


@workflow_app.command("start")
def start_workflow(
    workflow_id: str = typer.Argument(..., help="Workflow permanent ID"),
    parameters: str = typer.Option(
        "{}", "--parameters", "-p", help="JSON parameters for the workflow"
    ),
    title: Optional[str] = typer.Option(None, "--title", help="Title for the workflow run"),
    max_steps: Optional[int] = typer.Option(
        None, "--max-steps", help="Override the workflow max steps"
    ),
) -> None:
    """Dispatch a workflow run."""
    try:
        params_dict = json.loads(parameters) if parameters else {}
    except json.JSONDecodeError:
        console.print(f"[red]Invalid JSON for parameters: {parameters}[/red]")
        raise typer.Exit(code=1)

    client = _get_client()
    run_resp = client.agent.run_workflow(
        workflow_id=workflow_id,
        parameters=params_dict or None,
        title=title,
        max_steps_override=max_steps,
    )
    console.print(
        Panel(
            f"Started workflow run [bold]{run_resp.run_id}[/bold]",
            border_style="green",
        )
    )


@workflow_app.command("stop")
def stop_workflow(run_id: str = typer.Argument(..., help="ID of the workflow run")) -> None:
    """Cancel a running workflow."""
    client = _get_client()
    client.agent.cancel_run(run_id=run_id)
    console.print(Panel(f"Stop signal sent for run {run_id}", border_style="red"))


@workflow_app.command("status")
def workflow_status(run_id: str = typer.Argument(..., help="ID of the workflow run")) -> None:
    """Retrieve status information for a workflow run."""
    client = _get_client()
    run = client.agent.get_run(run_id=run_id)
    console.print(Panel(run.model_dump_json(indent=2), border_style="cyan"))
