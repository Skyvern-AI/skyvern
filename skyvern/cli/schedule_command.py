"""Workflow-schedule CLI: ``skyvern schedule list | get | create | update | enable | disable | delete``."""

from __future__ import annotations

import json
from typing import Any

import typer
from click.core import ParameterSource
from dotenv import load_dotenv

from skyvern.config import settings
from skyvern.utils.env_paths import resolve_backend_env_path

from .commands._output import run_tool
from .mcp_tools.schedule import skyvern_schedule_create as tool_schedule_create
from .mcp_tools.schedule import skyvern_schedule_delete as tool_schedule_delete
from .mcp_tools.schedule import skyvern_schedule_disable as tool_schedule_disable
from .mcp_tools.schedule import skyvern_schedule_enable as tool_schedule_enable
from .mcp_tools.schedule import skyvern_schedule_get as tool_schedule_get
from .mcp_tools.schedule import skyvern_schedule_list as tool_schedule_list
from .mcp_tools.schedule import skyvern_schedule_list_for_workflow as tool_schedule_list_for_workflow
from .mcp_tools.schedule import skyvern_schedule_update as tool_schedule_update

schedule_app = typer.Typer(
    help="Manage workflow schedules (list, create, update, enable/disable, delete).",
    no_args_is_help=True,
)


@schedule_app.callback()
def schedule_callback(
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="SKYVERN_API_KEY",
        help="Skyvern API key.",
    ),
) -> None:
    load_dotenv(resolve_backend_env_path())
    if api_key:
        settings.SKYVERN_API_KEY = api_key


