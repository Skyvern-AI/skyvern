import asyncio
import base64
import binascii
import json
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Depends, HTTPException, Path, Query, Request
from fastapi.responses import ORJSONResponse
from pydantic import ValidationError

from skyvern import analytics
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.routes.code_samples import (
    CLOSE_BROWSER_SESSION_CODE_SAMPLE_PYTHON,
    CLOSE_BROWSER_SESSION_CODE_SAMPLE_TS,
    CREATE_BROWSER_SESSION_CODE_SAMPLE_PYTHON,
    CREATE_BROWSER_SESSION_CODE_SAMPLE_TS,
    GET_BROWSER_SESSION_CODE_SAMPLE_PYTHON,
    GET_BROWSER_SESSION_CODE_SAMPLE_TS,
    GET_BROWSER_SESSIONS_CODE_SAMPLE_PYTHON,
    GET_BROWSER_SESSIONS_CODE_SAMPLE_TS,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import is_final_status
from skyvern.forge.sdk.services import org_auth_service
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.schemas.action_log import (
    ACTION_LOG_DEFAULT_PAGE_SIZE,
    ACTION_LOG_MAX_BODY_BYTES,
    ACTION_LOG_MAX_PAGE_SIZE,
    ActionLogBatchRequest,
    ActionLogBatchResponse,
    ActionLogEvent,
    ActionLogPage,
    sanitize_action_log_event,
)
from skyvern.schemas.browser_sessions import (
    CreateBrowserSessionRequest,
    ProcessBrowserSessionRecordingRequest,
    ProcessBrowserSessionRecordingResponse,
    UpdateBrowserSessionRequest,
)
from skyvern.schemas.proxy_pinning import should_generate_proxy_session_id
from skyvern.schemas.runs import ProxyLocation
from skyvern.webeye.schemas import BrowserSessionResponse

LOG = structlog.get_logger(__name__)

_ACTION_LOG_MAX_PAST_AGE = timedelta(days=7)
_ACTION_LOG_MAX_FUTURE_SKEW = timedelta(minutes=5)
_ACTION_LOG_MAX_CURSOR_LENGTH = 256


def _browser_session_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "browser_session_not_found"})


async def _read_action_log_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > ACTION_LOG_MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail={"code": "action_log_body_too_large"})
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > ACTION_LOG_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail={"code": "action_log_body_too_large"})
    return bytes(body)


def _validate_action_log_timestamps(events: list[ActionLogEvent]) -> None:
    now = datetime.now(timezone.utc)
    earliest = now - _ACTION_LOG_MAX_PAST_AGE
    latest = now + _ACTION_LOG_MAX_FUTURE_SKEW
    if any(event.occurred_at < earliest or event.occurred_at > latest for event in events):
        raise HTTPException(status_code=422, detail={"code": "invalid_action_log_timestamp"})


