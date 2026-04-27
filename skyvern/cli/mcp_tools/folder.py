"""Skyvern MCP folder tools — CRUD for organizing workflows."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from skyvern.client.errors import NotFoundError

from ._common import ErrorCode, Timer, make_error, make_result
from ._session import get_skyvern
from ._validation import validate_folder_id


def _serialize_folder(folder: Any) -> dict[str, Any]:
    return {
        "folder_id": folder.folder_id,
        "organization_id": folder.organization_id,
        "title": folder.title,
        "description": folder.description,
        "workflow_count": folder.workflow_count,
        "created_at": folder.created_at.isoformat() if folder.created_at else None,
        "modified_at": folder.modified_at.isoformat() if folder.modified_at else None,
    }


def _folder_not_found(action: str, folder_id: str, timer: Timer) -> dict[str, Any]:
    return make_result(
        action,
        ok=False,
        timing_ms=timer.timing_ms,
        error=make_error(
            ErrorCode.INVALID_INPUT,
            f"Folder {folder_id!r} not found",
            "Use skyvern_folder_list to find valid folder IDs.",
        ),
    )


async def skyvern_folder_list(
    page: Annotated[int, Field(description="Page number (1-based)", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Results per page", ge=1, le=500)] = 50,
    search: Annotated[str | None, "Search folders by title or description"] = None,
) -> dict[str, Any]:
    """List available folders. Use when you need to discover folder IDs before assigning workflows."""

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            folders = await skyvern.get_folders(page=page, page_size=page_size, search=search)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_folder_list",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection."),
            )

    return make_result(
        "skyvern_folder_list",
        data={
            "folders": [_serialize_folder(folder) for folder in folders],
            "page": page,
            "page_size": page_size,
            "count": len(folders),
            # Heuristic until the API exposes pagination metadata.
            "has_more": len(folders) == page_size,
        },
        timing_ms=timer.timing_ms,
    )


async def skyvern_folder_create(
    title: Annotated[str, Field(description="Folder title", min_length=1, max_length=255)],
    description: Annotated[str | None, Field(description="Optional folder description")] = None,
) -> dict[str, Any]:
    """Create a new folder for organizing workflows."""

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            folder = await skyvern.create_folder(title=title, description=description)
            timer.mark("sdk")
        except Exception as e:
            return make_result(
                "skyvern_folder_create",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the folder title and your permissions."),
            )

    data = _serialize_folder(folder)
    data["sdk_equivalent"] = f"await skyvern.create_folder(title={title!r}, description={description!r})"
    return make_result("skyvern_folder_create", data=data, timing_ms=timer.timing_ms)


async def skyvern_folder_get(
    folder_id: Annotated[str, "Folder ID (starts with fld_)"],
) -> dict[str, Any]:
    """Get details for a specific folder."""

    if err := validate_folder_id(folder_id, "skyvern_folder_get"):
        return err

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            folder = await skyvern.get_folder(folder_id)
            timer.mark("sdk")
        except NotFoundError:
            return _folder_not_found("skyvern_folder_get", folder_id, timer)
        except Exception as e:
            return make_result(
                "skyvern_folder_get",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection."),
            )

    data = _serialize_folder(folder)
    data["sdk_equivalent"] = f"await skyvern.get_folder({folder_id!r})"
    return make_result("skyvern_folder_get", data=data, timing_ms=timer.timing_ms)


async def skyvern_folder_update(
    folder_id: Annotated[str, "Folder ID (starts with fld_)"],
    title: Annotated[str | None, Field(description="New folder title", min_length=1, max_length=255)] = None,
    description: Annotated[str | None, Field(description="New folder description")] = None,
) -> dict[str, Any]:
    """Update a folder's title or description."""

    if err := validate_folder_id(folder_id, "skyvern_folder_update"):
        return err
    if title is None and description is None:
        return make_result(
            "skyvern_folder_update",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "At least one of title or description must be provided",
                "Provide title, description, or both.",
            ),
        )

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            folder = await skyvern.update_folder(folder_id, title=title, description=description)
            timer.mark("sdk")
        except NotFoundError:
            return _folder_not_found("skyvern_folder_update", folder_id, timer)
        except Exception as e:
            return make_result(
                "skyvern_folder_update",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the folder ID and updated values."),
            )

    data = _serialize_folder(folder)
    data["sdk_equivalent"] = f"await skyvern.update_folder({folder_id!r}, title={title!r}, description={description!r})"
    return make_result("skyvern_folder_update", data=data, timing_ms=timer.timing_ms)


async def skyvern_folder_delete(
    folder_id: Annotated[str, "Folder ID (starts with fld_)"],
    delete_workflows: Annotated[
        bool,
        "Also delete workflows in the folder instead of just removing their folder assignment",
    ] = False,
    force: Annotated[bool, "Must be true to confirm deletion — prevents accidental deletes"] = False,
) -> dict[str, Any]:
    """Delete a folder. By default workflows remain and are removed from the folder."""

    if err := validate_folder_id(folder_id, "skyvern_folder_delete"):
        return err
    if not force:
        return make_result(
            "skyvern_folder_delete",
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Deletion of folder {folder_id!r} requires confirmation",
                "Set force=true to confirm deletion.",
            ),
        )

    skyvern = get_skyvern()

    with Timer() as timer:
        try:
            result = await skyvern.delete_folder(folder_id, delete_workflows=delete_workflows)
            timer.mark("sdk")
        except NotFoundError:
            return _folder_not_found("skyvern_folder_delete", folder_id, timer)
        except Exception as e:
            return make_result(
                "skyvern_folder_delete",
                ok=False,
                timing_ms=timer.timing_ms,
                error=make_error(ErrorCode.API_ERROR, str(e), "Check the folder ID and your permissions."),
            )

    data = result if isinstance(result, dict) else {}
    data["sdk_equivalent"] = f"await skyvern.delete_folder({folder_id!r}, delete_workflows={delete_workflows!r})"
    return make_result("skyvern_folder_delete", data=data, timing_ms=timer.timing_ms)
