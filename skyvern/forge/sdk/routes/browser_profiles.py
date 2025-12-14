import asyncio
from typing import NoReturn

import structlog
from fastapi import Depends, HTTPException, Path, Query, status
from sqlalchemy.exc import IntegrityError

from skyvern.exceptions import (
    BrowserProfileNotFound,
    BrowserSessionNotFound,
    WorkflowNotFound,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.routes.code_samples import (
    CREATE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    CREATE_BROWSER_PROFILE_CODE_SAMPLE_TS,
    DELETE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    DELETE_BROWSER_PROFILE_CODE_SAMPLE_TS,
    GET_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    GET_BROWSER_PROFILE_CODE_SAMPLE_TS,
    GET_BROWSER_PROFILES_CODE_SAMPLE_PYTHON,
    GET_BROWSER_PROFILES_CODE_SAMPLE_TS,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.browser_profiles import (
    BrowserProfile,
    CreateBrowserProfileRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service

LOG = structlog.get_logger()


def _handle_duplicate_profile_name(*, organization_id: str, name: str, exc: IntegrityError) -> NoReturn:
    LOG.warning(
        "Duplicate browser profile name",
        organization_id=organization_id,
        name=name,
        exc_info=True,
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"A browser profile named '{name}' already exists. Use a different name or delete the existing profile.",
    ) from exc


@base_router.post(
    "/browser_profiles",
    response_model=BrowserProfile,
    tags=["Browser Profiles"],
    summary="Create a browser profile",
    description="Create a browser profile from a persistent browser session or workflow run.",
    openapi_extra={
        "x-fern-sdk-method-name": "create_browser_profile",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": CREATE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": CREATE_BROWSER_PROFILE_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    responses={
        200: {"description": "Successfully created browser profile"},
        400: {"description": "Invalid request - missing source or source not found"},
        409: {"description": "Browser profile name already exists"},
    },
)
@base_router.post(
    "/browser_profiles/",
    response_model=BrowserProfile,
    include_in_schema=False,
)
async def create_browser_profile(
    request: CreateBrowserProfileRequest,
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserProfile:
    organization_id = current_org.organization_id
    LOG.info(
        "Creating browser profile",
        organization_id=organization_id,
        browser_session_id=request.browser_session_id,
        workflow_run_id=request.workflow_run_id,
    )

    if request.browser_session_id:
        browser_session_id = request.browser_session_id
        return await _create_profile_from_session(
            organization_id=organization_id,
            name=request.name,
            description=request.description,
            browser_session_id=browser_session_id,
        )

    workflow_run_id = request.workflow_run_id
    assert workflow_run_id is not None  # model validator guarantees one of the sources
    return await _create_profile_from_workflow_run(
        organization_id=organization_id,
        name=request.name,
        description=request.description,
        workflow_run_id=workflow_run_id,
    )


@base_router.get(
    "/browser_profiles",
    response_model=list[BrowserProfile],
    tags=["Browser Profiles"],
    summary="List browser profiles",
    description="Get all browser profiles for the organization",
    openapi_extra={
        "x-fern-sdk-method-name": "list_browser_profiles",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_BROWSER_PROFILES_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_BROWSER_PROFILES_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
    responses={
        200: {"description": "Successfully retrieved browser profiles"},
    },
)
@base_router.get(
    "/browser_profiles/",
    response_model=list[BrowserProfile],
    include_in_schema=False,
)
async def list_browser_profiles(
    include_deleted: bool = Query(default=False, description="Include deleted browser profiles"),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[BrowserProfile]:
    """List all browser profiles for the current organization."""
    organization_id = current_org.organization_id
    LOG.info(
        "Listing browser profiles",
        organization_id=organization_id,
        include_deleted=include_deleted,
    )

    profiles = await app.DATABASE.list_browser_profiles(
        organization_id=organization_id,
        include_deleted=include_deleted,
    )

    LOG.info(
        "Listed browser profiles",
        organization_id=organization_id,
        count=len(profiles),
    )
    return profiles


@base_router.get(
    "/browser_profiles/{profile_id}",
    response_model=BrowserProfile,
    tags=["Browser Profiles"],
    summary="Get browser profile",
    description="Get a specific browser profile by ID",
    responses={
        200: {"description": "Successfully retrieved browser profile"},
        404: {"description": "Browser profile not found"},
    },
    openapi_extra={
        "x-fern-sdk-method-name": "get_browser_profile",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": GET_BROWSER_PROFILE_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": GET_BROWSER_PROFILE_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.get(
    "/browser_profiles/{profile_id}/",
    response_model=BrowserProfile,
    include_in_schema=False,
)
async def get_browser_profile(
    profile_id: str = Path(
        ...,
        description="The ID of the browser profile. browser_profile_id starts with `bp_`",
        examples=["bp_123456"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserProfile:
    """Get a browser profile for the current organization."""
    organization_id = current_org.organization_id
    LOG.info(
        "Getting browser profile",
        organization_id=organization_id,
        browser_profile_id=profile_id,
    )

    profile = await app.DATABASE.get_browser_profile(
        profile_id=profile_id,
        organization_id=organization_id,
    )

    if not profile:
        LOG.warning(
            "Browser profile not found",
            organization_id=organization_id,
            browser_profile_id=profile_id,
        )
        raise BrowserProfileNotFound(profile_id=profile_id, organization_id=organization_id)

    LOG.info(
        "Retrieved browser profile",
        organization_id=organization_id,
        browser_profile_id=profile_id,
    )
    return profile


@base_router.delete(
    "/browser_profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Browser Profiles"],
    summary="Delete browser profile",
    description="Delete a browser profile (soft delete)",
    responses={
        204: {"description": "Successfully deleted browser profile"},
        404: {"description": "Browser profile not found"},
    },
    openapi_extra={
        "x-fern-sdk-method-name": "delete_browser_profile",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": DELETE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": DELETE_BROWSER_PROFILE_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.delete(
    "/browser_profiles/{profile_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def delete_browser_profile(
    profile_id: str = Path(
        ...,
        description="The ID of the browser profile to delete. browser_profile_id starts with `bp_`",
        examples=["bp_123456"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> None:
    """Delete a browser profile for the current organization."""
    organization_id = current_org.organization_id
    LOG.info(
        "Deleting browser profile",
        organization_id=organization_id,
        browser_profile_id=profile_id,
    )

    try:
        await app.DATABASE.delete_browser_profile(
            profile_id=profile_id,
            organization_id=organization_id,
        )
    except BrowserProfileNotFound:
        LOG.warning(
            "Browser profile not found for deletion",
            organization_id=organization_id,
            browser_profile_id=profile_id,
        )
        raise

    LOG.info(
        "Deleted browser profile",
        organization_id=organization_id,
        browser_profile_id=profile_id,
    )


async def _create_profile_from_session(
    *,
    organization_id: str,
    name: str,
    description: str | None,
    browser_session_id: str,
) -> BrowserProfile:
    browser_session = await app.DATABASE.get_persistent_browser_session(browser_session_id, organization_id)
    if browser_session is None:
        LOG.warning(
            "Browser session not found for profile creation",
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )
        raise BrowserSessionNotFound(browser_session_id)

    session_dir = await app.STORAGE.retrieve_browser_profile(
        organization_id=organization_id,
        profile_id=browser_session_id,
    )
    if not session_dir:
        LOG.warning(
            "Browser session archive not found for profile creation",
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Browser session does not have a persisted profile archive. "
                "Close the session and wait for upload before creating a browser profile."
            ),
        )

    try:
        profile = await app.DATABASE.create_browser_profile(
            organization_id=organization_id,
            name=name,
            description=description,
        )
    except IntegrityError as exc:
        _handle_duplicate_profile_name(organization_id=organization_id, name=name, exc=exc)

    try:
        await app.STORAGE.store_browser_profile(
            organization_id=organization_id,
            profile_id=profile.browser_profile_id,
            directory=session_dir,
        )
    except Exception:
        # Rollback: delete the profile if storage fails
        await app.DATABASE.delete_browser_profile(profile.browser_profile_id, organization_id=organization_id)
        LOG.error(
            "Failed to store browser profile artifacts, rolled back profile creation",
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
            exc_info=True,
        )
        raise

    LOG.info(
        "Created browser profile from session",
        organization_id=organization_id,
        browser_profile_id=profile.browser_profile_id,
        browser_session_id=browser_session_id,
    )
    return profile


async def _create_profile_from_workflow_run(
    *,
    organization_id: str,
    name: str,
    description: str | None,
    workflow_run_id: str,
) -> BrowserProfile:
    workflow_run = await app.DATABASE.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        LOG.warning(
            "Workflow run not found for profile creation",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
        )
        raise WorkflowRunNotFound(workflow_run_id)

    workflow = await app.DATABASE.get_workflow(
        workflow_id=workflow_run.workflow_id,
        organization_id=organization_id,
    )
    if not workflow:
        LOG.warning(
            "Workflow not found for profile creation",
            organization_id=organization_id,
            workflow_id=workflow_run.workflow_id,
            workflow_permanent_id=workflow_run.workflow_permanent_id,
        )
        raise WorkflowNotFound(workflow_id=workflow_run.workflow_id)

    if not getattr(workflow, "persist_browser_session", False):
        LOG.warning(
            "Workflow does not persist browser sessions",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow does not persist browser sessions",
        )

    # The session persistence task runs asynchronously after workflow completion.
    # Poll for a short grace period so that immediate profile-creation requests
    # succeed without forcing clients to implement retry loops.
    poll_attempts = 30  # ~30 s max wait
    session_dir: str | None = None
    for attempt in range(poll_attempts):
        session_dir = await app.STORAGE.retrieve_browser_session(
            organization_id=organization_id,
            workflow_permanent_id=workflow.workflow_permanent_id,
        )
        if session_dir:
            break  # session found
        # Avoid busy-waiting; sleep 1 s between attempts (non-blocking asyncio sleep)
        if attempt < poll_attempts - 1:
            await asyncio.sleep(1)

    if not session_dir:
        LOG.warning(
            "Workflow run has no persisted session after waiting",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow run does not have a persisted session",
        )

    try:
        profile = await app.DATABASE.create_browser_profile(
            organization_id=organization_id,
            name=name,
            description=description,
        )
    except IntegrityError as exc:
        _handle_duplicate_profile_name(organization_id=organization_id, name=name, exc=exc)

    try:
        await app.STORAGE.store_browser_profile(
            organization_id=organization_id,
            profile_id=profile.browser_profile_id,
            directory=session_dir,
        )
        LOG.info(
            "Created browser profile from workflow run",
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
            workflow_run_id=workflow_run_id,
        )
    except Exception:
        # Rollback: delete the profile if storage fails
        await app.DATABASE.delete_browser_profile(profile.browser_profile_id, organization_id=organization_id)
        LOG.error(
            "Failed to store browser profile artifacts, rolled back profile creation",
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        raise

    return profile
