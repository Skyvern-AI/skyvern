import asyncio

from fastapi import Depends, HTTPException, Path, Query
from fastapi.responses import ORJSONResponse

from skyvern import analytics
from skyvern.forge import app
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
from skyvern.forge.sdk.services import org_auth_service
from skyvern.schemas.browser_sessions import (
    CreateBrowserSessionRequest,
    ProcessBrowserSessionRecordingRequest,
    ProcessBrowserSessionRecordingResponse,
)
from skyvern.webeye.schemas import BrowserSessionResponse


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

    browser_sessions = await app.DATABASE.get_persistent_browser_sessions_history(
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
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(
        organization_id=current_org.organization_id,
        timeout_minutes=browser_session_request.timeout,
        proxy_location=browser_session_request.proxy_location,
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
    await app.PERSISTENT_SESSIONS_MANAGER.close_session(current_org.organization_id, browser_session_id)
    return ORJSONResponse(
        content={"message": "Browser session closed"},
        status_code=200,
        media_type="application/json",
    )


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
    )

    return ProcessBrowserSessionRecordingResponse(blocks=blocks, parameters=parameters)
