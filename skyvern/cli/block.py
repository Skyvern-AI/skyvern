"""Workflow block CLI commands with MCP-parity output and validation."""

from __future__ import annotations

from typing import Any

import typer

from skyvern._cli_bootstrap import prepare_cli_runtime
from skyvern.utils.env_paths import EnvIntent

from .commands._output import resolve_inline_or_file, run_tool

block_app = typer.Typer(help="Workflow block schema and validation commands.", no_args_is_help=True)


async def tool_block_schema(**kwargs: Any) -> dict[str, Any]:
    from .mcp_tools.blocks import skyvern_block_schema  # noqa: PLC0415

    return await skyvern_block_schema(**kwargs)


async def tool_block_validate(**kwargs: Any) -> dict[str, Any]:
    from .mcp_tools.blocks import skyvern_block_validate  # noqa: PLC0415

    return await skyvern_block_validate(**kwargs)


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
    prepare_cli_runtime(intent=EnvIntent.CLOUD)
    if api_key:
        from skyvern.config import settings  # noqa: PLC0415

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

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check block type input.",
        action="skyvern_block_schema",
    )


@block_app.command("validate")
def block_validate(
    block_json: str = typer.Option(..., "--block-json", help="Block JSON string or @file path."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Validate a single workflow block definition."""

    async def _run() -> dict[str, Any]:
        resolved_block_json = resolve_inline_or_file(block_json, param_name="block_json")
        return await tool_block_validate(block_json=block_json if resolved_block_json is None else resolved_block_json)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check block JSON syntax and required fields.",
        action="skyvern_block_validate",
    )
