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

# Default navigation_goal for LoginBlocks. Instructs the LLM how to find the login
# page, fill credentials, and handle multi-step flows / 2FA.
DEFAULT_LOGIN_PROMPT = """\
If you're not on the login page, navigate to the login page first.
First, dismiss any promotional popups or cookie prompts that could block interaction with the page.

Log in using the credentials provided in the user details:
1. Find the username/email input field and enter the username or email from the provided credentials.
2. Find the password input field and enter the password from the provided credentials. \
Some websites use a multi-step login flow where you enter the email first, click a "Continue" or "Next" button, \
and then the password field appears on the next step. Handle this by entering the email, clicking continue, \
then entering the password once the field is revealed.
3. Click the login/sign-in button to submit the credentials.
4. If a 2-factor authentication step appears, enter the authentication code.

Make sure you enter the username and password separately — do not paste both into the same field.
Use your action history to determine if you already attempted to log in. \
If you have not clicked the login button since filling in the credentials, try submitting before assuming failure.

If you fail to log in or can't find the login page after several trials, terminate.
If the credentials are invalid, expired, or explicitly rejected by the website (e.g., "Invalid credentials", \
"Wrong password"), terminate immediately and take no further actions.
If login is completed, you're successful."""

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

# Template for wrapping a block-level mini-goal with the user's original prompt as context.
# Used by both TaskV2 planning and the workflow-copilot-v2 tool handler so that every block's
# navigation_goal carries the user's overarching intent — the verifier (complete_verify) can
# then reason about completion against the user's goal rather than the block's narrow action
# decomposition.
MINI_GOAL_TEMPLATE = """Achieve the following mini goal and once it's achieved, complete:
```{mini_goal}```

This mini goal is part of the big goal the user wants to achieve and use the big goal as context to achieve the mini goal:
```{main_goal}```"""

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
