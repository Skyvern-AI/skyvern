from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from skyvern.forge.sdk.experimentation.drive_shared_drive_search import drive_shared_drive_search_enabled
from skyvern.forge.sdk.routes.google_sheets import _mint_access_token
from skyvern.forge.sdk.schemas.google_drive import GoogleDriveFile, ListGoogleDriveFilesResponse
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import google_drive_service, org_auth_service

google_drive_router = APIRouter()


def _http_error(exc: google_drive_service.GoogleDriveAPIError) -> HTTPException:
    if exc.status == 403 and exc.code == "reconnect_required":
        return HTTPException(
            status_code=409,
            detail={"code": "reconnect_required", "missing_scope": True, "message": exc.message},
        )
    if exc.status in (400, 401, 403, 404, 429):
        return HTTPException(status_code=exc.status, detail=exc.message)
    return HTTPException(status_code=502, detail=exc.message)


# exclude_unset keeps incomplete_search out of the body unless the shared-drive search set it.
@google_drive_router.get("/files", response_model_exclude_unset=True)
async def list_files(
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    folders: Annotated[bool, Query(description="List only folders instead of only files")] = False,
    q: Annotated[str | None, Query(description="Optional search query on file name")] = None,
    page_token: Annotated[str | None, Query(description="Drive v3 pagination token")] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ListGoogleDriveFilesResponse:
    access_token = await _mint_access_token(current_org.organization_id, credential_id)
    search_all_drives = await drive_shared_drive_search_enabled(current_org.organization_id)
    try:
        page = await google_drive_service.list_files(
            access_token=access_token,
            folders=folders,
            query=q,
            page_token=page_token,
            page_size=page_size,
            search_all_drives=search_all_drives,
            downloadable_only=not folders,
        )
    except google_drive_service.GoogleDriveAPIError as exc:
        raise _http_error(exc)
    response = ListGoogleDriveFilesResponse(
        files=[GoogleDriveFile.model_validate(item) for item in page.files],
        next_page_token=page.next_page_token,
    )
    if search_all_drives:
        response.incomplete_search = page.incomplete_search
    return response
