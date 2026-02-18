"""Credential CLI commands with MCP-parity output and validation."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Callable, Coroutine

import typer
from dotenv import load_dotenv

from skyvern.config import settings
from skyvern.utils.env_paths import resolve_backend_env_path

from .commands._output import output, output_error
from .mcp_tools.credential import skyvern_credential_delete as tool_credential_delete
from .mcp_tools.credential import skyvern_credential_get as tool_credential_get
from .mcp_tools.credential import skyvern_credential_list as tool_credential_list

credential_app = typer.Typer(
    help="MCP-parity credential commands (list/get/delete). Use `skyvern credentials add` for secure creation.",
    no_args_is_help=True,
)


def _emit_tool_result(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        if not result.get("ok", False):
            raise SystemExit(1)
        return

    if result.get("ok", False):
        output(result.get("data"), action=str(result.get("action", "")), json_mode=False)
        return

    err = result.get("error") or {}
    output_error(str(err.get("message") or "Unknown error"), hint=str(err.get("hint") or ""), json_mode=False)


def _run_tool(
    runner: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    *,
    json_output: bool,
    hint_on_exception: str,
) -> None:
    try:
        result: dict[str, Any] = asyncio.run(runner())
        _emit_tool_result(result, json_output=json_output)
    except typer.BadParameter:
        raise
    except Exception as e:
        output_error(str(e), hint=hint_on_exception, json_mode=json_output)


@credential_app.callback()
def credential_callback(
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Load environment and optional API key override."""
    load_dotenv(resolve_backend_env_path())
    if api_key:
        settings.SKYVERN_API_KEY = api_key


@credential_app.command("list")
def credential_list(
    page: int = typer.Option(1, "--page", min=1, help="Page number (1-based)."),
    page_size: int = typer.Option(10, "--page-size", min=1, max=100, help="Results per page."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List stored credentials (metadata only)."""

    async def _run() -> dict[str, Any]:
        return await tool_credential_list(page=page, page_size=page_size)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check your API key and Skyvern connection.")


@credential_app.command("get")
def credential_get(
    credential_id: str = typer.Option(..., "--id", "--credential-id", help="Credential ID (starts with cred_)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get credential metadata by ID."""

    async def _run() -> dict[str, Any]:
        return await tool_credential_get(credential_id=credential_id)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check your API key and credential ID.")


@credential_app.command("delete")
def credential_delete(
    credential_id: str = typer.Option(..., "--id", "--credential-id", help="Credential ID (starts with cred_)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Delete a credential by ID."""

    async def _run() -> dict[str, Any]:
        return await tool_credential_delete(credential_id=credential_id)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check your API key and credential ID.")
