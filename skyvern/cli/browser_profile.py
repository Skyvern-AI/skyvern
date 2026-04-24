"""Browser-profile CLI commands for cloud saved-login state."""

from __future__ import annotations

from typing import Any

import typer
from dotenv import load_dotenv

from skyvern.config import settings
from skyvern.utils.env_paths import resolve_backend_env_path

from .commands._output import run_tool
from .mcp_tools.browser_profile import skyvern_browser_profile_create as tool_browser_profile_create
from .mcp_tools.browser_profile import skyvern_browser_profile_delete as tool_browser_profile_delete
from .mcp_tools.browser_profile import skyvern_browser_profile_get as tool_browser_profile_get
from .mcp_tools.browser_profile import skyvern_browser_profile_list as tool_browser_profile_list

browser_profile_app = typer.Typer(
    help=(
        "Manage cloud saved-login browser profiles. "
        "Create one from a persisted workflow run or eligible browser session, "
        "then reuse its bp_ ID on workflow runs or cloud sessions."
    ),
    no_args_is_help=True,
)


@browser_profile_app.callback()
def browser_profile_callback(
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Load browser-profile CLI environment and optional API key override."""
    load_dotenv(resolve_backend_env_path())
    if api_key:
        settings.SKYVERN_API_KEY = api_key


@browser_profile_app.command("list")
def browser_profile_list(
    include_deleted: bool = typer.Option(False, "--include-deleted", help="Include soft-deleted profiles."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List cloud browser profiles for reusable authenticated state."""

    async def _run() -> dict[str, Any]:
        return await tool_browser_profile_list(include_deleted=include_deleted)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check your API key and Skyvern connection.",
        action="skyvern_browser_profile_list",
        telemetry_tool_name="skyvern_browser_profile_list",
    )


@browser_profile_app.command("get")
def browser_profile_get(
    browser_profile_id: str = typer.Option(..., "--id", "--browser-profile-id", help="Browser profile ID (bp_...)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get a cloud browser profile by ID."""

    async def _run() -> dict[str, Any]:
        return await tool_browser_profile_get(browser_profile_id=browser_profile_id)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the browser profile ID and your API key.",
        action="skyvern_browser_profile_get",
        telemetry_tool_name="skyvern_browser_profile_get",
    )


@browser_profile_app.command("create")
def browser_profile_create(
    name: str = typer.Option(..., "--name", help="Human-readable name for the saved login browser profile."),
    workflow_run_id: str | None = typer.Option(
        None,
        "--workflow-run-id",
        "--from-run",
        help=(
            "Workflow run ID (wr_...) whose persisted browser state should be archived. "
            "The workflow definition must have persist_browser_session=true."
        ),
    ),
    browser_session_id: str | None = typer.Option(
        None,
        "--browser-session-id",
        "--from-session",
        help="Persistent browser session ID (pbs_...) to snapshot into a cloud browser profile.",
    ),
    description: str | None = typer.Option(None, "--description", help="Optional profile description."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Create a cloud saved-login browser profile from exactly one source."""

    async def _run() -> dict[str, Any]:
        return await tool_browser_profile_create(
            name=name,
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run_id,
            description=description,
        )

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the source run/session ID and whether its browser archive is ready.",
        action="skyvern_browser_profile_create",
        telemetry_tool_name="skyvern_browser_profile_create",
    )


@browser_profile_app.command("delete")
def browser_profile_delete(
    browser_profile_id: str = typer.Option(..., "--id", "--browser-profile-id", help="Browser profile ID (bp_...)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Delete a cloud browser profile by ID."""

    async def _run() -> dict[str, Any]:
        return await tool_browser_profile_delete(browser_profile_id=browser_profile_id)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the browser profile ID and your API key.",
        action="skyvern_browser_profile_delete",
        telemetry_tool_name="skyvern_browser_profile_delete",
    )
