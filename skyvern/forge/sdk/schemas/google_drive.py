from enum import StrEnum

from pydantic import BaseModel


class GoogleDriveHopRefusal(StrEnum):
    """Why a run got no capability for a connected Drive account, recorded per block."""

    ember_test_run = "copilot_test_run"
    unsafe_selection_input_source = "unsafe_selection_input_source"


GOOGLE_DRIVE_EMBER_TEST_RUN_REASON = (
    "Copilot test runs can't use a connected Drive account, except the run Test end-to-end starts in the editor "
    "(not workflows it triggers). Otherwise, save the workflow and run it to use this account."
)

GOOGLE_DRIVE_LITERAL_SELECTION_REASON = (
    "Connected Drive reads require saved file and folder selections without templates. "
    "Choose fixed Drive items or folders, then run the workflow again."
)


class GoogleDriveFile(BaseModel):
    id: str
    name: str
    modified_time: str | None = None
    web_view_link: str | None = None


class GoogleDriveItem(BaseModel):
    id: str
    name: str
    is_folder: bool
    web_view_link: str | None = None


class ListGoogleDriveFilesResponse(BaseModel):
    files: list[GoogleDriveFile]
    next_page_token: str | None = None
    incomplete_search: bool = False
