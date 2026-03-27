from enum import StrEnum
from pathlib import Path

# This is the attribute name used to tag interactable elements
SKYVERN_ID_ATTR: str = "unique_id"
SKYVERN_DIR = Path(__file__).parent
REPO_ROOT_DIR = SKYVERN_DIR.parent

INPUT_TEXT_TIMEOUT = 120000  # 2 minutes
PAGE_CONTENT_TIMEOUT = 300  # 5 mins
BROWSER_PAGE_CLOSE_TIMEOUT = 5  # 5 seconds
BROWSER_CLOSE_TIMEOUT = 180  # 3 minute
BROWSER_DOWNLOAD_MAX_WAIT_TIME = 120  # 2 minute
BROWSER_DOWNLOAD_TIMEOUT = 600  # 10 minute
DOWNLOAD_FILE_PREFIX = "downloads"
SAVE_DOWNLOADED_FILES_TIMEOUT = 180
GET_DOWNLOADED_FILES_TIMEOUT = 30
NAVIGATION_MAX_RETRY_TIME = 5
PERMANENT_NAV_ERRORS = ("net::ERR_INVALID_URL",)
PROXY_SENSITIVE_NAV_ERRORS = (
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_NAME_RESOLUTION_FAILED",
    "net::ERR_CERT_",
    "net::ERR_SSL_",
)
# Errors that should not be retried within the same browser context/proxy.
# The outer context-recreation retry in get_or_create_page may still attempt
# recovery for proxy-sensitive errors by picking a different proxy node.
SKIP_INNER_NAV_RETRY_ERRORS = PERMANENT_NAV_ERRORS + PROXY_SENSITIVE_NAV_ERRORS

AUTO_COMPLETION_POTENTIAL_VALUES_COUNT = 3
DROPDOWN_MENU_MAX_DISTANCE = 100
BROWSER_DOWNLOADING_SUFFIX = ".crdownload"
MAX_UPLOAD_FILE_COUNT = 50
AZURE_BLOB_STORAGE_MAX_UPLOAD_FILE_COUNT = 50
DEFAULT_MAX_SCREENSHOT_SCROLLS = 3

# Default complete_criterion for LoginBlocks. Guides the LLM to check for actual
# logged-in indicators rather than relying on page location, which fails on sites
# that redirect to the homepage after successful login.
DEFAULT_LOGIN_COMPLETE_CRITERION = (
    "The login is successful. To verify, look for ANY of these logged-in indicators on the page: "
    "(1) a user name, email, or account name displayed in the header/navigation bar, "
    "(2) a 'Sign out', 'Log out', or 'Logout' button or link, "
    "(3) an account/profile menu or avatar that was not present before login, "
    "(4) a personalized greeting such as 'Welcome [Name]' or 'Hello [Name]', "
    "(5) a 'My Account', 'My Dashboard', or similar authenticated-only link. "
    "IMPORTANT: Being redirected to the homepage does NOT mean login failed — "
    "many websites redirect to the homepage after a successful login. "
    "Check the page elements carefully for the indicators listed above. "
    "Do NOT assume login failed just because you are on the homepage or the same page as before."
)

# reserved fields for navigation payload
SPECIAL_FIELD_VERIFICATION_CODE = "verification_code"


class ScrapeType(StrEnum):
    NORMAL = "normal"
    STOPLOADING = "stoploading"
    RELOAD = "reload"


SCRAPE_TYPE_ORDER = [ScrapeType.NORMAL, ScrapeType.NORMAL, ScrapeType.RELOAD]
DEFAULT_MAX_TOKENS = 100000
MAX_FILE_PARSE_INPUT_TOKENS = 256_000
MAX_IMAGE_MESSAGES = 10
SCROLL_AMOUNT_MULTIPLIER = 100
EXTRACT_ACTION_SCROLL_AMOUNT = 500  # pixels per scroll action from extract-action prompt

# Text input constants
TEXT_INPUT_DELAY = 10  # 10ms between each character input
# Number of trailing characters typed keystroke-by-keystroke (the rest use fill()).
# 10 chars yield 9 inter-key intervals, balancing speed with realistic input cadence.
TEXT_PRESS_MAX_LENGTH = 10

# Script generation constants
DEFAULT_SCRIPT_RUN_ID = "default"

# SkyvernPage constants
SKYVERN_PAGE_MAX_SCRAPING_RETRIES = 2
