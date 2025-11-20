"""Task-related CLI helpers."""

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

tasks_app = typer.Typer(help="Manage Skyvern tasks and operations.")


@tasks_app.callback()
def tasks_callback(
    ctx: typer.Context,
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Store API key in Typer context."""
    ctx.obj = {"api_key": api_key}


def _get_client(api_key: str | None = None) -> Skyvern:
    """Instantiate a Skyvern SDK client using environment variables."""
    load_dotenv(resolve_backend_env_path())
    key = api_key or os.getenv("SKYVERN_API_KEY") or settings.SKYVERN_API_KEY
    return Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=key)


def _list_workflow_tasks(client: Skyvern, run_id: str) -> list[dict]:
    """Return tasks for the given workflow run."""
    resp = client._client_wrapper.httpx_client.request(
        "api/v1/tasks",
        method="GET",
        params={"workflow_run_id": run_id, "page_size": 100, "page": 1},
    )
    resp.raise_for_status()
    return resp.json()


@tasks_app.command("list")
def list_tasks(
    ctx: typer.Context,
    workflow_run_id: str = typer.Option(..., "--workflow-run-id", "-r", help="Workflow run ID"),
) -> None:
    """List tasks for a workflow run."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    tasks = _list_workflow_tasks(client, workflow_run_id)
    console.print(Panel(json.dumps(tasks, indent=2), border_style="cyan"))
