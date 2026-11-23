from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.exc import DataError, OperationalError
from starlette.exceptions import HTTPException

from skyvern.client.core.api_error import ApiError
from skyvern.client.core.pydantic_utilities import parse_obj_as
from skyvern.client.types.workflow_schedule_response import WorkflowScheduleResponse as SdkWorkflowScheduleResponse
from skyvern.exceptions import SkyvernHTTPException
from skyvern.forge import app
from skyvern.forge.sdk.db.exceptions import DatabaseConnectionUnavailableError, NotFoundError, is_connection_failure
from skyvern.forge.sdk.routes.workflow_schedules import (
    cancel_workflow_schedule,
    create_workflow_schedule,
    disable_workflow_schedule,
    enable_workflow_schedule,
    get_workflow_schedule,
    require_schedules_enabled,
    update_workflow_schedule,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_schedules import WorkflowScheduleResponse, WorkflowScheduleUpsertRequest
from skyvern.forge.sdk.services.request_principal import get_request_principal

LOG = structlog.get_logger()

# The loopback SDK client these writes replaced gave up after 60 seconds.
_ROUTE_TIMEOUT_SECONDS = 60


def _log_late_failure(call: asyncio.Future[WorkflowScheduleResponse]) -> None:
    if not call.cancelled() and (exc := call.exception()) is not None:
        LOG.warning("In-process schedule write failed after its caller stopped waiting", exc_info=exc)


def validation_errors(exc: RequestValidationError | ValidationError) -> list[dict[str, Any]]:
    # Field, message and type only: an echoed input value could be a schedule parameter secret.
    return [{key: error[key] for key in ("loc", "msg", "type") if key in error} for error in exc.errors()]


async def _await_route(route_call: Awaitable[WorkflowScheduleResponse]) -> WorkflowScheduleResponse:
    call = asyncio.ensure_future(route_call)
    try:
        # Shielded: a write cut off midway skips the route's own cleanup, so it finishes as an HTTP request would.
        return await asyncio.wait_for(asyncio.shield(call), _ROUTE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        call.add_done_callback(_log_late_failure)
        raise
    except TimeoutError as exc:
        if call.done():
            # The route raised TimeoutError itself, or finished in the tick the deadline fired: report what it did.
            return call.result()
        call.add_done_callback(_log_late_failure)
        detail = (
            f"The schedule change did not finish within {_ROUTE_TIMEOUT_SECONDS}s and may still be saved; "
            "list the schedules before retrying it."
        )
        raise ApiError(status_code=504, body={"detail": detail}) from exc


def _unexpected(exc: Exception) -> ApiError:
    LOG.exception("In-process schedule write failed", exc_info=exc)
    return ApiError(status_code=500, body={"error": f"Unexpected error: {type(exc).__name__}"})


class ChatRequestScheduleWrites:
    """Runs the schedule routes in-process as the chat's own request, skipping their route dependencies and the cloud
    folder guard; the overlay's pre-hook must mirror any check added there.

    Results and errors take the shapes the SDK client gave over HTTP, so the shared tools cannot tell the paths apart.
    """

    def __init__(self, organization: Organization) -> None:
        self._organization = organization

    async def _call(
        self,
        route: Callable[..., Awaitable[WorkflowScheduleResponse]],
        *args: Any,
        body: dict[str, Any] | None = None,
        action: str | None = None,
    ) -> SdkWorkflowScheduleResponse:
        if body is not None:
            try:
                args = (*args, WorkflowScheduleUpsertRequest(**body))
            except ValidationError as exc:
                raise ApiError(status_code=422, body={"detail": validation_errors(exc)}) from exc
        try:
            require_schedules_enabled()
            if body is not None and action is not None:
                # Like the HTTP folder guard, judge the body the route will store: a partial update arrives here with
                # its stored values merged back in, which the overlay pre-hook never saw.
                await app.AGENT_FUNCTION.enforce_folder_access_for_references(
                    get_request_principal(), action, {**body, "workflow_permanent_id": args[0]}
                )
            response = await _await_route(route(*args, organization=self._organization, _=None))
        except ApiError:
            raise
        # The same status and body the app's exception handlers in api_app.py would have sent over HTTP.
        except HTTPException as exc:
            raise ApiError(status_code=exc.status_code, body={"detail": exc.detail}) from exc
        except SkyvernHTTPException as exc:
            content: dict[str, Any] = {"detail": exc.message}
            if exc.error_code is not None:
                content["error_code"] = exc.error_code
            raise ApiError(status_code=exc.status_code, body=content) from exc
        except (RequestValidationError, ValidationError) as exc:
            raise ApiError(status_code=422, body={"detail": validation_errors(exc)}) from exc
        except NotFoundError as exc:
            raise ApiError(status_code=404) from exc
        except DataError as exc:
            detail = "A value in the request cannot be stored: out of range, or containing a NUL byte."
            raise ApiError(status_code=422, body={"detail": detail}) from exc
        except (OperationalError, DatabaseConnectionUnavailableError) as exc:
            if isinstance(exc, OperationalError) and not is_connection_failure(exc.orig):
                raise _unexpected(exc) from exc
            raise ApiError(status_code=503, body={"error": "Database temporarily unavailable"}) from exc
        except Exception as exc:
            # str(exc) can carry raw SQL or internal hosts, so only the class name reaches the model.
            raise _unexpected(exc) from exc
        return parse_obj_as(SdkWorkflowScheduleResponse, response.model_dump(mode="json", by_alias=True))

    async def get(self, workflow_permanent_id: str, workflow_schedule_id: str) -> SdkWorkflowScheduleResponse:
        return await self._call(get_workflow_schedule, workflow_permanent_id, workflow_schedule_id)

    async def create(self, workflow_permanent_id: str, **body: Any) -> SdkWorkflowScheduleResponse:
        return await self._call(
            create_workflow_schedule, workflow_permanent_id, body=body, action="workflow_schedule.create"
        )

    async def update(
        self, workflow_permanent_id: str, workflow_schedule_id: str, **body: Any
    ) -> SdkWorkflowScheduleResponse:
        return await self._call(
            update_workflow_schedule,
            workflow_permanent_id,
            workflow_schedule_id,
            body=body,
            action="workflow_schedule.update",
        )

    async def enable(self, workflow_permanent_id: str, workflow_schedule_id: str) -> SdkWorkflowScheduleResponse:
        return await self._call(enable_workflow_schedule, workflow_permanent_id, workflow_schedule_id)

    async def disable(self, workflow_permanent_id: str, workflow_schedule_id: str) -> SdkWorkflowScheduleResponse:
        return await self._call(disable_workflow_schedule, workflow_permanent_id, workflow_schedule_id)

    async def cancel(self, workflow_permanent_id: str, workflow_schedule_id: str) -> SdkWorkflowScheduleResponse:
        return await self._call(cancel_workflow_schedule, workflow_permanent_id, workflow_schedule_id)