def _parse_parameters(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"--parameters must be valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise typer.BadParameter("--parameters must be a JSON object (dict).")
    return parsed


@schedule_app.command("list")
def schedule_list(
    ctx: typer.Context,
    workflow_id: str | None = typer.Option(
        None,
        "--workflow-id",
        help=(
            "Filter to a single workflow_permanent_id (wpid_…). Omit for org-wide list. "
            "Per-workflow rows include backend_schedule_id; the org-wide list does not — fetch a single schedule with `get` for the backend handle."
        ),
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Filter: 'active' or 'paused'. (Org-wide list only.)",
    ),
    search: str | None = typer.Option(
        None, "--search", help="Search workflow title or schedule name. (Org-wide list only.)"
    ),
    page: int = typer.Option(1, "--page", min=1, help="Page number (1-based). (Org-wide list only.)"),
    page_size: int = typer.Option(10, "--page-size", min=1, max=100, help="Results per page. (Org-wide list only.)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List schedules — org-wide by default, or per-workflow with --workflow-id.

    The per-workflow path is unpaginated and ignores --status/--search; passing
    those alongside --workflow-id is rejected so they don't get silently dropped.
    """
    if workflow_id is not None:
        # Click reports the source for each parameter; reject org-wide-only flags
        # if the user explicitly passed them alongside --workflow-id.
        org_wide_flags: list[str] = []
        for flag_name, click_param_name in (
            ("--status", "status"),
            ("--search", "search"),
            ("--page", "page"),
            ("--page-size", "page_size"),
        ):
            src = ctx.get_parameter_source(click_param_name)
            if src is not None and src not in (ParameterSource.DEFAULT, ParameterSource.DEFAULT_MAP):
                org_wide_flags.append(flag_name)
        if org_wide_flags:
            raise typer.BadParameter(
                f"{', '.join(org_wide_flags)} cannot be used with --workflow-id "
                "(per-workflow list is unpaginated and unfiltered). Drop the flag(s) or omit --workflow-id."
            )

    async def _run() -> dict[str, Any]:
        if workflow_id is not None:
            return await tool_schedule_list_for_workflow(workflow_permanent_id=workflow_id)
        return await tool_schedule_list(page=page, page_size=page_size, status=status, search=search)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check your API key and Skyvern connection.",
        action="skyvern_schedule_list",
    )


@schedule_app.command("get")
def schedule_get(
    workflow_id: str = typer.Option(..., "--workflow-id", help="Workflow ID (wpid_…)."),
    schedule_id: str = typer.Option(..., "--id", "--schedule-id", help="Schedule ID (wfs_…)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Get a single schedule by ID."""

    async def _run() -> dict[str, Any]:
        return await tool_schedule_get(workflow_permanent_id=workflow_id, workflow_schedule_id=schedule_id)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the workflow and schedule IDs.",
        action="skyvern_schedule_get",
    )


@schedule_app.command("create")
def schedule_create(
    workflow_id: str = typer.Option(..., "--workflow-id", help="Workflow ID (wpid_…)."),
    cron: str = typer.Option(..., "--cron", help="Cron expression, e.g. '0 9 * * *'."),
    timezone: str = typer.Option("UTC", "--timezone", help="IANA timezone name."),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="Whether the schedule fires immediately."),
    parameters: str | None = typer.Option(None, "--parameters", help="Workflow input parameters as JSON object."),
    name: str | None = typer.Option(None, "--name", help="Schedule name."),
    description: str | None = typer.Option(None, "--description", help="Schedule description."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Create a schedule for a workflow."""
    params_dict = _parse_parameters(parameters)

    async def _run() -> dict[str, Any]:
        return await tool_schedule_create(
            workflow_permanent_id=workflow_id,
            cron_expression=cron,
            timezone=timezone,
            enabled=enabled,
            parameters=params_dict,
            name=name,
            description=description,
        )

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check cron, timezone, and parameter shape.",
        action="skyvern_schedule_create",
    )


@schedule_app.command("update")
def schedule_update(
    workflow_id: str = typer.Option(..., "--workflow-id", help="Workflow ID (wpid_…)."),
    schedule_id: str = typer.Option(..., "--id", "--schedule-id", help="Schedule ID (wfs_…)."),
    cron: str | None = typer.Option(None, "--cron", help="New cron expression."),
    timezone: str | None = typer.Option(None, "--timezone", help="New IANA timezone."),
    enabled: bool | None = typer.Option(
        None,
        "--enabled/--disabled",
        help="New enabled flag. Omit to leave unchanged in partial mode.",
    ),
    parameters: str | None = typer.Option(None, "--parameters", help="New parameters as JSON object."),
    clear_parameters: bool = typer.Option(False, "--clear-parameters", help="Clear parameters (set to null)."),
    name: str | None = typer.Option(None, "--name", help="New schedule name."),
    clear_name: bool = typer.Option(False, "--clear-name", help="Clear schedule name."),
    description: str | None = typer.Option(None, "--description", help="New schedule description."),
    clear_description: bool = typer.Option(False, "--clear-description", help="Clear description."),
    exact: bool = typer.Option(
        False,
        "--exact",
        help="Skip fetch+merge and require every replacement field explicitly. Prevents silent enable/clear.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Update a schedule.

    Partial by default: GET the schedule, merge supplied fields/clear flags, then PUT.
    Pass --exact to skip the GET; in that case every replacement field must be supplied
    explicitly (or via the corresponding --clear-* flag for nullable fields).
    """
    if name is not None and clear_name:
        raise typer.BadParameter("Cannot pass both --name and --clear-name.")
    if description is not None and clear_description:
        raise typer.BadParameter("Cannot pass both --description and --clear-description.")
    if parameters is not None and clear_parameters:
        raise typer.BadParameter("Cannot pass both --parameters and --clear-parameters.")

    params_dict = _parse_parameters(parameters)

    async def _run() -> dict[str, Any]:
        return await tool_schedule_update(
            workflow_permanent_id=workflow_id,
            workflow_schedule_id=schedule_id,
            cron_expression=cron,
            timezone=timezone,
            enabled=enabled,
            parameters=params_dict,
            clear_parameters=clear_parameters,
            name=name,
            clear_name=clear_name,
            description=description,
            clear_description=clear_description,
            exact=exact,
        )

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check cron, timezone, and parameter shape.",
        action="skyvern_schedule_update",
    )


@schedule_app.command("enable")
def schedule_enable(
    workflow_id: str = typer.Option(..., "--workflow-id", help="Workflow ID (wpid_…)."),
    schedule_id: str = typer.Option(..., "--id", "--schedule-id", help="Schedule ID (wfs_…)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Enable a paused schedule."""

    async def _run() -> dict[str, Any]:
        return await tool_schedule_enable(workflow_permanent_id=workflow_id, workflow_schedule_id=schedule_id)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the schedule and workflow IDs.",
        action="skyvern_schedule_enable",
    )


@schedule_app.command("disable")
def schedule_disable(
    workflow_id: str = typer.Option(..., "--workflow-id", help="Workflow ID (wpid_…)."),
    schedule_id: str = typer.Option(..., "--id", "--schedule-id", help="Schedule ID (wfs_…)."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Disable an active schedule."""

    async def _run() -> dict[str, Any]:
        return await tool_schedule_disable(workflow_permanent_id=workflow_id, workflow_schedule_id=schedule_id)

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the schedule and workflow IDs.",
        action="skyvern_schedule_disable",
    )


@schedule_app.command("delete")
def schedule_delete(
    workflow_id: str = typer.Option(..., "--workflow-id", help="Workflow ID (wpid_…)."),
    schedule_id: str = typer.Option(..., "--id", "--schedule-id", help="Schedule ID (wfs_…)."),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Required: confirm deletion. The schedule will stop firing immediately.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Delete a schedule. Irreversible."""
    if not yes:
        raise typer.BadParameter("Pass --yes to confirm deletion. This is irreversible.")

    async def _run() -> dict[str, Any]:
        return await tool_schedule_delete(
            workflow_permanent_id=workflow_id,
            workflow_schedule_id=schedule_id,
            force=True,
        )

    run_tool(
        _run,
        json_output=json_output,
        hint_on_exception="Check the schedule and workflow IDs.",
        action="skyvern_schedule_delete",
    )