def _encode_action_log_cursor(artifact: Artifact) -> str:
    created_at = artifact.created_at
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    value = json.dumps([created_at.isoformat(), artifact.artifact_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_action_log_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if cursor is None:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at_raw, artifact_id = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(created_at_raw, str) or not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError
        created_at = datetime.fromisoformat(created_at_raw)
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        return created_at, artifact_id
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_action_log_cursor"}) from exc


@base_router.get(
    "/browser_sessions/history",
    include_in_schema=False,
)
@base_router.get(
    "/browser_sessions/history/",
    include_in_schema=False,
)
async def get_browser_sessions_all(
    current_org: Organization = Depends(org_auth_service.get_current_org),
    page: int = Query(1, ge=1, description="Page number for pagination"),
    page_size: int = Query(10, ge=1, le=100, description="Number of items per page"),
) -> list[BrowserSessionResponse]:
    """Get all browser sessions for the organization"""
    analytics.capture("skyvern-oss-agent-browser-sessions-get-all")

    browser_sessions = await app.DATABASE.browser_sessions.get_persistent_browser_sessions_history(
        current_org.organization_id,
        page=page,
        page_size=page_size,
    )

    responses = await asyncio.gather(
        *[
            BrowserSessionResponse.from_browser_session(browser_session, app.STORAGE)
            for browser_session in browser_sessions
        ]
    )

    return responses


@base_router.post(
    "/browser_sessions",
    response_model=BrowserSessionResponse,
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-method-name": "create_browser_session",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": CREATE_BROWSER_SESSION_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": CREATE_BROWSER_SESSION_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Create a browser session that persists across multiple runs",
    summary="Create a session",
    responses={
        200: {"description": "Successfully created browser session"},
        403: {"description": "Unauthorized - Invalid or missing authentication"},
        404: {"description": "Browser profile not found"},
    },
)
@base_router.post(
    "/browser_sessions/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def create_browser_session(
    browser_session_request: CreateBrowserSessionRequest = CreateBrowserSessionRequest(),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    proxy_location = browser_session_request.proxy_location
    proxy_session_id = browser_session_request.proxy_session_id
    if browser_session_request.browser_profile_id:
        profile = await app.DATABASE.browser_sessions.get_browser_profile(
            browser_session_request.browser_profile_id,
            current_org.organization_id,
        )
        if not profile:
            raise HTTPException(
                status_code=404,
                detail=f"Browser profile {browser_session_request.browser_profile_id} not found",
            )
        proxy_location_was_set = "proxy_location" in browser_session_request.model_fields_set
        if not proxy_location_was_set:
            proxy_location = profile.proxy_location
        if "proxy_session_id" not in browser_session_request.model_fields_set:
            if not proxy_location_was_set or should_generate_proxy_session_id(proxy_location):
                proxy_session_id = profile.proxy_session_id
            else:
                proxy_session_id = None
    if proxy_session_id and proxy_location is None:
        proxy_location = ProxyLocation.RESIDENTIAL_ISP
    if proxy_session_id and not should_generate_proxy_session_id(proxy_location):
        raise HTTPException(
            status_code=400,
            detail="proxy_session_id is only supported with RESIDENTIAL_ISP proxy_location",
        )

    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
        organization_id=current_org.organization_id,
        url=browser_session_request.url,
        timeout_minutes=browser_session_request.timeout,
        proxy_location=proxy_location,
        proxy_session_id=proxy_session_id,
        extensions=browser_session_request.extensions,
        browser_type=browser_session_request.browser_type,
        browser_profile_id=browser_session_request.browser_profile_id,
        generate_browser_profile=browser_session_request.generate_browser_profile,
    )
    return await BrowserSessionResponse.from_browser_session(browser_session)


@base_router.post(
    "/browser_sessions/{browser_session_id}/close",
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-method-name": "close_browser_session",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": CLOSE_BROWSER_SESSION_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": CLOSE_BROWSER_SESSION_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Close a session. Once closed, the session cannot be used again.",
    summary="Close a session",
    responses={
        200: {"description": "Successfully closed browser session"},
        404: {"description": "Browser session not found"},
        403: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@base_router.post(
    "/browser_sessions/{browser_session_id}/close/",
    include_in_schema=False,
)
async def close_browser_session(
    browser_session_id: str = Path(
        ...,
        description="The ID of the browser session to close. completed_at will be set when the browser session is closed. browser_session_id starts with `pbs_`",
        examples=["pbs_123456"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ORJSONResponse:
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if not browser_session:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")
    await app.PERSISTENT_SESSIONS_MANAGER.close_session(current_org.organization_id, browser_session_id)
    return ORJSONResponse(
        content={"message": "Browser session closed"},
        status_code=200,
        media_type="application/json",
    )


@base_router.patch(
    "/browser_sessions/{browser_session_id}",
    response_model=BrowserSessionResponse,
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-method-name": "update_browser_session",
    },
    description=(
        "Update a live browser session. Currently supports toggling generate_browser_profile, which is read "
        "when the session ends to decide whether to save its browser profile."
    ),
    summary="Update a session",
    responses={
        200: {"description": "Successfully updated browser session"},
        404: {"description": "Browser session not found"},
        403: {"description": "Unauthorized - Invalid or missing authentication"},
        409: {"description": "Conflict - browser session has already ended and can no longer be updated"},
    },
)
@base_router.patch(
    "/browser_sessions/{browser_session_id}/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def update_browser_session(
    request: UpdateBrowserSessionRequest,
    browser_session_id: str = Path(
        ..., description="The ID of the browser session. browser_session_id starts with `pbs_`", examples=["pbs_123456"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    existing = await app.PERSISTENT_SESSIONS_MANAGER.get_session(browser_session_id, current_org.organization_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")
    # The flag is read at teardown, so toggling it after the session has ended has no effect and
    # would make a later profile-creation attempt treat the missing archive as a transient upload.
    if is_final_status(existing.status):
        raise HTTPException(
            status_code=409,
            detail=f"Browser session {browser_session_id} has already ended and can no longer be updated.",
        )
    updated = await app.DATABASE.browser_sessions.update_persistent_browser_session(
        browser_session_id,
        organization_id=current_org.organization_id,
        generate_browser_profile=request.generate_browser_profile,
    )
    return await BrowserSessionResponse.from_browser_session(updated, app.STORAGE)


@base_router.get(
    "/browser_sessions/{browser_session_id}",
    response_model=BrowserSessionResponse,
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_browser_session",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_BROWSER_SESSION_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_BROWSER_SESSION_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Get details about a specific browser session, including the browser address for cdp connection.",
    summary="Get a session",
    responses={
        200: {"description": "Successfully retrieved browser session details"},
        404: {"description": "Browser session not found"},
        403: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@base_router.get(
    "/browser_sessions/{browser_session_id}/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def get_browser_session(
    browser_session_id: str = Path(
        ..., description="The ID of the browser session. browser_session_id starts with `pbs_`", examples=["pbs_123456"]
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    analytics.capture("skyvern-oss-agent-browser-session-get")
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if not browser_session:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")
    return await BrowserSessionResponse.from_browser_session(browser_session, app.STORAGE)


@base_router.get(
    "/browser_sessions/{browser_session_id}/workflow_runs",
    response_model=list[WorkflowRun],
    include_in_schema=False,
)
@base_router.get(
    "/browser_sessions/{browser_session_id}/workflow_runs/",
    response_model=list[WorkflowRun],
    include_in_schema=False,
)
async def get_workflow_runs_for_browser_session(
    browser_session_id: str = Path(
        ..., description="The ID of the browser session. browser_session_id starts with `pbs_`", examples=["pbs_123456"]
    ),
    page: int = Query(1, ge=1, description="Page number for pagination"),
    page_size: int = Query(10, ge=1, le=100, description="Number of items per page"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[WorkflowRun]:
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if not browser_session:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")
    return await app.WORKFLOW_SERVICE.get_workflow_runs_for_browser_session(
        browser_session_id=browser_session_id,
        organization_id=current_org.organization_id,
        page=page,
        page_size=page_size,
    )


@base_router.get(
    "/browser_sessions",
    response_model=list[BrowserSessionResponse],
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-method-name": "get_browser_sessions",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_BROWSER_SESSIONS_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_BROWSER_SESSIONS_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    description="Get all active browser sessions for the organization",
    summary="Get active browser sessions",
    responses={
        200: {"description": "Successfully retrieved all active browser sessions"},
        403: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@base_router.get(
    "/browser_sessions/",
    response_model=list[BrowserSessionResponse],
    include_in_schema=False,
)
async def get_browser_sessions(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[BrowserSessionResponse]:
    """Get all active browser sessions for the organization"""
    analytics.capture("skyvern-oss-agent-browser-sessions-get")
    browser_sessions = await app.PERSISTENT_SESSIONS_MANAGER.get_active_sessions(current_org.organization_id)
    return await asyncio.gather(
        *[
            BrowserSessionResponse.from_browser_session(browser_session, app.STORAGE)
            for browser_session in browser_sessions
        ]
    )


@base_router.post(
    "/browser_sessions/{browser_session_id}/action_logs",
    response_model=ActionLogBatchResponse,
    include_in_schema=False,
)
async def create_browser_session_action_logs(
    request: Request,
    browser_session_id: str = Path(..., description="The ID of the browser session.", examples=["pbs_123456"]),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ActionLogBatchResponse:
    body = await _read_action_log_body(request)
    try:
        batch = ActionLogBatchRequest.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_action_log_batch"}) from exc
    events = [sanitize_action_log_event(event) for event in batch.events]
    _validate_action_log_timestamps(events)

    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if browser_session is None:
        raise _browser_session_not_found()

    for event in events:
        await app.ARTIFACT_MANAGER.create_browser_session_action_log_artifact(
            organization_id=current_org.organization_id,
            browser_session_id=browser_session_id,
            event=event,
        )
    return ActionLogBatchResponse(accepted=len(events))


@base_router.get(
    "/browser_sessions/{browser_session_id}/action_logs",
    response_model=ActionLogPage,
    include_in_schema=False,
)
async def get_browser_session_action_logs(
    browser_session_id: str = Path(..., description="The ID of the browser session.", examples=["pbs_123456"]),
    cursor: str | None = Query(default=None, max_length=_ACTION_LOG_MAX_CURSOR_LENGTH),
    page_size: int = Query(default=ACTION_LOG_DEFAULT_PAGE_SIZE, ge=1, le=ACTION_LOG_MAX_PAGE_SIZE),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ActionLogPage:
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if browser_session is None:
        raise _browser_session_not_found()

    created_after, artifact_id_after = _decode_action_log_cursor(cursor)
    artifacts = await app.DATABASE.artifacts.list_artifacts_for_browser_session_by_type_after(
        browser_session_id=browser_session_id,
        organization_id=current_org.organization_id,
        artifact_type=ArtifactType.BROWSER_SESSION_ACTION_LOG,
        created_after=created_after,
        artifact_id_after=artifact_id_after,
        limit=page_size,
    )
    page_artifacts = artifacts
    events: list[ActionLogEvent] = []
    seen_event_ids: set[str] = set()
    for artifact in page_artifacts:
        data = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
        if data is None:
            LOG.warning(
                "Browser session action log artifact is unavailable",
                artifact_id=artifact.artifact_id,
                browser_session_id=browser_session_id,
            )
            continue
        try:
            event = ActionLogEvent.model_validate_json(data)
            event_id = str(event.event_id)
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            events.append(event)
        except ValidationError:
            LOG.warning(
                "Browser session action log artifact is invalid",
                artifact_id=artifact.artifact_id,
                browser_session_id=browser_session_id,
            )

    events.sort(key=lambda event: event.order_key)
    next_cursor = _encode_action_log_cursor(page_artifacts[-1]) if page_artifacts else None
    return ActionLogPage(events=events, next_cursor=next_cursor)


@base_router.post(
    "/browser_sessions/{browser_session_id}/process_recording",
    include_in_schema=False,
)
async def process_recording(
    browser_session_id: str = Path(..., description="The ID of the browser session.", examples=["pbs_123456"]),
    recording_request: ProcessBrowserSessionRecordingRequest = ProcessBrowserSessionRecordingRequest(),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ProcessBrowserSessionRecordingResponse:
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if not browser_session:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")

    blocks, parameters = await app.BROWSER_SESSION_RECORDING_SERVICE.process_recording(
        organization_id=current_org.organization_id,
        browser_session_id=browser_session_id,
        compressed_chunks=recording_request.compressed_chunks,
        workflow_permanent_id=recording_request.workflow_permanent_id,
        draft_steps=recording_request.draft_steps,
        code_first=recording_request.code_first,
    )

    return ProcessBrowserSessionRecordingResponse(blocks=blocks, parameters=parameters)
