"""Workflow-related CLI commands with MCP-parity flags and output."""

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
from .mcp_tools.workflow import skyvern_workflow_cancel as tool_workflow_cancel
from .mcp_tools.workflow import skyvern_workflow_create as tool_workflow_create
from .mcp_tools.workflow import skyvern_workflow_delete as tool_workflow_delete
from .mcp_tools.workflow import skyvern_workflow_get as tool_workflow_get
from .mcp_tools.workflow import skyvern_workflow_list as tool_workflow_list
from .mcp_tools.workflow import skyvern_workflow_run as tool_workflow_run
from .mcp_tools.workflow import skyvern_workflow_status as tool_workflow_status
from .mcp_tools.workflow import skyvern_workflow_update as tool_workflow_update

workflow_app = typer.Typer(help="Manage Skyvern workflows.", no_args_is_help=True)


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
    output_error(str(err.get("message", "Unknown error")), hint=str(err.get("hint", "")), json_mode=False)


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


def _resolve_inline_or_file(value: str | None, *, param_name: str) -> str | None:
    if value is None or not value.startswith("@"):
        return value

    file_path = value[1:]
    if not file_path:
        raise typer.BadParameter(f"{param_name} file path cannot be empty after '@'.")

    path = Path(file_path).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise typer.BadParameter(f"Unable to read {param_name} file '{path}': {e}") from e


@workflow_app.callback()
def workflow_callback(
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="Skyvern API key",
        envvar="SKYVERN_API_KEY",
    ),
) -> None:
    """Load workflow CLI environment and optional API key override."""
    load_dotenv(resolve_backend_env_path())
    if api_key:
        settings.SKYVERN_API_KEY = api_key


@workflow_app.command("list")
def workflow_list(
    search: str | None = typer.Option(
        None,
        "--search",
        help="Search across workflow titles, folder names, and parameter metadata.",
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number (1-based)."),
    page_size: int = typer.Option(10, "--page-size", min=1, max=100, help="Results per page."),
    only_workflows: bool = typer.Option(
        False,
        "--only-workflows",
        help="Only return multi-step workflows (exclude saved tasks).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List workflows."""

    async def _run() -> dict[str, Any]:
        return await tool_workflow_list(
            search=search,
            page=page,
            page_size=page_size,
            only_workflows=only_workflows,
        )

    _run_tool(_run, json_output=json_output, hint_on_exception="Check your API key and workflow list filters.")


@workflow_app.command("get")
def workflow_get(
    workflow_id: str = typer.Option(..., "--id", help="Workflow permanent ID (wpid_...)."),
    version: int | None = typer.Option(None, "--version", min=1, help="Specific version to retrieve."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get a workflow definition by ID."""

    async def _run() -> dict[str, Any]:
        return await tool_workflow_get(workflow_id=workflow_id, version=version)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check your API key and workflow ID.")


@workflow_app.command("create")
def workflow_create(
    definition: str = typer.Option(
        ...,
        "--definition",
        help="Workflow definition as YAML/JSON string or @file path.",
    ),
    definition_format: str = typer.Option(
        "auto",
        "--format",
        help="Definition format: json, yaml, or auto.",
    ),
    folder_id: str | None = typer.Option(None, "--folder-id", help="Folder ID (fld_...) for the workflow."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Create a workflow."""

    async def _run() -> dict[str, Any]:
        resolved_definition = _resolve_inline_or_file(definition, param_name="definition")
        assert resolved_definition is not None
        return await tool_workflow_create(
            definition=resolved_definition,
            format=definition_format,
            folder_id=folder_id,
        )

    _run_tool(_run, json_output=json_output, hint_on_exception="Check the workflow definition syntax.")


@workflow_app.command("update")
def workflow_update(
    workflow_id: str = typer.Option(..., "--id", help="Workflow permanent ID (wpid_...)."),
    definition: str = typer.Option(
        ...,
        "--definition",
        help="Updated workflow definition as YAML/JSON string or @file path.",
    ),
    definition_format: str = typer.Option(
        "auto",
        "--format",
        help="Definition format: json, yaml, or auto.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Update a workflow definition."""

    async def _run() -> dict[str, Any]:
        resolved_definition = _resolve_inline_or_file(definition, param_name="definition")
        assert resolved_definition is not None
        return await tool_workflow_update(
            workflow_id=workflow_id,
            definition=resolved_definition,
            format=definition_format,
        )

    _run_tool(_run, json_output=json_output, hint_on_exception="Check the workflow ID and definition syntax.")


@workflow_app.command("delete")
def workflow_delete(
    workflow_id: str = typer.Option(..., "--id", help="Workflow permanent ID (wpid_...)."),
    force: bool = typer.Option(False, "--force", help="Confirm permanent deletion."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Delete a workflow."""

    async def _run() -> dict[str, Any]:
        return await tool_workflow_delete(workflow_id=workflow_id, force=force)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check the workflow ID and your permissions.")


@workflow_app.command("run")
def workflow_run(
    workflow_id: str = typer.Option(..., "--id", help="Workflow permanent ID (wpid_...)."),
    params: str | None = typer.Option(
        None,
        "--params",
        "--parameters",
        "-p",
        help="Workflow parameters as JSON string or @file path.",
    ),
    session: str | None = typer.Option(None, "--session", help="Browser session ID (pbs_...) to reuse."),
    webhook: str | None = typer.Option(None, "--webhook", help="Status webhook callback URL."),
    proxy: str | None = typer.Option(None, "--proxy", help="Proxy location (e.g., RESIDENTIAL)."),
    wait: bool = typer.Option(False, "--wait", help="Wait for workflow completion before returning."),
    timeout: int = typer.Option(
        300,
        "--timeout",
        min=10,
        max=3600,
        help="Max wait time in seconds when --wait is set.",
    ),
    run_with: str | None = typer.Option(None, "--run-with", help="Execution mode (e.g., 'code' for cached script)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run a workflow."""

    async def _run() -> dict[str, Any]:
        resolved_params = _resolve_inline_or_file(params, param_name="params")
        return await tool_workflow_run(
            workflow_id=workflow_id,
            parameters=resolved_params,
            browser_session_id=session,
            webhook_url=webhook,
            proxy_location=proxy,
            wait=wait,
            timeout_seconds=timeout,
            run_with=run_with,
        )

    _run_tool(_run, json_output=json_output, hint_on_exception="Check the workflow ID and run parameters.")


@workflow_app.command("status")
def workflow_status(
    run_id: str = typer.Option(..., "--run-id", help="Run ID (wr_... or tsk_v2_...)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get workflow run status."""

    async def _run() -> dict[str, Any]:
        return await tool_workflow_status(run_id=run_id)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check the run ID and API key.")


@workflow_app.command("cancel")
def workflow_cancel(
    run_id: str = typer.Option(..., "--run-id", help="Run ID (wr_... or tsk_v2_...)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Cancel a workflow run."""

    async def _run() -> dict[str, Any]:
        return await tool_workflow_cancel(run_id=run_id)

    _run_tool(_run, json_output=json_output, hint_on_exception="Check the run ID and API key.")
