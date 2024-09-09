from enum import StrEnum
from pathlib import Path

# This is the attribute name used to tag interactable elements
SKYVERN_ID_ATTR: str = "unique_id"
SKYVERN_DIR = Path(__file__).parent
REPO_ROOT_DIR = SKYVERN_DIR.parent

INPUT_TEXT_TIMEOUT = 120000  # 2 minutes
PAGE_CONTENT_TIMEOUT = 300  # 5 mins
BROWSER_CLOSE_TIMEOUT = 180  # 3 minute

# reserved fields for navigation payload
SPECIAL_FIELD_VERIFICATION_CODE = "verification_code"


class ScrapeType(StrEnum):
    NORMAL = "normal"
    STOPLOADING = "stoploading"
    RELOAD = "reload"


SCRAPE_TYPE_ORDER = [ScrapeType.NORMAL, ScrapeType.NORMAL, ScrapeType.RELOAD]
