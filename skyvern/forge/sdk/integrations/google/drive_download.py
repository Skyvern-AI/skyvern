"""Download selected Drive items into a workflow's run-scoped download directory."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skyvern.config import settings
from skyvern.exceptions import DownloadFileMaxSizeExceeded
from skyvern.forge import app
from skyvern.forge.sdk.api.files import get_path_for_workflow_download_directory, resolve_run_download_id
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.integrations.connected_accounts import UseHop, resolve_account_token
from skyvern.forge.sdk.services import google_drive_service, google_oauth_service

if TYPE_CHECKING:
    from skyvern.forge.sdk.integrations.action_runners import ActionRunContext

MAX_ITEMS = 1000
MAX_DEPTH = 20
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


async def download_selected_items(
    *,
    organization_id: str,
    connection_id: str,
    references: list[str],
    recursive: bool,
    output_dir: str,
    workflow_run_id: str | None = None,
    workflow_run_block_id: str | None = None,
    hop_token: str | None = None,
    nango_drive: bool = False,
) -> dict[str, Any]:
    access = (
        UseHop()
        if nango_drive
        else await resolve_account_token(
            organization_id,
            connection_id,
            list(google_oauth_service.GOOGLE_DRIVE_STEP_SCOPES),
            api="google",
        )
    )
    if not access:
        raise ValueError("Google Drive account is unavailable or lacks the required permission.")
    if isinstance(access, UseHop) and (not workflow_run_id or not workflow_run_block_id or not hop_token):
        raise ValueError("Google Drive account is unavailable for this run.")

    pending: deque[tuple[str, int, str | None, Mapping[str, Any] | None, str | None]] = deque(
        (reference, 0, None, None, None) for reference in references
    )
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    total_bytes = 0
    can_list_complete_folder: bool | None = None
    while pending:
        reference, depth, selection_grant, listed_item, listed_resource_key = pending.popleft()
        try:
            resource_key = (
                listed_resource_key
                if listed_item is not None
                else google_drive_service.extract_item_reference(reference).resource_key
            )
            item = (
                listed_item
                if listed_item is not None
                else (
                    await app.AGENT_FUNCTION.get_google_drive_item_for_run(
                        organization_id=organization_id,
                        workflow_run_id=workflow_run_id or "",
                        workflow_run_block_id=workflow_run_block_id or "",
                        connection_id=connection_id,
                        hop_token=hop_token,
                        item=reference,
                        selection_grant=selection_grant,
                    )
                    if isinstance(access, UseHop)
                    else await google_drive_service.get_item(access_token=access, item=reference)
                )
            )
            item_id = item["id"]
            if item_id in seen:
                continue
            if len(seen) >= MAX_ITEMS:
                errors.append({"item": item_id, "error": "item_limit_exceeded"})
                break
            seen.add(item_id)
            if item.get("mimeType") == google_drive_service.GOOGLE_FOLDER_MIME_TYPE or item.get("is_folder") is True:
                if depth > 0 and not recursive:
                    continue
                if can_list_complete_folder is None:
                    if isinstance(access, UseHop):
                        can_list_complete_folder = True
                    else:
                        try:
                            credentials = await google_oauth_service.get_visible_credentials_for_org(organization_id)
                        except Exception:  # noqa: BLE001 - a failed scope read must not imply a complete folder.
                            errors.append({"item": item_id, "error": "folder_scope_check_failed"})
                            continue
                        credential = google_oauth_service.resolve_connection_reference(
                            credentials, connection_id
                        ).credential
                        can_list_complete_folder = credential is not None and google_oauth_service.has_required_scopes(
                            credential.scopes_granted, google_oauth_service.GOOGLE_DRIVE_SCOPES
                        )
                if not can_list_complete_folder:
                    errors.append({"item": item_id, "error": "folder_requires_full_drive_scope"})
                    continue
                if depth >= MAX_DEPTH:
                    errors.append({"item": item_id, "error": "folder_depth_limit_exceeded"})
                    continue
                page_token = None
                while True:
                    page = (
                        await app.AGENT_FUNCTION.list_google_drive_folder_for_run(
                            organization_id=organization_id,
                            workflow_run_id=workflow_run_id or "",
                            workflow_run_block_id=workflow_run_block_id or "",
                            connection_id=connection_id,
                            hop_token=hop_token,
                            folder=item_id,
                            max_results=100,
                            page_token=page_token,
                            include_page_token=True,
                            selection_grant=selection_grant,
                            resource_key=resource_key,
                        )
                        if isinstance(access, UseHop)
                        else await google_drive_service.list_folder(
                            access_token=access,
                            folder=item_id,
                            max_results=100,
                            page_token=page_token,
                            include_page_token=True,
                            resource_key=resource_key,
                            include_resource_keys=True,
                        )
                    )
                    for child in page["files"]:
                        child_id = child.get("id")
                        if isinstance(child_id, str) and child_id and (recursive or not child.get("is_folder")):
                            child_grant = child.get("selection_grant")
                            child_key = child.get("resource_key")
                            pending.append(
                                (
                                    child_id,
                                    depth + 1,
                                    child_grant if isinstance(child_grant, str) else None,
                                    child,
                                    child_key
                                    if isinstance(child_key, str) and google_drive_service.is_valid_drive_id(child_key)
                                    else None,
                                )
                            )
                    page_token = page.get("next_page_token")
                    if not page_token:
                        break
                    if len(pending) + len(seen) >= MAX_ITEMS:
                        errors.append({"item": item_id, "error": "item_limit_exceeded"})
                        break
                continue
            path = (
                await app.AGENT_FUNCTION.download_google_drive_file_for_run(
                    organization_id=organization_id,
                    workflow_run_id=workflow_run_id or "",
                    workflow_run_block_id=workflow_run_block_id or "",
                    connection_id=connection_id,
                    hop_token=hop_token,
                    file_id=item_id,
                    resource_key=resource_key,
                    output_dir=output_dir,
                    max_bytes=min(settings.MAX_HTTP_DOWNLOAD_FILE_SIZE, MAX_TOTAL_BYTES - total_bytes),
                    selection_grant=selection_grant,
                )
                if isinstance(access, UseHop)
                else await google_drive_service.download_file(
                    access_token=access,
                    file_id=item_id,
                    resource_key=resource_key,
                    output_dir=output_dir,
                    max_size_mb=settings.MAX_HTTP_DOWNLOAD_FILE_SIZE // (1024 * 1024),
                )
            )
            size = Path(path).stat().st_size
            if total_bytes + size > MAX_TOTAL_BYTES:
                Path(path).unlink(missing_ok=True)
                errors.append({"item": item_id, "error": "total_size_limit_exceeded"})
                break
            total_bytes += size
            files.append({"id": item_id, "path": path})
        except google_drive_service.GoogleDriveAPIError as exc:
            errors.append({"item": reference, "error": exc.code or "drive_error"})
        except (ValueError, OSError, DownloadFileMaxSizeExceeded) as exc:
            errors.append({"item": reference, "error": type(exc).__name__})
    return {"files": files, "errors": errors}


async def run_drive_download(
    organization_id: str,
    connection_id: str,
    action_id: str,
    inputs: Mapping[str, Any],
    context: ActionRunContext,
) -> dict[str, Any]:
    # The action runner imports this module, and the block module imports the integration action block.
    from skyvern.forge.sdk.integrations.action_runners import ActionRunFailed, google_drive_hop_refusal
    from skyvern.forge.sdk.workflow.models.block import name_run_files, register_workflow_downloads

    workflow_run_id = context.workflow_run_id
    workflow_run_block_id = context.workflow_run_block_id
    if refusal := google_drive_hop_refusal(context):
        raise refusal
    run_download_id = resolve_run_download_id(skyvern_context.current(), fallback_run_id=workflow_run_id)
    directory = get_path_for_workflow_download_directory(run_download_id)
    try:
        result = await download_selected_items(
            organization_id=organization_id,
            connection_id=connection_id,
            references=inputs["items"],
            recursive=inputs.get("recursive", False),
            output_dir=str(directory),
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            hop_token=context.hop_token,
            nango_drive=context.broker_connection,
        )
    except ValueError as exc:
        raise ActionRunFailed(str(exc)) from None
    files = result["files"]
    try:
        held = (
            await app.STORAGE.get_current_attempt_downloaded_files(
                organization_id=organization_id, run_id=run_download_id
            )
            if files
            else []
        )
    except Exception as exc:
        raise ActionRunFailed(
            "Could not check existing workflow files before saving downloads.",
            {
                "files": [],
                "downloaded_file_urls": [],
                "errors": [
                    *result["errors"],
                    *({"item": item["id"], "error": "registration_failed"} for item in files),
                ],
            },
        ) from exc
    names, paths = name_run_files([item["path"] for item in files], held)
    registered = (
        await register_workflow_downloads(
            organization_id=organization_id,
            run_download_id=run_download_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            files=paths,
        )
        if paths
        else held
    )
    by_name = {item.filename: item for item in [*held, *registered]}
    output: dict[str, Any] = {"files": [], "errors": result["errors"], "downloaded_file_urls": []}
    for item in files:
        name, checksum = names[item["path"]]
        file = by_name.get(name)
        if file is None or (file.checksum and file.checksum != checksum):
            output["errors"].append({"item": item["id"], "error": "registration_failed"})
            continue
        output["files"].append({"id": item["id"], "filename": file.filename, "url": file.url})
        output["downloaded_file_urls"].append(file.url)
    if output["errors"]:
        raise ActionRunFailed("Some Google Drive items could not be downloaded.", output)
    return output
