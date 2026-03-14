"""Workflow block CLI commands with MCP-parity output and validation."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable, Coroutine

import typer
from dotenv import load_dotenv

from skyvern.config import settings
from skyvern.utils.env_paths import resolve_backend_env_path

from .commands._output import output, output_error
from .mcp_tools.blocks import skyvern_block_schema as tool_block_schema
from .mcp_tools.blocks import skyvern_block_validate as tool_block_validate

block_app = typer.Typer(help="Workflow block schema and validation commands.", no_args_is_help=True)


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


def _resolve_inline_or_file(value: str, *, param_name: str) -> str:
    if not value.startswith("@"):
        return value

    file_path = value[1:]
    if not file_path:
        raise typer.BadParameter(f"{param_name} file path cannot be empty after '@'.")

    path = Path(file_path).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise typer.BadParameter(f"Unable to read {param_name} file '{path}': {e}") from e


@block_app.callback()
def block_callback(
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


@block_app.command("schema")
def block_schema(
    block_type: str | None = typer.Option(
        None,
        "--type",
        "--block-type",
        help="Block type to inspect (omit to list all available types).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get schema for a specific block type or list all block types."""

    async def _run() -> dict[str, Any]:
        return await tool_block_schema(block_type=block_type)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check block type input.")


@block_app.command("validate")
def block_validate(
    block_json: str = typer.Option(..., "--block-json", help="Block JSON string or @file path."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Validate a single workflow block definition."""

    async def _run() -> dict[str, Any]:
        resolved_json = _resolve_inline_or_file(block_json, param_name="block_json")
        return await tool_block_validate(block_json=resolved_json)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check block JSON syntax and required fields.")
