from fastapi import Depends, HTTPException
from fastapi.responses import ORJSONResponse

from skyvern import analytics
from skyvern.forge import app
from skyvern.forge.sdk.routes.routers import base_router, legacy_base_router
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service
from skyvern.webeye.schemas import BrowserSessionResponse


@base_router.get(
    "/browser_sessions/{browser_session_id}",
    response_model=BrowserSessionResponse,
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser_session",
        "x-fern-sdk-method-name": "get_browser_session",
    },
    description="Get details about a specific browser session by ID",
    summary="Get browser session details",
    responses={
        200: {"description": "Successfully retrieved browser session details"},
        404: {"description": "Browser session not found"},
        401: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@legacy_base_router.get(
    "/browser_sessions/{browser_session_id}",
    response_model=BrowserSessionResponse,
    tags=["session"],
    openapi_extra={
        "x-fern-sdk-group-name": "session",
        "x-fern-sdk-method-name": "get_browser_session",
    },
)
@legacy_base_router.get(
    "/browser_sessions/{browser_session_id}/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def get_browser_session(
    browser_session_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    analytics.capture("skyvern-oss-agent-browser-session-get")
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        browser_session_id,
        current_org.organization_id,
    )
    if not browser_session:
        raise HTTPException(status_code=404, detail=f"Browser session {browser_session_id} not found")
    return BrowserSessionResponse.from_browser_session(browser_session)


@base_router.get(
    "/browser_sessions",
    response_model=list[BrowserSessionResponse],
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser_session",
        "x-fern-sdk-method-name": "get_browser_sessions",
    },
    description="Get all active browser sessions for the organization",
    summary="Get all active browser sessions",
    responses={
        200: {"description": "Successfully retrieved all active browser sessions"},
        401: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@legacy_base_router.get(
    "/browser_sessions",
    response_model=list[BrowserSessionResponse],
    tags=["session"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser_sessions",
        "x-fern-sdk-method-name": "get_browser_sessions",
    },
)
@legacy_base_router.get(
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
    return [BrowserSessionResponse.from_browser_session(browser_session) for browser_session in browser_sessions]


@base_router.post(
    "/browser_sessions",
    response_model=BrowserSessionResponse,
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser_session",
        "x-fern-sdk-method-name": "create_browser_session",
    },
    description="Create a new browser session",
    summary="Create a new browser session",
    responses={
        200: {"description": "Successfully created browser session"},
        401: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@legacy_base_router.post(
    "/browser_sessions",
    response_model=BrowserSessionResponse,
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-group-name": "session",
        "x-fern-sdk-method-name": "create_browser_session",
    },
)
@legacy_base_router.post(
    "/browser_sessions/",
    response_model=BrowserSessionResponse,
    include_in_schema=False,
)
async def create_browser_session(
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserSessionResponse:
    browser_session = await app.PERSISTENT_SESSIONS_MANAGER.create_session(current_org.organization_id)
    return BrowserSessionResponse.from_browser_session(browser_session)


@base_router.post(
    "/browser_sessions/{browser_session_id}/close",
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser_session",
        "x-fern-sdk-method-name": "close_browser_session",
    },
    description="Close a browser session",
    summary="Close a browser session",
    responses={
        200: {"description": "Successfully closed browser session"},
        401: {"description": "Unauthorized - Invalid or missing authentication"},
    },
)
@legacy_base_router.post(
    "/browser_sessions/{browser_session_id}/close",
    tags=["Browser Sessions"],
    openapi_extra={
        "x-fern-sdk-group-name": "browser_session",
        "x-fern-sdk-method-name": "close_browser_session",
    },
)
@legacy_base_router.post(
    "/browser_sessions/{browser_session_id}/close/",
    include_in_schema=False,
)
async def close_browser_session(
    browser_session_id: str,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> ORJSONResponse:
    await app.PERSISTENT_SESSIONS_MANAGER.close_session(current_org.organization_id, browser_session_id)
    return ORJSONResponse(
        content={"message": "Browser session closed"},
        status_code=200,
        media_type="application/json",
    )
