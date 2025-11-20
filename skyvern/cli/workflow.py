"""Workflow-related CLI helpers."""

from __future__ import annotations

import json
import os

import typer
from dotenv import load_dotenv
from rich.panel import Panel

from skyvern.client import Skyvern
from skyvern.config import settings
from skyvern.utils.env_paths import resolve_backend_env_path

from .console import console
from .tasks import _list_workflow_tasks

workflow_app = typer.Typer(help="Manage Skyvern workflows.")


@workflow_app.callback()
def workflow_callback(
    ctx: typer.Context,
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Store the provided API key in the Typer context."""
    ctx.obj = {"api_key": api_key}


def _get_client(api_key: str | None = None) -> Skyvern:
    """Instantiate a Skyvern SDK client using environment variables."""
    load_dotenv(resolve_backend_env_path())
    key = api_key or os.getenv("SKYVERN_API_KEY") or settings.SKYVERN_API_KEY
    return Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=key)


@workflow_app.command("run")
def run_workflow(
    ctx: typer.Context,
    workflow_id: str = typer.Argument(..., help="Workflow permanent ID"),
    parameters: str = typer.Option("{}", "--parameters", "-p", help="JSON parameters for the workflow"),
    title: str | None = typer.Option(None, "--title", help="Title for the workflow run"),
    max_steps: int | None = typer.Option(None, "--max-steps", help="Override the workflow max steps"),
) -> None:
    """Run a workflow."""
    try:
        params_dict = json.loads(parameters) if parameters else {}
    except json.JSONDecodeError:
        console.print(f"[red]Invalid JSON for parameters: {parameters}[/red]")
        raise typer.Exit(code=1)

    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    run_resp = client.run_workflow(
        workflow_id=workflow_id,
        parameters=params_dict,
        title=title,
        max_steps_override=max_steps,
    )
    console.print(
        Panel(
            f"Started workflow run [bold]{run_resp.run_id}[/bold]",
            border_style="green",
        )
    )


@workflow_app.command("cancel")
def cancel_workflow(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="ID of the workflow run"),
) -> None:
    """Cancel a running workflow."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    client.cancel_run(run_id=run_id)
    console.print(Panel(f"Cancel signal sent for run {run_id}", border_style="red"))


@workflow_app.command("status")
def workflow_status(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="ID of the workflow run"),
    tasks: bool = typer.Option(False, "--tasks", help="Show task executions"),
) -> None:
    """Retrieve status information for a workflow run."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    run = client.get_run(run_id=run_id)
    console.print(Panel(run.model_dump_json(indent=2), border_style="cyan"))
    if tasks:
        task_list = _list_workflow_tasks(client, run_id)
        console.print(Panel(json.dumps(task_list, indent=2), border_style="magenta"))


@workflow_app.command("list")
def list_workflows(
    ctx: typer.Context,
    page: int = typer.Option(1, "--page", help="Page number"),
    page_size: int = typer.Option(10, "--page-size", help="Number of workflows to return"),
    template: bool = typer.Option(False, "--template", help="List template workflows"),
) -> None:
    """List workflows for the organization."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    resp = client._client_wrapper.httpx_client.request(
        "api/v1/workflows",
        method="GET",
        params={"page": page, "page_size": page_size, "template": template},
    )
    resp.raise_for_status()
    console.print(Panel(json.dumps(resp.json(), indent=2), border_style="cyan"))
