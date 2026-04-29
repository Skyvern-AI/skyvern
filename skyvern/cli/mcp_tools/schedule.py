"""Skyvern MCP workflow-schedule tools — CRUD + enable/disable via ``client.schedules.*``."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from pydantic import Field

from skyvern.client.core.api_error import ApiError

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern
from ._validation import validate_schedule_id, validate_workflow_id

LOG = structlog.get_logger()


def _serialize_schedule(s: Any) -> dict[str, Any]:
    return {
        "workflow_schedule_id": s.workflow_schedule_id,
        "organization_id": s.organization_id,
        "workflow_permanent_id": s.workflow_permanent_id,
        "cron_expression": s.cron_expression,
        "timezone": s.timezone,
        "enabled": s.enabled,
        "parameters": s.parameters,
        # Re-project the SDK's internal handle to a backend-agnostic public key.
        "backend_schedule_id": getattr(s, "temporal_schedule_id", None),
        "name": s.name,
        "description": s.description,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "modified_at": s.modified_at.isoformat() if s.modified_at else None,
    }


def _serialize_schedule_response(resp: Any) -> dict[str, Any]:
    return {
        "schedule": _serialize_schedule(resp.schedule),
        "next_runs": [r.isoformat() for r in (resp.next_runs or [])],
    }


def _serialize_org_schedule_item(item: Any) -> dict[str, Any]:
    # Intentionally omits backend_schedule_id — see OrganizationScheduleItem schema.
    return {
        "workflow_schedule_id": item.workflow_schedule_id,
        "organization_id": item.organization_id,
        "workflow_permanent_id": item.workflow_permanent_id,
        "workflow_title": item.workflow_title,
        "cron_expression": item.cron_expression,
        "timezone": item.timezone,
        "enabled": item.enabled,
        "parameters": item.parameters,
        "name": item.name,
        "description": item.description,
        "next_run": item.next_run.isoformat() if item.next_run else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "modified_at": item.modified_at.isoformat() if item.modified_at else None,
    }


def _api_error(
    action: str, exc: Exception, timer: Timer, hint: str = "Check your API key and Skyvern connection"
) -> dict[str, Any]:
    return make_result(
        action,
        ok=False,
        timing_ms=timer.timing_ms,
        error=make_error(ErrorCode.API_ERROR, str(exc), hint),
    )


def _input_error(action: str, message: str, hint: str) -> dict[str, Any]:
    return make_result(
        action,
        ok=False,
        error=make_error(ErrorCode.INVALID_INPUT, message, hint),
    )


async def skyvern_schedule_list(
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=100)] = 10,
    status: Annotated[str | None, Field(description="Filter: 'active' or 'paused'")] = None,
    search: Annotated[str | None, Field(description="Search by workflow title or schedule name")] = None,
) -> dict[str, Any]:
    """List all workflow schedules in the organization.

    The org-wide list intentionally omits ``backend_schedule_id`` to match the
    server's ``OrganizationScheduleItem`` contract; call ``skyvern_schedule_get``
    when you need the execution-backend handle. Returns 501 if workflow
    schedules are disabled in this Skyvern build. Status filter values are
    validated server-side.
    """
    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            resp = await skyvern.schedules.list_all(
                page=page,
                page_size=page_size,
                status=status,  # type: ignore[arg-type]
                search=search,
            )
            timer.mark("sdk")
        except Exception as e:
            return _api_error("skyvern_schedule_list", e, timer)

    return make_result(
        "skyvern_schedule_list",
        data={
            "schedules": [_serialize_org_schedule_item(s) for s in (resp.schedules or [])],
            "total_count": resp.total_count,
            "page": resp.page,
            "page_size": resp.page_size,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_schedule_list_for_workflow(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
) -> dict[str, Any]:
    """List schedules for a single workflow."""
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_list_for_workflow"):
        return err

    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            resp = await skyvern.schedules.list(workflow_permanent_id)
            timer.mark("sdk")
        except Exception as e:
            return _api_error("skyvern_schedule_list_for_workflow", e, timer)

    return make_result(
        "skyvern_schedule_list_for_workflow",
        data={"schedules": [_serialize_schedule(s) for s in (resp.schedules or [])]},
        timing_ms=timer.timing_ms,
    )


async def skyvern_schedule_get(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
    workflow_schedule_id: Annotated[str, Field(description="Schedule ID (starts with wfs_)")],
) -> dict[str, Any]:
    """Get a single workflow schedule by id."""
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_get"):
        return err
    if err := validate_schedule_id(workflow_schedule_id, "skyvern_schedule_get"):
        return err

    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            resp = await skyvern.schedules.get(workflow_permanent_id, workflow_schedule_id)
            timer.mark("sdk")
        except ApiError as e:
            if e.status_code == 404:
                return make_result(
                    "skyvern_schedule_get",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        f"Schedule {workflow_schedule_id!r} not found",
                        "Use skyvern_schedule_list to find valid schedule IDs.",
                    ),
                )
            return _api_error("skyvern_schedule_get", e, timer)
        except Exception as e:
            return _api_error("skyvern_schedule_get", e, timer)

    return make_result(
        "skyvern_schedule_get",
        data=_serialize_schedule_response(resp),
        timing_ms=timer.timing_ms,
    )


async def skyvern_schedule_create(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
    cron_expression: Annotated[str, Field(description="Cron expression, e.g. '0 9 * * *'")],
    timezone: Annotated[str, Field(description="IANA timezone, e.g. 'UTC' or 'America/New_York'")],
    enabled: Annotated[bool, Field(description="Whether the schedule fires immediately. Defaults to True.")] = True,
    parameters: Annotated[
        dict[str, Any] | None,
        Field(description="Workflow input parameters keyed by parameter name."),
    ] = None,
    name: Annotated[str | None, Field(description="Human-readable schedule name.")] = None,
    description: Annotated[str | None, Field(description="Optional description.")] = None,
) -> dict[str, Any]:
    """Create a recurring schedule for an existing workflow.

    Cron expression and timezone are validated server-side; a 400 is surfaced
    verbatim. Returns 501 if workflow schedules are disabled in this build.
    """
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_create"):
        return err

    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            resp = await skyvern.schedules.create(
                workflow_permanent_id,
                cron_expression=cron_expression,
                timezone=timezone,
                enabled=enabled,
                parameters=parameters,
                name=name,
                description=description,
            )
            timer.mark("sdk")
        except ApiError as e:
            if e.status_code == 404:
                return make_result(
                    "skyvern_schedule_create",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        f"Workflow {workflow_permanent_id!r} not found",
                        "Use skyvern_workflow_list to find valid workflow IDs.",
                    ),
                )
            return _api_error(
                "skyvern_schedule_create", e, timer, hint="Check cron expression, timezone, and parameters."
            )
        except Exception as e:
            return _api_error(
                "skyvern_schedule_create", e, timer, hint="Check cron expression, timezone, and parameters."
            )

    return make_result(
        "skyvern_schedule_create",
        data=_serialize_schedule_response(resp),
        timing_ms=timer.timing_ms,
    )


def _check_update_mutex(
    parameters: dict[str, Any] | None,
    clear_parameters: bool,
    name: str | None,
    clear_name: bool,
    description: str | None,
    clear_description: bool,
) -> dict[str, Any] | None:
    conflicts: list[str] = []
    if parameters is not None and clear_parameters:
        conflicts.append("parameters")
    if name is not None and clear_name:
        conflicts.append("name")
    if description is not None and clear_description:
        conflicts.append("description")
    if conflicts:
        return _input_error(
            "skyvern_schedule_update",
            f"Cannot set and clear the same field at once: {', '.join(conflicts)}",
            "Pass either a value OR the clear flag, not both.",
        )
    return None


def _check_update_exact_completeness(
    cron_expression: str | None,
    timezone: str | None,
    enabled: bool | None,
    parameters: dict[str, Any] | None,
    clear_parameters: bool,
    name: str | None,
    clear_name: bool,
    description: str | None,
    clear_description: bool,
) -> dict[str, Any] | None:
    """In ``exact=True`` mode, every replacement field must be specified explicitly.

    Without this guard, a body sent with only cron+timezone would inherit the
    route schema defaults (``enabled=True``, ``parameters/name/description=None``)
    and silently flip a paused schedule active or wipe metadata.
    """
    missing: list[str] = []
    if cron_expression is None:
        missing.append("cron_expression")
    if timezone is None:
        missing.append("timezone")
    if enabled is None:
        missing.append("enabled")
    if parameters is None and not clear_parameters:
        missing.append("parameters or clear_parameters")
    if name is None and not clear_name:
        missing.append("name or clear_name")
    if description is None and not clear_description:
        missing.append("description or clear_description")
    if missing:
        return _input_error(
            "skyvern_schedule_update",
            f"exact=True requires explicit values for: {', '.join(missing)}",
            "Pass each missing field, or set the corresponding clear flag for nullable fields.",
        )
    return None


async def skyvern_schedule_update(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
    workflow_schedule_id: Annotated[str, Field(description="Schedule ID (starts with wfs_)")],
    cron_expression: Annotated[str | None, Field(description="New cron expression.")] = None,
    timezone: Annotated[str | None, Field(description="New IANA timezone.")] = None,
    enabled: Annotated[bool | None, Field(description="New enabled flag.")] = None,
    parameters: Annotated[dict[str, Any] | None, Field(description="New workflow input parameters.")] = None,
    clear_parameters: Annotated[bool, Field(description="Set parameters to null on the server.")] = False,
    name: Annotated[str | None, Field(description="New schedule name.")] = None,
    clear_name: Annotated[bool, Field(description="Set name to null on the server.")] = False,
    description: Annotated[str | None, Field(description="New description.")] = None,
    clear_description: Annotated[bool, Field(description="Set description to null on the server.")] = False,
    exact: Annotated[
        bool,
        Field(description="If True, skip fetch+merge and require every replacement field explicitly."),
    ] = False,
) -> dict[str, Any]:
    """Update a workflow schedule.

    Two modes:
    - **Partial (default, ``exact=False``)**: GET the schedule, merge supplied
      fields and clear flags, then PUT the full body. Empty patches are
      rejected before any I/O. Note: read-modify-write race window is logged
      via ``modified_at`` of the snapshot.
    - **Exact (``exact=True``)**: caller must supply every replacement field;
      no GET. Prevents accidental ``enabled=True`` / clearing of metadata via
      the route's schema defaults.

    Cannot pass a value AND the corresponding clear flag for the same field.
    """
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_update"):
        return err
    if err := validate_schedule_id(workflow_schedule_id, "skyvern_schedule_update"):
        return err

    if err := _check_update_mutex(parameters, clear_parameters, name, clear_name, description, clear_description):
        return err

    any_value_set = any(v is not None for v in (cron_expression, timezone, enabled, parameters, name, description))
    any_clear_set = any((clear_parameters, clear_name, clear_description))
    if not exact and not any_value_set and not any_clear_set:
        return _input_error(
            "skyvern_schedule_update",
            "Empty update — no fields supplied.",
            "Pass at least one field to update, or set exact=true to do a full replace.",
        )

    # Single SDK client for both the optional GET and the PUT — sharing one
    # instance avoids any auth-context drift if get_skyvern() ever returns
    # a fresh client per call.
    skyvern = get_skyvern()

    # One Timer covers the whole operation so timing_ms reports total wall
    # time including the partial-mode fetch (marked sdk_fetch) plus the PUT
    # (marked sdk_update). Timer auto-records "total" on exit.
    with Timer() as timer:
        if exact:
            if err := _check_update_exact_completeness(
                cron_expression,
                timezone,
                enabled,
                parameters,
                clear_parameters,
                name,
                clear_name,
                description,
                clear_description,
            ):
                return err
            # In exact mode, every field is supplied explicitly; clear flags map to None.
            body_cron = cron_expression
            body_tz = timezone
            body_enabled = enabled
            body_params = None if clear_parameters else parameters
            body_name = None if clear_name else name
            body_description = None if clear_description else description
        else:
            try:
                existing_resp = await skyvern.schedules.get(workflow_permanent_id, workflow_schedule_id)
                timer.mark("sdk_fetch")
            except ApiError as e:
                if e.status_code == 404:
                    return make_result(
                        "skyvern_schedule_update",
                        ok=False,
                        timing_ms=timer.timing_ms,
                        error=make_error(
                            ErrorCode.INVALID_INPUT,
                            f"Schedule {workflow_schedule_id!r} not found",
                            "Use skyvern_schedule_list to find valid schedule IDs.",
                        ),
                    )
                return _api_error("skyvern_schedule_update", e, timer)
            except Exception as e:
                return _api_error("skyvern_schedule_update", e, timer)

            existing = existing_resp.schedule
            # Log the snapshot's modified_at so an audit can reconstruct any window
            # clobbered by a concurrent writer. Callers wanting strict semantics
            # should pass exact=True (skips this fetch).
            LOG.info(
                "Workflow schedule fetch+merge snapshot",
                workflow_permanent_id=workflow_permanent_id,
                workflow_schedule_id=workflow_schedule_id,
                snapshot_modified_at=existing.modified_at.isoformat() if existing.modified_at else None,
            )

            body_cron = cron_expression if cron_expression is not None else existing.cron_expression
            body_tz = timezone if timezone is not None else existing.timezone
            body_enabled = enabled if enabled is not None else existing.enabled
            body_params = None if clear_parameters else (parameters if parameters is not None else existing.parameters)
            body_name = None if clear_name else (name if name is not None else existing.name)
            body_description = (
                None if clear_description else (description if description is not None else existing.description)
            )

        try:
            resp = await skyvern.schedules.update(
                workflow_permanent_id,
                workflow_schedule_id,
                cron_expression=body_cron,
                timezone=body_tz,
                enabled=body_enabled,
                parameters=body_params,
                name=body_name,
                description=body_description,
            )
            timer.mark("sdk_update")
        except ApiError as e:
            if e.status_code == 404:
                return make_result(
                    "skyvern_schedule_update",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        f"Schedule {workflow_schedule_id!r} not found",
                        "Use skyvern_schedule_list to find valid schedule IDs.",
                    ),
                )
            return _api_error("skyvern_schedule_update", e, timer, hint="Check cron, timezone, and parameter shape.")
        except Exception as e:
            return _api_error("skyvern_schedule_update", e, timer, hint="Check cron, timezone, and parameter shape.")

    return make_result(
        "skyvern_schedule_update",
        data=_serialize_schedule_response(resp),
        timing_ms=timer.timing_ms,
    )


def _not_found_404(action: str, workflow_schedule_id: str, timer: Timer) -> dict[str, Any]:
    return make_result(
        action,
        ok=False,
        timing_ms=timer.timing_ms,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            f"Workflow or schedule not found: {workflow_schedule_id!r}",
            "Use skyvern_schedule_list to find valid IDs.",
        ),
    )


async def skyvern_schedule_enable(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
    workflow_schedule_id: Annotated[str, Field(description="Schedule ID (starts with wfs_)")],
) -> dict[str, Any]:
    """Enable a paused workflow schedule."""
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_enable"):
        return err
    if err := validate_schedule_id(workflow_schedule_id, "skyvern_schedule_enable"):
        return err

    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            resp = await skyvern.schedules.enable(workflow_permanent_id, workflow_schedule_id)
            timer.mark("sdk")
        except ApiError as e:
            if e.status_code == 404:
                return _not_found_404("skyvern_schedule_enable", workflow_schedule_id, timer)
            return _api_error("skyvern_schedule_enable", e, timer)
        except Exception as e:
            return _api_error("skyvern_schedule_enable", e, timer)

    return make_result(
        "skyvern_schedule_enable",
        data=_serialize_schedule_response(resp),
        timing_ms=timer.timing_ms,
    )


async def skyvern_schedule_disable(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
    workflow_schedule_id: Annotated[str, Field(description="Schedule ID (starts with wfs_)")],
) -> dict[str, Any]:
    """Disable an active workflow schedule."""
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_disable"):
        return err
    if err := validate_schedule_id(workflow_schedule_id, "skyvern_schedule_disable"):
        return err

    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            resp = await skyvern.schedules.disable(workflow_permanent_id, workflow_schedule_id)
            timer.mark("sdk")
        except ApiError as e:
            if e.status_code == 404:
                return _not_found_404("skyvern_schedule_disable", workflow_schedule_id, timer)
            return _api_error("skyvern_schedule_disable", e, timer)
        except Exception as e:
            return _api_error("skyvern_schedule_disable", e, timer)

    return make_result(
        "skyvern_schedule_disable",
        data=_serialize_schedule_response(resp),
        timing_ms=timer.timing_ms,
    )


async def skyvern_schedule_delete(
    workflow_permanent_id: Annotated[str, Field(description="Workflow ID (starts with wpid_)")],
    workflow_schedule_id: Annotated[str, Field(description="Schedule ID (starts with wfs_)")],
    force: Annotated[
        bool,
        Field(description="Must be true to confirm deletion — prevents accidental deletes."),
    ] = False,
) -> dict[str, Any]:
    """Delete a workflow schedule. Irreversible — schedule stops firing immediately."""
    if err := validate_workflow_id(workflow_permanent_id, "skyvern_schedule_delete"):
        return err
    if err := validate_schedule_id(workflow_schedule_id, "skyvern_schedule_delete"):
        return err

    if not force:
        return _input_error(
            "skyvern_schedule_delete",
            f"Deletion of schedule {workflow_schedule_id!r} requires confirmation",
            "Set force=true to confirm deletion. This is irreversible — the schedule will stop firing immediately.",
        )

    skyvern = get_skyvern()
    with Timer() as timer:
        try:
            await skyvern.schedules.delete(workflow_permanent_id, workflow_schedule_id)
            timer.mark("sdk")
        except Exception as e:
            return _api_error("skyvern_schedule_delete", e, timer)

    # Reaching this point means the SDK returned without raising — that is the
    # success signal. DeleteScheduleResponse.ok is informational only.
    return make_result(
        "skyvern_schedule_delete",
        data={
            "workflow_permanent_id": workflow_permanent_id,
            "workflow_schedule_id": workflow_schedule_id,
            "deleted": True,
        },
        timing_ms=timer.timing_ms,
    )


__all__ = [
    "skyvern_schedule_create",
    "skyvern_schedule_delete",
    "skyvern_schedule_disable",
    "skyvern_schedule_enable",
    "skyvern_schedule_get",
    "skyvern_schedule_list",
    "skyvern_schedule_list_for_workflow",
    "skyvern_schedule_update",
]
