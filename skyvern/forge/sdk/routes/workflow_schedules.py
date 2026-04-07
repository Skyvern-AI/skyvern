from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import Depends, HTTPException, Query, status

from skyvern.forge import app
from skyvern.forge.sdk.core.permissions.schedule_limit_checker import ScheduleLimitCheckerFactory
from skyvern.forge.sdk.db.agent_db import ScheduleLimitExceededError
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_schedules import (
    DeleteScheduleResponse,
    OrganizationScheduleListResponse,
    WorkflowSchedule,
    WorkflowScheduleListResponse,
    WorkflowScheduleResponse,
    WorkflowScheduleUpsertRequest,
)
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.forge.sdk.workflow.schedules import (
    calculate_next_runs,
    validate_cron_expression,
    validate_timezone_name,
)

LOG = structlog.get_logger()
DEFAULT_NEXT_RUNS_COUNT = 5
SCHEDULE_SYNC_ERROR_DETAIL = "Failed to sync schedule with scheduling service"


def _require_schedules_enabled() -> None:
    if not app.AGENT_FUNCTION.workflow_schedules_enabled:
        raise HTTPException(
            status_code=501,
            detail=(
                "Workflow schedules are disabled in this Skyvern build. "
                "Use Skyvern Cloud or override AgentFunction with a scheduling backend."
            ),
        )


def _schedule_sync_error() -> HTTPException:
    return HTTPException(status_code=502, detail=SCHEDULE_SYNC_ERROR_DETAIL)


async def _ensure_workflow_exists(workflow_permanent_id: str, organization_id: str) -> Workflow:
    workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization_id,
    )
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_permanent_id} not found")
    return workflow


async def _get_schedule_or_404(
    workflow_schedule_id: str,
    organization_id: str,
) -> WorkflowSchedule:
    schedule = await app.DATABASE.schedules.get_workflow_schedule_by_id(
        workflow_schedule_id=workflow_schedule_id,
        organization_id=organization_id,
    )
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )
    return schedule


def _next_runs(
    schedule: WorkflowSchedule,
    count: int = DEFAULT_NEXT_RUNS_COUNT,
) -> list[datetime]:
    return calculate_next_runs(schedule.cron_expression, schedule.timezone, count)


