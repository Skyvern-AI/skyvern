from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from skyvern.forge.agent_functions import record_request_audit_event_strict
from skyvern.forge.sdk.experimentation.drive_shared_drive_search import drive_shared_drive_search_enabled
from skyvern.forge.sdk.integrations.google import tokens as google_tokens
from skyvern.forge.sdk.routes.google_sheets import _mint_access_token
from skyvern.forge.sdk.schemas.google_drive import GoogleDriveFile, ListGoogleDriveFilesResponse
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import google_drive_service, org_auth_service
from skyvern.forge.sdk.services.request_principal import (
    INTERACTIVE_HUMAN_AUTH_KINDS,
    get_request_principal,
    human_user_id,
)

google_drive_router = APIRouter()

_OWNER_ONLY_CODE = "picker_owner_only"
_OWNER_ONLY_MESSAGE = (
    "Only the person who connected this Google account can open the Drive picker. "
    "Ask them to choose the files, or paste a Drive link instead."
)
_RECONNECT_REQUIRED_CODE = "picker_reconnect_required"
_RECONNECT_REQUIRED_MESSAGE = (
    "Reconnect this Google account and leave the Drive files permission ticked on Google's consent screen "
    "to use the Drive picker."
)
_TOKEN_TOO_BROAD_CODE = "picker_token_too_broad"
_TOKEN_TOO_BROAD_MESSAGE = (
    "Google did not return a chosen-files-only token. Reconnect this Google account and try again."
)
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class DrivePickerTokenRequest(BaseModel):
    credential_id: str


class DrivePickerTokenResponse(BaseModel):
    access_token: str
    expires_at: str | None


def _picker_error(status_code: int, detail: object) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers=_NO_STORE_HEADERS)


def _picker_refusal(code: str, message: str) -> HTTPException:
    return _picker_error(403, {"code": code, "message": message})


def _current_picker_user_id(
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
) -> str | None:
    principal = get_request_principal()
    if (
        principal is None
        or principal.impersonator_user_id is not None
        or principal.auth_kind not in INTERACTIVE_HUMAN_AUTH_KINDS
    ):
        return None
    return human_user_id(principal, current_org.organization_id)


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


@google_drive_router.post(
    "/picker-token",
    response_model=DrivePickerTokenResponse,
    include_in_schema=False,
)
async def mint_drive_picker_token(
    request: DrivePickerTokenRequest,
    response: Response,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org_for_credential_routes)],
    current_user_id: Annotated[str | None, Depends(_current_picker_user_id)],
) -> DrivePickerTokenResponse:
    response.headers.update(_NO_STORE_HEADERS)
    try:
        result = await google_tokens.resolve_drive_picker_token(
            current_org.organization_id,
            request.credential_id,
            current_user_id,
        )
    except Exception as exc:
        raise _picker_error(502, "Could not refresh the Google access token") from exc

    if result.status == google_tokens.DrivePickerTokenStatus.OWNER_ONLY:
        raise _picker_refusal(_OWNER_ONLY_CODE, _OWNER_ONLY_MESSAGE)
    if result.status == google_tokens.DrivePickerTokenStatus.PICKER_RECONNECT_REQUIRED:
        raise _picker_refusal(_RECONNECT_REQUIRED_CODE, _RECONNECT_REQUIRED_MESSAGE)
    if result.status == google_tokens.DrivePickerTokenStatus.TOKEN_TOO_BROAD:
        raise _picker_refusal(_TOKEN_TOO_BROAD_CODE, _TOKEN_TOO_BROAD_MESSAGE)
    if result.status == google_tokens.DrivePickerTokenStatus.RECONNECT_REQUIRED:
        raise _picker_error(
            409,
            {"code": "reconnect_required", "message": "Reconnect this Google account and try again."},
        )
    if result.status == google_tokens.DrivePickerTokenStatus.RETRY_REQUIRED:
        raise _picker_error(
            409,
            {
                "code": "picker_retry_required",
                "message": "This Google account changed while creating the picker token. Try again.",
            },
        )
    if result.status == google_tokens.DrivePickerTokenStatus.DATABASE_UNAVAILABLE:
        raise _picker_error(503, "Database unavailable")
    if result.status == google_tokens.DrivePickerTokenStatus.CONFIGURATION_UNAVAILABLE:
        raise _picker_error(503, "Google OAuth configuration is unavailable")
    if result.status != google_tokens.DrivePickerTokenStatus.READY or result.access_token is None:
        raise _picker_error(502, "Could not refresh the Google access token")

    try:
        await record_request_audit_event_strict(
            current_org.organization_id,
            "google_oauth.picker_token",
            "google_oauth_credential",
            request.credential_id,
        )
    except Exception as exc:
        raise _picker_error(503, "Could not record the Google access token audit event") from exc
    return DrivePickerTokenResponse(
        access_token=result.access_token,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
    )
