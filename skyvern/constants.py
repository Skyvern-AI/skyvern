from enum import StrEnum
from pathlib import Path

# This is the attribute name used to tag interactable elements
SKYVERN_ID_ATTR: str = "unique_id"
SKYVERN_DIR = Path(__file__).parent
REPO_ROOT_DIR = SKYVERN_DIR.parent

INPUT_TEXT_TIMEOUT = 120000  # 2 minutes
PAGE_CONTENT_TIMEOUT = 300  # 5 mins
BUILDING_ELEMENT_TREE_TIMEOUT_MS = 60 * 1000  # 1 minute
BROWSER_CLOSE_TIMEOUT = 180  # 3 minute
BROWSER_DOWNLOAD_MAX_WAIT_TIME = 120  # 2 minute
BROWSER_DOWNLOAD_TIMEOUT = 600  # 10 minute
DOWNLOAD_FILE_PREFIX = "downloads"
SAVE_DOWNLOADED_FILES_TIMEOUT = 180
GET_DOWNLOADED_FILES_TIMEOUT = 30
NAVIGATION_MAX_RETRY_TIME = 5
AUTO_COMPLETION_POTENTIAL_VALUES_COUNT = 5
DROPDOWN_MENU_MAX_DISTANCE = 100
BROWSER_DOWNLOADING_SUFFIX = ".crdownload"
MAX_UPLOAD_FILE_COUNT = 50

# reserved fields for navigation payload
SPECIAL_FIELD_VERIFICATION_CODE = "verification_code"


class ScrapeType(StrEnum):
    NORMAL = "normal"
    STOPLOADING = "stoploading"
    RELOAD = "reload"


SCRAPE_TYPE_ORDER = [ScrapeType.NORMAL, ScrapeType.NORMAL, ScrapeType.RELOAD]
