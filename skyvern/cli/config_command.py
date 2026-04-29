"""Organization config CLI: ``skyvern config show | get | set``."""

from __future__ import annotations

from typing import Any

import typer
from dotenv import load_dotenv

from skyvern.config import settings
from skyvern.forge.sdk.schemas.organizations import Organization, OrganizationUpdate
from skyvern.utils.env_paths import resolve_backend_env_path

from .commands._output import run_tool
from .mcp_tools.org import skyvern_org_get as tool_org_get
from .mcp_tools.org import skyvern_org_update as tool_org_update

_SETTABLE_KEYS: frozenset[str] = frozenset(OrganizationUpdate.model_fields)
# ``clear_*`` keys are write-only verbs (reset to NULL); not surfaced by ``get``.
_READABLE_KEYS: frozenset[str] = frozenset(
    {name for name in Organization.model_fields if not name.startswith("clear_")}
)


config_app = typer.Typer(
    help="Read and update organization settings (max_steps_per_run, webhook URL, retries, artifact URL expiry).",
    no_args_is_help=True,
)


@config_app.callback()
def config_callback(
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="SKYVERN_API_KEY",
        help="Skyvern API key.",
    ),
) -> None:
    """Load env and apply the optional API key override."""
    load_dotenv(resolve_backend_env_path())
    if api_key:
        settings.SKYVERN_API_KEY = api_key


@config_app.command("show")
def config_show(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show all current organization settings."""

    async def _run() -> dict[str, Any]:
        return await tool_org_get()

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check your API key and Skyvern connection.",
        action="skyvern_org_get",
    )


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help=f"Setting key. One of: {', '.join(sorted(_READABLE_KEYS))}."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get a single setting value."""
    if key not in _READABLE_KEYS:
        raise typer.BadParameter(f"Unknown key: {key!r}. Allowed: {', '.join(sorted(_READABLE_KEYS))}")

    async def _run() -> dict[str, Any]:
        result = await tool_org_get()
        if not result.get("ok"):
            return result
        full = result.get("data") or {}
        return {**result, "data": {key: full.get(key)}}

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check your API key and Skyvern connection.",
        action="skyvern_org_get",
    )


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help=f"Setting key. One of: {', '.join(sorted(_SETTABLE_KEYS))}."),
    value: str = typer.Argument(..., help="New value (Pydantic coerces strings to ints/bools)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Update a single organization setting."""
    if key not in _SETTABLE_KEYS:
        raise typer.BadParameter(f"Unknown key: {key!r}. Allowed: {', '.join(sorted(_SETTABLE_KEYS))}")

    async def _run() -> dict[str, Any]:
        return await tool_org_update(updates={key: value})

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check your API key and Skyvern connection.",
        action="skyvern_org_update",
    )
