from collections.abc import Awaitable, Callable
from typing import Annotated, TypeVar

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from skyvern.forge import app
from skyvern.forge.sdk.experimentation.drive_shared_drive_search import drive_shared_drive_search_enabled
from skyvern.forge.sdk.schemas.google_sheets import (
    CreateGoogleSheetTabRequest,
    CreateGoogleSheetTabResponse,
    CreateGoogleSpreadsheetRequest,
    CreateGoogleSpreadsheetResponse,
    GetSheetDimensionsResponse,
    GetSheetHeadersResponse,
    GoogleSheetTab,
    GoogleSpreadsheetSummary,
    ListGoogleSheetTabsResponse,
    ListGoogleSpreadsheetsResponse,
    SheetHeader,
)
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import google_oauth_service, google_sheets_service, org_auth_service
from skyvern.schemas.google_sheets import column_index_to_letter

LOG = structlog.get_logger()

google_sheets_router = APIRouter()

T = TypeVar("T")

# Blocks wait up to 125 s for another pod's mint; a picker call stops after this and mints without caching.
_TOKEN_CACHE_LOCK_WAIT_SECONDS = 10


async def _load_credential_secrets(
    organization_id: str, credential_id: str
) -> google_oauth_service.GoogleCredentialSecrets:
    try:
        return await google_oauth_service.load_credential_secrets(
            organization_id=organization_id,
            credential_id=credential_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SQLAlchemyError:
        # DB connectivity / timeout: the credential may be fine, retry server-side.
        LOG.exception("Database error loading Google credential", credential_id=credential_id)
        raise HTTPException(status_code=503, detail="Database unavailable")
    except Exception as exc:
        # Remaining failures (decrypt errors from key rotation / corrupted ciphertext)
        # surface as reconnect_required so callers route the user back through consent.
        LOG.exception("Failed to load Google credential secrets", credential_id=credential_id)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "reconnect_required",
                "message": f"Could not load stored Google credential: {exc}",
            },
        )


async def _mint_access_token(organization_id: str, credential_id: str, rejected_token: str | None = None) -> str:
    """Reuse the credential's cached access token; on a miss, load secrets and refresh without holding the session."""
    secrets: google_oauth_service.GoogleCredentialSecrets | None = None

    async def mint() -> str:
        nonlocal secrets
        secrets = await _load_credential_secrets(organization_id, credential_id)
        return await google_oauth_service.access_token_from_secrets(secrets, organization_id=organization_id)

    try:
        return await app.AGENT_FUNCTION.get_or_mint_google_access_token(
            organization_id,
            credential_id,
            mint,
            lock_wait_seconds=_TOKEN_CACHE_LOCK_WAIT_SECONDS,
            rejected_token=rejected_token,
        )
    except HTTPException:
        raise
    except google_oauth_service.ExpiredRefreshTokenError as exc:
        # Google rejected the refresh token as revoked/expired; record the needs-reconnect state so
        # the integrations UI reflects it, then route the caller to the reconnect flow.
        await google_oauth_service.mark_credential_expired(
            organization_id,
            credential_id,
            expected_version=secrets.credential_version if secrets is not None else None,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "reconnect_required",
                "message": str(exc),
            },
        )
    except google_oauth_service.MissingAccessTokenError as exc:
        # Google rejected the refresh token (invalid_grant after revoke/expiry);
        # only a fresh consent round can recover, so route to the reconnect flow.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "reconnect_required",
                "message": str(exc),
            },
        )
    except Exception as exc:
        LOG.exception("Failed to refresh Google access token", credential_id=credential_id)
        raise HTTPException(status_code=502, detail=f"Failed to refresh access token: {exc}")


async def _with_access_token(organization_id: str, credential_id: str, call: Callable[[str], Awaitable[T]]) -> T:
    access_token = await _mint_access_token(organization_id, credential_id)
    try:
        return await call(access_token)
    except google_sheets_service.GoogleSheetsAPIError as exc:
        if exc.status != 401:
            raise
    # Google revokes a cached token early when access is removed on its side; mint once more so the caller gets a
    # fresh token or the 409 reconnect, not a 401 the frontend reads as a Skyvern logout. Google acted on nothing
    # it answered 401, so resending a write is safe.
    return await call(await _mint_access_token(organization_id, credential_id, rejected_token=access_token))


def _translate_api_error(exc: google_sheets_service.GoogleSheetsAPIError) -> HTTPException:
    if exc.status == 403 and exc.code == "reconnect_required":
        return HTTPException(
            status_code=409,
            detail={
                "code": "reconnect_required",
                "missing_scope": True,
                "message": exc.message,
            },
        )
    if exc.status == 404:
        return HTTPException(status_code=404, detail=exc.message)
    if exc.status == 429:
        return HTTPException(status_code=429, detail=exc.message)
    if exc.status in (400, 401, 403):
        return HTTPException(status_code=exc.status, detail=exc.message)
    return HTTPException(status_code=502, detail=exc.message)


