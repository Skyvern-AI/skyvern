import asyncio
import shutil
from pathlib import Path as FilePath
from typing import NoReturn

import structlog
from fastapi import Depends, HTTPException, Path, Query, status
from sqlalchemy.exc import IntegrityError

from skyvern.config import settings
from skyvern.exceptions import (
    BrowserProfileNotFound,
    BrowserSessionNotFound,
    WorkflowNotFound,
    WorkflowRunNotFound,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.files import make_temp_directory
from skyvern.forge.sdk.routes.code_samples import (
    CREATE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    CREATE_BROWSER_PROFILE_CODE_SAMPLE_TS,
    DELETE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    DELETE_BROWSER_PROFILE_CODE_SAMPLE_TS,
    GET_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    GET_BROWSER_PROFILE_CODE_SAMPLE_TS,
    GET_BROWSER_PROFILES_CODE_SAMPLE_PYTHON,
    GET_BROWSER_PROFILES_CODE_SAMPLE_TS,
    UPDATE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON,
    UPDATE_BROWSER_PROFILE_CODE_SAMPLE_TS,
)
from skyvern.forge.sdk.routes.routers import base_router
from skyvern.forge.sdk.schemas.browser_profiles import (
    BrowserProfile,
    CreateBrowserProfileRequest,
    UpdateBrowserProfileRequest,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.persistent_browser_sessions import export_profile_storage_id
from skyvern.forge.sdk.services import org_auth_service

LOG = structlog.get_logger()

DEFAULT_PROFILE_BROWSER_TYPES = ("chrome", "chromium")
DEFAULT_PROFILE_COPY_IGNORE = {
    "Snapshots",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "Guest Profile",
    "Profile 2",
    "Profile 3",
    "BrowserMetrics",
    "Crashpad",
    "CrashpadMetrics-active.pma",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
    "DevToolsActivePort",
}


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
    description=("Create a blank browser profile, or create one from a persistent browser session or workflow run."),
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
        400: {"description": "Invalid request - source not found or source archive unavailable"},
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

    if request.browser_session_id is not None:
        browser_session_id = request.browser_session_id
        return await _create_profile_from_session(
            organization_id=organization_id,
            name=request.name,
            description=request.description,
            browser_session_id=browser_session_id,
        )

    if request.workflow_run_id is not None:
        return await _create_profile_from_workflow_run(
            organization_id=organization_id,
            name=request.name,
            description=request.description,
            workflow_run_id=request.workflow_run_id,
        )

    await app.RATE_LIMITER.rate_limit_submit_run(organization_id)
    return await _create_empty_profile(
        organization_id=organization_id,
        name=request.name,
        description=request.description,
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
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
    include_deleted: bool = Query(default=False, description="Include deleted browser profiles"),
    search_key: str | None = Query(
        None,
        description=(
            "Case-insensitive substring search across: browser profile name and description. "
            "A profile is returned if either field matches."
        ),
        examples=["my_profile", "production"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> list[BrowserProfile]:
    """List all browser profiles for the current organization."""
    organization_id = current_org.organization_id
    LOG.info(
        "Listing browser profiles",
        organization_id=organization_id,
        include_deleted=include_deleted,
        page=page,
        page_size=page_size,
        search_key=search_key,
    )

    profiles = await app.DATABASE.browser_sessions.list_browser_profiles(
        organization_id=organization_id,
        include_deleted=include_deleted,
        page=page,
        page_size=page_size,
        search_key=search_key,
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

    profile = await app.DATABASE.browser_sessions.get_browser_profile(
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


@base_router.patch(
    "/browser_profiles/{profile_id}",
    response_model=BrowserProfile,
    tags=["Browser Profiles"],
    summary="Update browser profile",
    description="Update a browser profile's name and/or description",
    responses={
        200: {"description": "Successfully updated browser profile"},
        404: {"description": "Browser profile not found"},
        409: {"description": "Browser profile name already exists"},
    },
    openapi_extra={
        "x-fern-sdk-method-name": "update_browser_profile",
        "x-fern-examples": [
            {
                "code-samples": [
                    {"sdk": "python", "code": UPDATE_BROWSER_PROFILE_CODE_SAMPLE_PYTHON},
                    {"sdk": "typescript", "code": UPDATE_BROWSER_PROFILE_CODE_SAMPLE_TS},
                ]
            }
        ],
    },
)
@base_router.patch(
    "/browser_profiles/{profile_id}/",
    response_model=BrowserProfile,
    include_in_schema=False,
)
async def update_browser_profile(
    request: UpdateBrowserProfileRequest,
    profile_id: str = Path(
        ...,
        description="The ID of the browser profile to update. browser_profile_id starts with `bp_`",
        examples=["bp_123456"],
    ),
    current_org: Organization = Depends(org_auth_service.get_current_org),
) -> BrowserProfile:
    organization_id = current_org.organization_id
    LOG.info(
        "Updating browser profile",
        organization_id=organization_id,
        browser_profile_id=profile_id,
    )

    try:
        profile = await app.DATABASE.browser_sessions.update_browser_profile(
            profile_id=profile_id,
            organization_id=organization_id,
            name=request.name,
            description=request.description,
        )
    except BrowserProfileNotFound:
        LOG.warning(
            "Browser profile not found for update",
            organization_id=organization_id,
            browser_profile_id=profile_id,
        )
        raise
    except IntegrityError as exc:
        if request.name is None:
            LOG.exception(
                "Unexpected integrity error on browser profile update without name change",
                organization_id=organization_id,
                browser_profile_id=profile_id,
            )
            raise
        _handle_duplicate_profile_name(organization_id=organization_id, name=request.name, exc=exc)

    LOG.info(
        "Updated browser profile",
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
        await app.DATABASE.browser_sessions.delete_browser_profile(
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

    # Reap the stored blob so soft-deleted profiles don't leave orphaned S3 objects behind.
    # Best-effort: the soft-delete already succeeded, so a reap failure must not fail the request.
    try:
        await app.STORAGE.delete_browser_profile(
            organization_id=organization_id,
            profile_id=profile_id,
        )
    except Exception:
        LOG.exception(
            "Failed to delete browser profile blob after soft-delete",
            organization_id=organization_id,
            browser_profile_id=profile_id,
        )

    LOG.info(
        "Deleted browser profile",
        organization_id=organization_id,
        browser_profile_id=profile_id,
    )


def _create_empty_browser_profile_directory() -> str:
    profile_dir = FilePath(make_temp_directory(prefix="skyvern_empty_browser_profile_"))
    _seed_empty_browser_profile_directory(profile_dir)
    return str(profile_dir)


def _default_browser_profile_template_candidates() -> list[FilePath]:
    if not settings.DEFAULT_BROWSER_PROFILE_DIR:
        return []

    base_dir = FilePath(settings.DEFAULT_BROWSER_PROFILE_DIR)
    candidates = [base_dir]
    for browser_type in DEFAULT_PROFILE_BROWSER_TYPES:
        candidates.extend(_versioned_browser_profile_template_candidates(base_dir, browser_type))
        candidates.append(base_dir / browser_type)
    return candidates


def _versioned_browser_profile_template_candidates(base_dir: FilePath, browser_type: str) -> list[FilePath]:
    prefix = f"{browser_type}_"
    candidates: list[tuple[int, FilePath]] = []
    try:
        children = list(base_dir.iterdir())
    except OSError:
        return []

    for child in children:
        if not child.is_dir() or not child.name.startswith(prefix):
            continue
        suffix = child.name.removeprefix(prefix)
        if not suffix.isdigit():
            continue
        candidates.append((int(suffix), child))

    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    return [path for _, path in candidates]


def _is_valid_browser_profile_template(directory: FilePath) -> bool:
    return (
        directory.is_dir()
        and (directory / "Default").is_dir()
        and (directory / "Default" / "Preferences").is_file()
        and (directory / "Local State").is_file()
    )


def _clear_directory(directory: FilePath) -> None:
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _copy_browser_profile_template(source_dir: FilePath, destination_dir: FilePath) -> None:
    for source_child in source_dir.iterdir():
        if source_child.name in DEFAULT_PROFILE_COPY_IGNORE:
            continue

        destination_child = destination_dir / source_child.name
        if source_child.is_dir():
            shutil.copytree(source_child, destination_child)
        else:
            shutil.copy2(source_child, destination_child)


def _seed_minimal_empty_browser_profile_directory(profile_dir: FilePath) -> None:
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "Preferences").write_text("{}", encoding="utf-8")
    (profile_dir / "Local State").write_text("{}", encoding="utf-8")


def _seed_empty_browser_profile_directory(profile_dir: FilePath) -> None:
    for template_dir in _default_browser_profile_template_candidates():
        if not _is_valid_browser_profile_template(template_dir):
            continue
        try:
            _copy_browser_profile_template(template_dir, profile_dir)
            LOG.info(
                "Seeded empty browser profile from default profile template",
                template_dir=str(template_dir),
                profile_dir=str(profile_dir),
            )
            return
        except Exception:
            LOG.warning(
                "Failed to seed empty browser profile from default template, falling back",
                template_dir=str(template_dir),
                profile_dir=str(profile_dir),
                exc_info=True,
            )
            _clear_directory(profile_dir)

    _seed_minimal_empty_browser_profile_directory(profile_dir)
    LOG.info("Seeded empty browser profile from minimal profile skeleton", profile_dir=str(profile_dir))


async def _hard_delete_created_profile_after_store_failure(
    *,
    organization_id: str,
    browser_profile_id: str,
) -> bool:
    try:
        await app.DATABASE.browser_sessions.hard_delete_browser_profile(
            browser_profile_id, organization_id=organization_id
        )
    except Exception:
        LOG.exception(
            "Failed to roll back browser profile after storage failure",
            organization_id=organization_id,
            browser_profile_id=browser_profile_id,
        )
        return False
    return True


async def _create_empty_profile(
    *,
    organization_id: str,
    name: str,
    description: str | None,
) -> BrowserProfile:
    # Seed before inserting so local setup failures never reserve a profile name.
    profile_dir = _create_empty_browser_profile_directory()
    try:
        try:
            profile = await app.DATABASE.browser_sessions.create_browser_profile(
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
                directory=profile_dir,
            )
        except Exception:
            rolled_back = await _hard_delete_created_profile_after_store_failure(
                organization_id=organization_id,
                browser_profile_id=profile.browser_profile_id,
            )
            LOG.error(
                "Failed to store empty browser profile artifacts",
                organization_id=organization_id,
                browser_profile_id=profile.browser_profile_id,
                rolled_back=rolled_back,
                exc_info=True,
            )
            raise
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    LOG.info(
        "Created empty browser profile",
        organization_id=organization_id,
        browser_profile_id=profile.browser_profile_id,
    )
    return profile


async def _create_profile_from_session(
    *,
    organization_id: str,
    name: str,
    description: str | None,
    browser_session_id: str,
) -> BrowserProfile:
    browser_session = await app.DATABASE.browser_sessions.get_persistent_browser_session(
        browser_session_id, organization_id
    )
    if browser_session is None:
        LOG.warning(
            "Browser session not found for profile creation",
            organization_id=organization_id,
            browser_session_id=browser_session_id,
        )
        raise BrowserSessionNotFound(browser_session_id)

    # Read the storage id the session actually exported to: reuse writes back to its bp_, but a fallback
    # (saved profile failed to load) exported under the session id, so resolve from the loaded profile.
    loaded_profile_id = browser_session.browser_profile_id if browser_session.browser_profile_loaded else None
    # Non-None only for a pure-reuse session; it drives the terminal "archive unavailable" error below.
    reused_profile_id = loaded_profile_id if not browser_session.generate_browser_profile else None
    source_profile_id = export_profile_storage_id(
        session_id=browser_session_id,
        browser_profile_id=loaded_profile_id,
        generate_browser_profile=browser_session.generate_browser_profile,
    )
    session_dir = await app.STORAGE.retrieve_browser_profile(
        organization_id=organization_id,
        profile_id=source_profile_id,
    )
    if not session_dir:
        # An opted-out session never uploads an archive, so retrying can't help — fail fast with a
        # distinct message the client can tell apart from the transient "upload not finished yet" case.
        if not browser_session.should_export_profile():
            LOG.info(
                "Browser session was not configured to generate a browser profile",
                organization_id=organization_id,
                browser_session_id=browser_session_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This browser session was not configured to generate a browser profile. "
                    "Start a session with generate_browser_profile enabled to capture a profile."
                ),
            )
        if reused_profile_id is not None:
            # A reuse session writes back over its own profile, which already existed before the session, so a
            # missing archive is terminal (a deleted/absent profile) — retrying can't conjure it.
            LOG.warning(
                "Reused browser profile archive not found for profile creation",
                organization_id=organization_id,
                browser_session_id=browser_session_id,
                browser_profile_id=reused_profile_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"This browser session reused profile {reused_profile_id}, whose archive is unavailable. "
                    "Create a profile from that session's source profile instead, or start a session with "
                    "generate_browser_profile enabled to capture a new profile."
                ),
            )
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

    source_browser_type = browser_session.browser_type.value if browser_session.browser_type else None

    try:
        profile = await app.DATABASE.browser_sessions.create_browser_profile(
            organization_id=organization_id,
            name=name,
            description=description,
            source_browser_type=source_browser_type,
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
        rolled_back = await _hard_delete_created_profile_after_store_failure(
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
        )
        LOG.error(
            "Failed to store browser profile artifacts",
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
            rolled_back=rolled_back,
            exc_info=True,
        )
        raise

    # The promote copied the session's own export (profiles/{pbs_session}.zip) into the new bp_, so
    # reap that source now that it's redundant. Keyed on the session id, never a reused bp_ profile.
    # Best-effort: only after a successful promote, and a reap failure must not fail the request.
    try:
        await app.STORAGE.delete_browser_profile(
            organization_id=organization_id,
            profile_id=browser_session_id,
        )
    except Exception:
        LOG.exception(
            "Failed to delete source session profile blob after promote",
            organization_id=organization_id,
            browser_session_id=browser_session_id,
            browser_profile_id=profile.browser_profile_id,
        )

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
    workflow_run = await app.DATABASE.workflow_runs.get_workflow_run(workflow_run_id, organization_id=organization_id)
    if not workflow_run:
        LOG.warning(
            "Workflow run not found for profile creation",
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
        )
        raise WorkflowRunNotFound(workflow_run_id)

    workflow = await app.DATABASE.workflows.get_workflow(
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
    browser_session_storage_key = await app.WORKFLOW_SERVICE.get_workflow_browser_session_storage_key(
        workflow=workflow,
        workflow_run=workflow_run,
    )
    session_dir: str | None = None
    for attempt in range(poll_attempts):
        session_dir = await app.STORAGE.retrieve_browser_session(
            organization_id=organization_id,
            workflow_permanent_id=browser_session_storage_key,
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
        profile = await app.DATABASE.browser_sessions.create_browser_profile(
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
        rolled_back = await _hard_delete_created_profile_after_store_failure(
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
        )
        LOG.error(
            "Failed to store browser profile artifacts",
            organization_id=organization_id,
            browser_profile_id=profile.browser_profile_id,
            workflow_run_id=workflow_run_id,
            rolled_back=rolled_back,
            exc_info=True,
        )
        raise

    return profile