def _strip_none_parameters(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove keys whose value is None so they fall through to defaults at execution time."""
    if params is None:
        return None
    stripped = {k: v for k, v in params.items() if v is not None}
    return stripped or None


def _validate_request(cron_expression: str, timezone: str) -> None:
    try:
        validate_cron_expression(cron_expression)
        validate_timezone_name(timezone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


async def _set_schedule_enabled(
    workflow_permanent_id: str,
    workflow_schedule_id: str,
    organization: Organization,
    enabled: bool,
) -> WorkflowScheduleResponse:
    # Guard lives on both enable/disable route signatures via Depends, so no
    # direct call here — the dependency has already run by the time we arrive.
    await _ensure_workflow_exists(workflow_permanent_id, organization.organization_id)
    existing = await _get_schedule_or_404(
        workflow_schedule_id,
        organization.organization_id,
    )
    if existing.workflow_permanent_id != workflow_permanent_id:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )

    backend_schedule_id = existing.backend_schedule_id or app.AGENT_FUNCTION.build_workflow_schedule_id(
        existing.workflow_schedule_id
    )
    if backend_schedule_id is None and app.AGENT_FUNCTION.workflow_schedules_enabled:
        # Defensive: schedules are enabled but no backend id is available, so the
        # DB toggle below would drift from the execution backend. Surface it loudly
        # — callers (route Depends, batch jobs) should have caught this earlier.
        LOG.warning(
            "Toggling workflow schedule enabled state with no backend_schedule_id; DB and backend may drift",
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_schedule_id=workflow_schedule_id,
        )
    previous_enabled = existing.enabled

    schedule = await app.DATABASE.schedules.update_workflow_schedule_enabled(
        workflow_schedule_id=workflow_schedule_id,
        organization_id=organization.organization_id,
        enabled=enabled,
    )
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )

    if backend_schedule_id:
        try:
            await app.AGENT_FUNCTION.set_workflow_schedule_enabled(backend_schedule_id, enabled)
        except Exception as e:
            LOG.exception(
                "Failed to update workflow schedule enabled state on execution backend",
                workflow_schedule_id=workflow_schedule_id,
                backend_schedule_id=backend_schedule_id,
                enabled=enabled,
                error_type=type(e).__name__,
            )
            try:
                await app.DATABASE.schedules.update_workflow_schedule_enabled(
                    workflow_schedule_id=workflow_schedule_id,
                    organization_id=organization.organization_id,
                    enabled=previous_enabled,
                )
            except Exception as rollback_err:
                LOG.exception(
                    "CRITICAL: DB rollback failed after backend sync failure, DB and backend may be out of sync",
                    workflow_schedule_id=workflow_schedule_id,
                    backend_schedule_id=backend_schedule_id,
                    rollback_error=str(rollback_err),
                    original_error=str(e),
                )
            raise _schedule_sync_error() from e

    LOG.info(
        "Workflow schedule enabled state updated",
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_schedule_id=workflow_schedule_id,
        enabled=enabled,
    )
    return WorkflowScheduleResponse(schedule=schedule, next_runs=_next_runs(schedule))


# Each route is double-decorated: the legacy `@legacy_base_router.*` decorator is
# applied first (schema-hidden, mounted at /api/v1/ for backward compatibility),
# then `@base_router.*` wraps the already-registered function with the public
# /v1/ schema entry. Keep this order — reversing it would change which decorator
# sees the raw function if a future contributor adds middleware.
@legacy_base_router.get("/schedules", include_in_schema=False)
@base_router.get(
    "/schedules",
    response_model=OrganizationScheduleListResponse,
    tags=["Schedules"],
    operation_id="schedules_list_all",
    summary="List all schedules for the organization",
)
async def list_organization_schedules(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    status: Literal["active", "paused"] | None = Query(
        default=None, description="Filter by status: 'active' or 'paused'"
    ),
    search: str | None = Query(default=None, description="Search by workflow title or schedule name"),
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> OrganizationScheduleListResponse:
    enabled_filter: bool | None = None
    if status == "active":
        enabled_filter = True
    elif status == "paused":
        enabled_filter = False

    schedules, total_count = await app.DATABASE.schedules.list_organization_schedules(
        organization_id=organization.organization_id,
        page=page,
        page_size=page_size,
        enabled_filter=enabled_filter,
        search=search,
    )

    return OrganizationScheduleListResponse(
        schedules=schedules,
        total_count=total_count,
        page=page,
        page_size=page_size,
    )


@legacy_base_router.post("/workflows/{workflow_permanent_id}/schedules", include_in_schema=False)
@base_router.post(
    "/workflows/{workflow_permanent_id}/schedules",
    response_model=WorkflowScheduleResponse,
    tags=["Schedules"],
    operation_id="schedules_create",
    summary="Create a schedule for a workflow",
)
async def create_workflow_schedule(
    workflow_permanent_id: str,
    body: WorkflowScheduleUpsertRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> WorkflowScheduleResponse:
    _validate_request(body.cron_expression, body.timezone)
    workflow = await _ensure_workflow_exists(workflow_permanent_id, organization.organization_id)
    await app.WORKFLOW_SERVICE.validate_schedule_parameters(
        workflow=workflow,
        organization=organization,
        request_data=body.parameters,
    )
    stored_parameters = _strip_none_parameters(body.parameters)

    # Atomic limit check + insert
    max_schedules = await ScheduleLimitCheckerFactory.get_instance().get_schedule_limit(
        organization,
        workflow_permanent_id,
    )

    try:
        schedule, count = await app.DATABASE.schedules.create_workflow_schedule_with_limit(
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow_permanent_id,
            max_schedules=max_schedules,
            cron_expression=body.cron_expression,
            timezone=body.timezone,
            enabled=body.enabled,
            parameters=stored_parameters,
            name=body.name,
            description=body.description,
        )
    except ScheduleLimitExceededError as e:
        LOG.info(
            "Schedule limit exceeded",
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow_permanent_id,
            current_count=e.current_count,
            max_allowed=e.max_allowed,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Maximum of {e.max_allowed} schedules per workflow reached",
        ) from e

    # Auto-generate name if not provided
    if not body.name:
        auto_name = f"{workflow.title} - Schedule #{count + 1}"
        updated = await app.DATABASE.schedules.update_workflow_schedule(
            workflow_schedule_id=schedule.workflow_schedule_id,
            organization_id=organization.organization_id,
            cron_expression=body.cron_expression,
            timezone=body.timezone,
            enabled=body.enabled,
            parameters=stored_parameters,
            name=auto_name,
            description=body.description,
        )
        if updated:
            schedule = updated

    backend_schedule_id = app.AGENT_FUNCTION.build_workflow_schedule_id(schedule.workflow_schedule_id)
    # The 501 guard above ensures this route only runs when schedules are enabled,
    # in which case a conforming AgentFunction override must return a non-None id.
    if backend_schedule_id is None:
        LOG.error(
            "AgentFunction.build_workflow_schedule_id returned None while schedules are enabled",
            workflow_schedule_id=schedule.workflow_schedule_id,
        )
        await app.DATABASE.schedules.delete_workflow_schedule(
            workflow_schedule_id=schedule.workflow_schedule_id,
            organization_id=organization.organization_id,
        )
        raise _schedule_sync_error()

    try:
        result = await app.DATABASE.schedules.set_backend_schedule_id(
            workflow_schedule_id=schedule.workflow_schedule_id,
            organization_id=organization.organization_id,
            backend_schedule_id=backend_schedule_id,
        )
        if not result:
            await app.DATABASE.schedules.delete_workflow_schedule(
                workflow_schedule_id=schedule.workflow_schedule_id,
                organization_id=organization.organization_id,
            )
            raise _schedule_sync_error()
        schedule = result
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception(
            "Failed to persist backend schedule id, cleaning up",
            workflow_schedule_id=schedule.workflow_schedule_id,
        )
        await app.DATABASE.schedules.delete_workflow_schedule(
            workflow_schedule_id=schedule.workflow_schedule_id,
            organization_id=organization.organization_id,
        )
        raise _schedule_sync_error() from e

    # Sync to execution backend. On failure, soft-delete the DB record as cleanup.
    try:
        await app.AGENT_FUNCTION.upsert_workflow_schedule(
            backend_schedule_id=backend_schedule_id,
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_schedule_id=schedule.workflow_schedule_id,
            cron_expression=body.cron_expression,
            timezone=body.timezone,
            enabled=body.enabled,
            parameters=stored_parameters,
        )
    except Exception as e:
        LOG.exception(
            "Failed to register schedule with execution backend, cleaning up DB record",
            workflow_permanent_id=workflow_permanent_id,
            error_type=type(e).__name__,
        )
        try:
            await app.DATABASE.schedules.delete_workflow_schedule(
                workflow_schedule_id=schedule.workflow_schedule_id,
                organization_id=organization.organization_id,
            )
        except Exception as cleanup_err:
            LOG.exception(
                "CRITICAL: Failed to clean up DB record after backend sync failure, "
                "orphaned schedule may block future creates until manually deleted",
                workflow_schedule_id=schedule.workflow_schedule_id,
                organization_id=organization.organization_id,
                cleanup_error=str(cleanup_err),
                original_error=str(e),
            )
        raise _schedule_sync_error() from e

    LOG.info(
        "Workflow schedule created",
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_schedule_id=schedule.workflow_schedule_id,
        cron_expression=body.cron_expression,
        enabled=body.enabled,
    )
    return WorkflowScheduleResponse(schedule=schedule, next_runs=_next_runs(schedule))


@legacy_base_router.get("/workflows/{workflow_permanent_id}/schedules", include_in_schema=False)
@base_router.get(
    "/workflows/{workflow_permanent_id}/schedules",
    response_model=WorkflowScheduleListResponse,
    tags=["Schedules"],
    operation_id="schedules_list",
    summary="List schedules for a workflow",
)
async def list_workflow_schedules(
    workflow_permanent_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> WorkflowScheduleListResponse:
    await _ensure_workflow_exists(workflow_permanent_id, organization.organization_id)
    schedules = await app.DATABASE.schedules.get_workflow_schedules(
        workflow_permanent_id=workflow_permanent_id,
        organization_id=organization.organization_id,
    )
    return WorkflowScheduleListResponse(schedules=schedules)


@legacy_base_router.get("/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}", include_in_schema=False)
@base_router.get(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}",
    response_model=WorkflowScheduleResponse,
    tags=["Schedules"],
    operation_id="schedules_get",
    summary="Get a workflow schedule by id",
)
async def get_workflow_schedule(
    workflow_permanent_id: str,
    workflow_schedule_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> WorkflowScheduleResponse:
    await _ensure_workflow_exists(workflow_permanent_id, organization.organization_id)
    schedule = await _get_schedule_or_404(
        workflow_schedule_id,
        organization.organization_id,
    )
    if schedule.workflow_permanent_id != workflow_permanent_id:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )
    return WorkflowScheduleResponse(schedule=schedule, next_runs=_next_runs(schedule))


@legacy_base_router.put("/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}", include_in_schema=False)
@base_router.put(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}",
    response_model=WorkflowScheduleResponse,
    tags=["Schedules"],
    operation_id="schedules_update",
    summary="Update a workflow schedule",
)
async def update_workflow_schedule(
    workflow_permanent_id: str,
    workflow_schedule_id: str,
    body: WorkflowScheduleUpsertRequest,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> WorkflowScheduleResponse:
    workflow = await _ensure_workflow_exists(workflow_permanent_id, organization.organization_id)
    existing = await _get_schedule_or_404(
        workflow_schedule_id,
        organization.organization_id,
    )
    if existing.workflow_permanent_id != workflow_permanent_id:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )

    _validate_request(body.cron_expression, body.timezone)
    await app.WORKFLOW_SERVICE.validate_schedule_parameters(
        workflow=workflow,
        organization=organization,
        request_data=body.parameters,
    )
    stored_parameters = _strip_none_parameters(body.parameters)

    # Prefer the id already persisted on the row; only re-derive when it's missing.
    # The `or` short-circuits, so a non-empty existing id is never overwritten.
    backend_schedule_id = existing.backend_schedule_id or app.AGENT_FUNCTION.build_workflow_schedule_id(
        existing.workflow_schedule_id
    )
    if backend_schedule_id is None:
        # The route-level _require_schedules_enabled dependency guarantees we only
        # reach this point when schedules are enabled, so a None here means a
        # misconfigured AgentFunction override (build returned None even though
        # workflow_schedules_enabled is True). Bail out so the DB doesn't drift.
        LOG.error(
            "AgentFunction.build_workflow_schedule_id returned None while schedules are enabled",
            workflow_schedule_id=workflow_schedule_id,
        )
        raise _schedule_sync_error()

    old_cron = existing.cron_expression
    old_timezone = existing.timezone
    old_enabled = existing.enabled
    old_parameters = existing.parameters
    old_name = existing.name
    old_description = existing.description

    schedule = await app.DATABASE.schedules.update_workflow_schedule(
        workflow_schedule_id=workflow_schedule_id,
        organization_id=organization.organization_id,
        cron_expression=body.cron_expression,
        timezone=body.timezone,
        enabled=body.enabled,
        parameters=stored_parameters,
        backend_schedule_id=backend_schedule_id,
        name=body.name,
        description=body.description,
    )
    if not schedule:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )

    try:
        await app.AGENT_FUNCTION.upsert_workflow_schedule(
            backend_schedule_id=backend_schedule_id,
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
            workflow_schedule_id=workflow_schedule_id,
            cron_expression=body.cron_expression,
            timezone=body.timezone,
            enabled=body.enabled,
            parameters=stored_parameters,
        )
    except Exception as e:
        LOG.exception(
            "Failed to update schedule on execution backend",
            workflow_permanent_id=workflow_permanent_id,
            backend_schedule_id=backend_schedule_id,
            error_type=type(e).__name__,
        )
        try:
            await app.DATABASE.schedules.update_workflow_schedule(
                workflow_schedule_id=workflow_schedule_id,
                organization_id=organization.organization_id,
                cron_expression=old_cron,
                timezone=old_timezone,
                enabled=old_enabled,
                parameters=old_parameters,
                backend_schedule_id=backend_schedule_id,
                name=old_name,
                description=old_description,
            )
        except Exception as rollback_err:
            LOG.exception(
                "CRITICAL: DB rollback failed after backend sync failure, DB and backend may be out of sync",
                workflow_permanent_id=workflow_permanent_id,
                backend_schedule_id=backend_schedule_id,
                rollback_error=str(rollback_err),
                original_error=str(e),
            )
        raise _schedule_sync_error() from e

    LOG.info(
        "Workflow schedule updated",
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_schedule_id=workflow_schedule_id,
        cron_expression=body.cron_expression,
        enabled=body.enabled,
    )
    return WorkflowScheduleResponse(schedule=schedule, next_runs=_next_runs(schedule))


@legacy_base_router.post(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}/enable",
    include_in_schema=False,
)
@base_router.post(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}/enable",
    response_model=WorkflowScheduleResponse,
    tags=["Schedules"],
    operation_id="schedules_enable",
    summary="Enable a workflow schedule",
)
async def enable_workflow_schedule(
    workflow_permanent_id: str,
    workflow_schedule_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> WorkflowScheduleResponse:
    return await _set_schedule_enabled(
        workflow_permanent_id=workflow_permanent_id,
        workflow_schedule_id=workflow_schedule_id,
        organization=organization,
        enabled=True,
    )


@legacy_base_router.post(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}/disable",
    include_in_schema=False,
)
@base_router.post(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}/disable",
    response_model=WorkflowScheduleResponse,
    tags=["Schedules"],
    operation_id="schedules_disable",
    summary="Disable a workflow schedule",
)
async def disable_workflow_schedule(
    workflow_permanent_id: str,
    workflow_schedule_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> WorkflowScheduleResponse:
    return await _set_schedule_enabled(
        workflow_permanent_id=workflow_permanent_id,
        workflow_schedule_id=workflow_schedule_id,
        organization=organization,
        enabled=False,
    )


@legacy_base_router.delete(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}", include_in_schema=False
)
@base_router.delete(
    "/workflows/{workflow_permanent_id}/schedules/{workflow_schedule_id}",
    response_model=DeleteScheduleResponse,
    tags=["Schedules"],
    operation_id="schedules_delete",
    summary="Delete a workflow schedule",
)
async def delete_workflow_schedule_route(
    workflow_permanent_id: str,
    workflow_schedule_id: str,
    organization: Organization = Depends(org_auth_service.get_current_org),
    _: None = Depends(_require_schedules_enabled),
) -> DeleteScheduleResponse:
    await _ensure_workflow_exists(workflow_permanent_id, organization.organization_id)
    existing = await _get_schedule_or_404(
        workflow_schedule_id,
        organization.organization_id,
    )
    if existing.workflow_permanent_id != workflow_permanent_id:
        raise HTTPException(
            status_code=404,
            detail=f"Schedule {workflow_schedule_id} not found",
        )

    await app.DATABASE.schedules.delete_workflow_schedule(
        workflow_schedule_id=workflow_schedule_id,
        organization_id=organization.organization_id,
    )

    if not existing.backend_schedule_id:
        LOG.warning(
            "Deleting workflow schedule with no backend_schedule_id; skipping backend sync",
            organization_id=organization.organization_id,
            workflow_permanent_id=workflow_permanent_id,
            workflow_schedule_id=workflow_schedule_id,
        )
    else:
        try:
            await app.AGENT_FUNCTION.delete_workflow_schedule(existing.backend_schedule_id)
        except Exception as e:
            LOG.exception(
                "Failed to delete schedule on execution backend, restoring DB record",
                backend_schedule_id=existing.backend_schedule_id,
            )
            try:
                await app.DATABASE.schedules.restore_workflow_schedule(
                    workflow_schedule_id=workflow_schedule_id,
                    organization_id=organization.organization_id,
                )
            except Exception as rollback_err:
                LOG.exception(
                    "CRITICAL: DB rollback failed after backend sync failure, DB and backend may be out of sync",
                    workflow_schedule_id=workflow_schedule_id,
                    backend_schedule_id=existing.backend_schedule_id,
                    rollback_error=str(rollback_err),
                    original_error=str(e),
                )
            raise _schedule_sync_error() from e

    LOG.info(
        "Workflow schedule deleted",
        organization_id=organization.organization_id,
        workflow_permanent_id=workflow_permanent_id,
        workflow_schedule_id=workflow_schedule_id,
    )
    return DeleteScheduleResponse(ok=True)
