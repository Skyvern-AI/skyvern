"""Workflow-related CLI helpers."""

from __future__ import annotations

import json
import os
from typing import Optional

import asyncio

import typer
from dotenv import load_dotenv
from rich.panel import Panel

from skyvern.forge import app
from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType

from skyvern.client import Skyvern
from skyvern.config import settings

from .console import console


workflow_app = typer.Typer(help="Manage Skyvern workflows.")


@workflow_app.callback()
def workflow_callback(
    ctx: typer.Context,
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Store the provided API key in the Typer context."""
    ctx.obj = {"api_key": api_key}


async def _fetch_local_api_key() -> str | None:
    """Retrieve the latest local API key from the database."""
    try:
        organization = await app.DATABASE.get_organization_by_domain("skyvern.local")
        if not organization:
            return None
        org_auth_token = await app.DATABASE.get_valid_org_auth_token(
            organization_id=organization.organization_id,
            token_type=OrganizationAuthTokenType.api,
        )
        return org_auth_token.token if org_auth_token else None
    except Exception:
        return None


def _get_client(api_key: Optional[str] = None) -> Skyvern:
    """Instantiate a Skyvern SDK client using environment variables."""
    load_dotenv()
    load_dotenv(".env")
    key = api_key or os.getenv("SKYVERN_API_KEY")
    if not key and settings.ENV == "local":
        key = asyncio.run(_fetch_local_api_key())
    key = key or settings.SKYVERN_API_KEY
    return Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=key)


@workflow_app.command("start")
def start_workflow(
    ctx: typer.Context,
    workflow_id: str = typer.Argument(..., help="Workflow permanent ID"),
    parameters: str = typer.Option("{}", "--parameters", "-p", help="JSON parameters for the workflow"),
    title: Optional[str] = typer.Option(None, "--title", help="Title for the workflow run"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", help="Override the workflow max steps"),
) -> None:
    """Dispatch a workflow run."""
    try:
        params_dict = json.loads(parameters) if parameters else {}
    except json.JSONDecodeError:
        console.print(f"[red]Invalid JSON for parameters: {parameters}[/red]")
        raise typer.Exit(code=1)

    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
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
def stop_workflow(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="ID of the workflow run"),
) -> None:
    """Cancel a running workflow."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    client.agent.cancel_run(run_id=run_id)
    console.print(Panel(f"Stop signal sent for run {run_id}", border_style="red"))


@workflow_app.command("status")
def workflow_status(
    ctx: typer.Context,
    run_id: str = typer.Argument(..., help="ID of the workflow run"),
) -> None:
    """Retrieve status information for a workflow run."""
    client = _get_client(ctx.obj.get("api_key") if ctx.obj else None)
    run = client.agent.get_run(run_id=run_id)
    console.print(Panel(run.model_dump_json(indent=2), border_style="cyan"))