# exclude_unset keeps incomplete_search out of the body unless the shared-drive search set it.
@google_sheets_router.get("/spreadsheets", response_model_exclude_unset=True)
async def list_spreadsheets(
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    q: Annotated[str | None, Query(description="Optional search query on spreadsheet name")] = None,
    page_token: Annotated[str | None, Query(description="Drive v3 pagination token")] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ListGoogleSpreadsheetsResponse:
    search_all_drives = await drive_shared_drive_search_enabled(current_org.organization_id)
    try:
        paged = await _with_access_token(
            current_org.organization_id,
            credential_id,
            lambda access_token: google_sheets_service.list_spreadsheets(
                access_token=access_token,
                query=q,
                page_token=page_token,
                page_size=page_size,
                search_all_drives=search_all_drives,
            ),
        )
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    response = ListGoogleSpreadsheetsResponse(
        spreadsheets=[
            GoogleSpreadsheetSummary(
                id=s.id,
                name=s.name,
                modified_time=s.modified_time,
                web_view_link=s.web_view_link,
            )
            for s in paged.spreadsheets
        ],
        next_page_token=paged.next_page_token,
    )
    if search_all_drives:
        response.incomplete_search = paged.incomplete_search
    return response


@google_sheets_router.get("/spreadsheets/{spreadsheet_id}")
async def get_spreadsheet(
    spreadsheet_id: str,
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> GoogleSpreadsheetSummary:
    """Look up a spreadsheet's Drive metadata by id so the block picker can show the
    human name after a workflow reload (the stored payload only carries the URL)."""
    try:
        summary = await _with_access_token(
            current_org.organization_id,
            credential_id,
            lambda access_token: google_sheets_service.get_spreadsheet_summary(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return GoogleSpreadsheetSummary(
        id=summary.id,
        name=summary.name,
        modified_time=summary.modified_time,
        web_view_link=summary.web_view_link,
    )


@google_sheets_router.get("/spreadsheets/{spreadsheet_id}/tabs")
async def get_spreadsheet_tabs(
    spreadsheet_id: str,
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> ListGoogleSheetTabsResponse:
    try:
        tabs = await _with_access_token(
            current_org.organization_id,
            credential_id,
            lambda access_token: google_sheets_service.get_spreadsheet_tabs(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return ListGoogleSheetTabsResponse(
        tabs=[GoogleSheetTab(sheet_id=t.sheet_id, title=t.title, index=t.index) for t in tabs]
    )


@google_sheets_router.get("/spreadsheets/{spreadsheet_id}/headers")
async def get_spreadsheet_headers(
    spreadsheet_id: str,
    sheet_title: Annotated[str, Query(description="Tab title; query param so titles containing '/' work")],
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> GetSheetHeadersResponse:
    try:
        rows = await _with_access_token(
            current_org.organization_id,
            credential_id,
            lambda access_token: google_sheets_service.get_sheet_headers(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                sheet_title=sheet_title,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return GetSheetHeadersResponse(headers=[SheetHeader(letter=h.letter, name=h.name) for h in rows])


@google_sheets_router.get("/spreadsheets/{spreadsheet_id}/dimensions")
async def get_spreadsheet_dimensions(
    spreadsheet_id: str,
    sheet_title: Annotated[str, Query(description="Tab title; query param so titles containing '/' work")],
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    include_merges: Annotated[bool, Query(description="Include all merged ranges in the tab")] = False,
) -> GetSheetDimensionsResponse:
    """Return the named tab's grid size + row-1 headers so the editor can preview the destination."""

    async def read_tab(
        access_token: str,
    ) -> tuple[google_sheets_service.SheetGridProperties, list[google_sheets_service.SheetHeader]]:
        grid = await google_sheets_service.get_sheet_grid_properties(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
            include_merges=include_merges,
        )
        if grid is None:
            # Don't echo `sheet_title` back: the dimensions endpoint is org-auth-gated
            # but a future caller could render this as HTML, so keep it safe-by-default.
            raise HTTPException(status_code=404, detail="Sheet tab not found")
        rows = await google_sheets_service.get_sheet_headers(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
        )
        return grid, rows

    try:
        grid, rows = await _with_access_token(current_org.organization_id, credential_id, read_tab)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    last_index = max(grid.column_count - 1, 0)
    return GetSheetDimensionsResponse(
        sheet_id=grid.sheet_id,
        title=grid.title,
        column_count=grid.column_count,
        row_count=grid.row_count,
        last_column_letter=column_index_to_letter(last_index),
        headers=[SheetHeader(letter=h.letter, name=h.name) for h in rows],
        merges=grid.merges,
        merges_complete=grid.merges_complete,
    )


@google_sheets_router.post("/spreadsheets")
async def create_spreadsheet(
    request: CreateGoogleSpreadsheetRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> CreateGoogleSpreadsheetResponse:
    try:
        created = await _with_access_token(
            current_org.organization_id,
            request.credential_id,
            lambda access_token: google_sheets_service.create_spreadsheet(
                access_token=access_token,
                title=request.title,
            ),
        )
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return CreateGoogleSpreadsheetResponse(
        spreadsheet=GoogleSpreadsheetSummary(
            id=created.id,
            name=created.title,
            web_view_link=created.web_view_link,
        ),
        first_sheet_name=created.first_sheet_name,
    )


@google_sheets_router.post("/spreadsheets/{spreadsheet_id}/tabs")
async def create_sheet_tab(
    spreadsheet_id: str,
    request: CreateGoogleSheetTabRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> CreateGoogleSheetTabResponse:
    try:
        tab = await _with_access_token(
            current_org.organization_id,
            request.credential_id,
            lambda access_token: google_sheets_service.create_sheet_tab(
                access_token=access_token,
                spreadsheet_id=spreadsheet_id,
                title=request.title,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return CreateGoogleSheetTabResponse(
        tab=GoogleSheetTab(sheet_id=tab.sheet_id, title=tab.title, index=tab.index),
    )
