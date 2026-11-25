from pydantic import BaseModel


class GoogleDriveFile(BaseModel):
    id: str
    name: str
    modified_time: str | None = None
    web_view_link: str | None = None


class ListGoogleDriveFilesResponse(BaseModel):
    files: list[GoogleDriveFile]
    next_page_token: str | None = None
    incomplete_search: bool = False
