from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import Field

from skyvern.client.types.browser_profile import BrowserProfile

from ._common import BrowserContext, ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern


def _profile_to_dict(profile: BrowserProfile) -> dict[str, Any]:
    data = {
        "browser_profile_id": profile.browser_profile_id,
        "organization_id": profile.organization_id,
        "name": profile.name,
        "description": profile.description,
        "source_browser_type": profile.source_browser_type,
        "created_at": _serialize_timestamp(profile.created_at),
        "modified_at": _serialize_timestamp(profile.modified_at),
        "deleted_at": _serialize_timestamp(profile.deleted_at),
    }
    return {key: value for key, value in data.items() if value is not None}


def _serialize_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def skyvern_browser_profile_create(
    name: Annotated[str, Field(description="Name for the browser profile.")],
    description: Annotated[str | None, Field(description="Optional profile description.")] = None,
    browser_session_id: Annotated[
        str | None,
        Field(
            description=(
                "Persistent browser session to capture into a profile. The session must have been started with "
                "generate_browser_profile enabled and then closed so its profile archive exists."
            )
        ),
    ] = None,
    workflow_run_id: Annotated[
        str | None,
        Field(description="Workflow run whose persisted session should be captured into a profile."),
    ] = None,
) -> dict[str, Any]:
    """Create a browser profile from a persistent browser session or workflow run."""
    with Timer() as timer:
        try:
            if browser_session_id and workflow_run_id:
                return make_result(
                    "skyvern_browser_profile_create",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        "Provide only one profile source",
                        "Use either browser_session_id or workflow_run_id, not both.",
                    ),
                )

            source_browser_session_id = browser_session_id
            if not source_browser_session_id and not workflow_run_id:
                return make_result(
                    "skyvern_browser_profile_create",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        "No browser profile source provided",
                        "Pass a browser_session_id or workflow_run_id. The source session must have been started "
                        "with generate_browser_profile enabled and already closed (its profile archive is created "
                        "on close).",
                    ),
                )

            profile = await get_skyvern().create_browser_profile(
                name=name,
                description=description,
                browser_session_id=source_browser_session_id,
                workflow_run_id=workflow_run_id,
            )
            timer.mark("sdk")
            browser_context = (
                BrowserContext(mode="cloud_session", session_id=source_browser_session_id)
                if source_browser_session_id
                else None
            )
            return make_result(
                "skyvern_browser_profile_create",
                browser_context=browser_context,
                data=_profile_to_dict(profile),
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_profile_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to create browser profile"),
            )


async def skyvern_browser_profile_list(
    page: Annotated[int | None, Field(description="Page number for paginated profile listing.")] = None,
    page_size: Annotated[int | None, Field(description="Page size for paginated profile listing.")] = None,
    include_deleted: Annotated[bool | None, Field(description="Include soft-deleted browser profiles.")] = None,
    search_key: Annotated[
        str | None,
        Field(description="Case-insensitive substring search across browser profile name and description."),
    ] = None,
) -> dict[str, Any]:
    """List browser profiles for the organization."""
    with Timer() as timer:
        try:
            profiles = await get_skyvern().list_browser_profiles(
                page=page,
                page_size=page_size,
                include_deleted=include_deleted,
                search_key=search_key,
            )
            timer.mark("sdk")
            serialized_profiles = [_profile_to_dict(profile) for profile in profiles]
            return make_result(
                "skyvern_browser_profile_list",
                data={"profiles": serialized_profiles, "count": len(serialized_profiles)},
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_profile_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to list browser profiles"),
            )


async def skyvern_browser_profile_get(
    browser_profile_id: Annotated[str, Field(description="Browser profile ID. Browser profile IDs start with bp_.")],
) -> dict[str, Any]:
    """Get a browser profile by ID."""
    with Timer() as timer:
        try:
            profile = await get_skyvern().get_browser_profile(browser_profile_id)
            timer.mark("sdk")
            return make_result(
                "skyvern_browser_profile_get",
                data=_profile_to_dict(profile),
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_profile_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to get browser profile"),
            )


async def skyvern_browser_profile_update(
    browser_profile_id: Annotated[str, Field(description="Browser profile ID. Browser profile IDs start with bp_.")],
    name: Annotated[str | None, Field(description="New browser profile name.")] = None,
    description: Annotated[str | None, Field(description="New browser profile description.")] = None,
) -> dict[str, Any]:
    """Update a browser profile's name and/or description."""
    with Timer() as timer:
        try:
            if name is None and description is None:
                return make_result(
                    "skyvern_browser_profile_update",
                    ok=False,
                    timing_ms=timer.timing_ms,
                    error=make_error(
                        ErrorCode.INVALID_INPUT,
                        "No browser profile updates provided",
                        "Pass name, description, or both.",
                    ),
                )
            profile = await get_skyvern().update_browser_profile(
                browser_profile_id,
                name=name,
                description=description,
            )
            timer.mark("sdk")
            return make_result(
                "skyvern_browser_profile_update",
                data=_profile_to_dict(profile),
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_profile_update",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to update browser profile"),
            )


async def skyvern_browser_profile_delete(
    browser_profile_id: Annotated[str, Field(description="Browser profile ID. Browser profile IDs start with bp_.")],
) -> dict[str, Any]:
    """Delete a browser profile."""
    with Timer() as timer:
        try:
            await get_skyvern().delete_browser_profile(browser_profile_id)
            timer.mark("sdk")
            return make_result(
                "skyvern_browser_profile_delete",
                data={"browser_profile_id": browser_profile_id, "deleted": True},
                timing_ms=timer.timing_ms,
            )
        except Exception as e:
            return make_result(
                "skyvern_browser_profile_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.SDK_ERROR, str(e), "Failed to delete browser profile"),
            )
