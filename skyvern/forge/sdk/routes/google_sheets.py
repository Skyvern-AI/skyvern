from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

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


async def _mint_access_token(organization_id: str, credential_id: str) -> str:
    """Load credential secrets, then refresh without holding the session."""
    try:
        secrets = await google_oauth_service.load_credential_secrets(
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
    try:
        return await google_oauth_service.access_token_from_secrets(secrets, organization_id=organization_id)
    except google_oauth_service.ExpiredRefreshTokenError as exc:
        # Google rejected the refresh token as revoked/expired; record the needs-reconnect state so
        # the integrations UI reflects it, then route the caller to the reconnect flow.
        await google_oauth_service.mark_credential_expired(
            organization_id,
            credential_id,
            expected_version=secrets.credential_version,
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


@google_sheets_router.get("/spreadsheets")
async def list_spreadsheets(
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
    q: Annotated[str | None, Query(description="Optional search query on spreadsheet name")] = None,
    page_token: Annotated[str | None, Query(description="Drive v3 pagination token")] = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ListGoogleSpreadsheetsResponse:
    access_token = await _mint_access_token(current_org.organization_id, credential_id)
    try:
        paged = await google_sheets_service.list_spreadsheets(
            access_token=access_token,
            query=q,
            page_token=page_token,
            page_size=page_size,
        )
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return ListGoogleSpreadsheetsResponse(
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


@google_sheets_router.get("/spreadsheets/{spreadsheet_id}")
async def get_spreadsheet(
    spreadsheet_id: str,
    credential_id: Annotated[str, Query(description="Stored Google OAuth credential id")],
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> GoogleSpreadsheetSummary:
    """Look up a spreadsheet's Drive metadata by id so the block picker can show the
    human name after a workflow reload (the stored payload only carries the URL)."""
    access_token = await _mint_access_token(current_org.organization_id, credential_id)
    try:
        summary = await google_sheets_service.get_spreadsheet_summary(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
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
    access_token = await _mint_access_token(current_org.organization_id, credential_id)
    try:
        tabs = await google_sheets_service.get_spreadsheet_tabs(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
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
    access_token = await _mint_access_token(current_org.organization_id, credential_id)
    try:
        rows = await google_sheets_service.get_sheet_headers(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
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
) -> GetSheetDimensionsResponse:
    """Return the named tab's grid size + row-1 headers so the editor can preview the destination."""
    access_token = await _mint_access_token(current_org.organization_id, credential_id)
    try:
        grid = await google_sheets_service.get_sheet_grid_properties(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            sheet_title=sheet_title,
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
    )


@google_sheets_router.post("/spreadsheets")
async def create_spreadsheet(
    request: CreateGoogleSpreadsheetRequest,
    current_org: Annotated[Organization, Depends(org_auth_service.get_current_org)],
) -> CreateGoogleSpreadsheetResponse:
    access_token = await _mint_access_token(current_org.organization_id, request.credential_id)
    try:
        created = await google_sheets_service.create_spreadsheet(
            access_token=access_token,
            title=request.title,
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
    access_token = await _mint_access_token(current_org.organization_id, request.credential_id)
    try:
        tab = await google_sheets_service.create_sheet_tab(
            access_token=access_token,
            spreadsheet_id=spreadsheet_id,
            title=request.title,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except google_sheets_service.GoogleSheetsAPIError as exc:
        raise _translate_api_error(exc)
    return CreateGoogleSheetTabResponse(
        tab=GoogleSheetTab(sheet_id=tab.sheet_id, title=tab.title, index=tab.index),
    )
