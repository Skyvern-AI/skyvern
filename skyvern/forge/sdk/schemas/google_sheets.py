from pydantic import BaseModel, Field


class GoogleSpreadsheetSummary(BaseModel):
    id: str
    name: str
    modified_time: str | None = None
    web_view_link: str | None = None


class ListGoogleSpreadsheetsResponse(BaseModel):
    spreadsheets: list[GoogleSpreadsheetSummary]
    next_page_token: str | None = None


class GoogleSheetTab(BaseModel):
    sheet_id: int
    title: str
    index: int | None = None


class ListGoogleSheetTabsResponse(BaseModel):
    tabs: list[GoogleSheetTab]


class SheetHeader(BaseModel):
    letter: str
    name: str


class GetSheetHeadersResponse(BaseModel):
    headers: list[SheetHeader]


class GetSheetDimensionsResponse(BaseModel):
    sheet_id: int
    title: str
    column_count: int
    row_count: int
    last_column_letter: str
    headers: list[SheetHeader]


class CreateGoogleSpreadsheetRequest(BaseModel):
    credential_id: str = Field(..., description="Stored Google OAuth credential id")
    title: str = Field(..., min_length=1, max_length=255, description="Spreadsheet title")


class CreateGoogleSpreadsheetResponse(BaseModel):
    spreadsheet: GoogleSpreadsheetSummary
    first_sheet_name: str | None = None


class CreateGoogleSheetTabRequest(BaseModel):
    credential_id: str = Field(..., description="Stored Google OAuth credential id")
    title: str = Field(..., min_length=1, max_length=255, description="Tab title")


class CreateGoogleSheetTabResponse(BaseModel):
    tab: GoogleSheetTab
