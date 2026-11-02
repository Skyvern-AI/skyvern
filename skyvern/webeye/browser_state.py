from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, Playwright

from skyvern.config import settings
from skyvern.constants import NAVIGATION_MAX_RETRY_TIME
from skyvern.exceptions import BROWSER_CONNECTION_LOST_ERROR_CODE, BrowserStateDiagnostic, MissingBrowserState
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_factory import BrowserCleanupFunc
from skyvern.webeye.browser_runtime_events import AcquireMode, BrowserRuntimeLogContext
from skyvern.webeye.scraper.scraped_page import CleanupElementTreeFunc, ScrapedPage, ScrapeExcludeFunc

if TYPE_CHECKING:
    from skyvern.webeye.browser_engine import BrowserEngineSelection

# URLs a tab reports when it holds no document — including ":" , which Chrome reports for a tab
# whose only navigation turned into a download.
BLANK_PAGE_URLS = {"about:blank", ":"}


# Compared and hashed by identity, so a reader can remember a record it has already reported.
@dataclass(eq=False)
class PageCrashRecord:
    crashed_at: datetime
    crashed_url: str
    cause: Literal["page_crash", "page_lost"]
    # True once a replacement tab opened and the crashed page's non-blank URL was restored in it.
    reopened: bool = False


def is_selectable_page_url(url: str) -> bool:
    """Whether the working-page lookup can pick a tab at `url`; others (an Edge download hub) are skipped."""
    return url in BLANK_PAGE_URLS or url == "chrome-error://chromewebdata/" or urlparse(url).scheme in ("http", "https")


class BrowserState(Protocol):
    browser_context: BrowserContext | None
    browser_context_route_policy_url: str | None
    browser_artifacts: BrowserArtifacts
    browser_cleanup: BrowserCleanupFunc
    pw: Playwright
    # The per-run pinned engine (or None for states built outside the per-run engine seam), so
    # recovery code can classify driver-native errors against THIS run's selected engine.
    engine_selection: BrowserEngineSelection | None
    # HTTP status of the most recent navigate_to_url (None until one runs, or when it produced no
    # response). The Task V3 loop reads it to classify a dead/removed starting URL.
    last_navigation_status: int | None
    # The URL that status came back on, recorded from the same response: the verdict naming a dead page
    # must name the page the status belongs to, not wherever the page went afterwards.
    last_navigation_url: str | None
    # The proxy this browser was actually built with. A consumer reading a flattened failure
    # sentence cannot tell which hop it went through; this carries that fact from where it is known.
    built_with_proxy_location: ProxyLocationInput

    def bind_runtime_event_context(self, context: BrowserRuntimeLogContext) -> None: ...

    @property
    def runtime_event_context(self) -> BrowserRuntimeLogContext: ...

    def record_browser_acquisition(
        self, acquire_mode: AcquireMode, requested_at_monotonic: float | None = None
    ) -> None: ...

    def publish_runtime_events(self) -> None: ...

    def mark_run_released(self) -> None: ...

    def add_on_close(self, callback: Callable[[], Awaitable[None]]) -> None: ...

    async def check_and_fix_state(
        self,
        url: str | None = None,
        browser_context_route_policy_url: str | None = None,
        proxy_location: ProxyLocationInput = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_permanent_id: str | None = None,
        script_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
        browser_session_id: str | None = None,
        *,
        before_navigation: Callable[[BrowserState], None] | None = None,
    ) -> None: ...

    def is_connected(self) -> bool: ...

    def record_connection_probe_failure(
        self, context: BrowserContext | None, driver: Playwright, *, timed_out: bool = False
    ) -> None: ...

    def get_browser_state_diagnostic(self) -> BrowserStateDiagnostic | None: ...

    def get_browser_connection_loss(self) -> BrowserStateDiagnostic | None: ...

    @property
    def page_crash_records(self) -> Sequence[PageCrashRecord]: ...

    @property
    def active_run_owner(self) -> str | None: ...

    async def reconnect(
        self,
        proxy_location: ProxyLocationInput = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_permanent_id: str | None = None,
        browser_context_route_policy_url: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
        browser_session_id: str | None = None,
        stale_context_is_unusable: bool = False,
    ) -> None: ...

    async def get_working_page(self, *, prune_excess_pages: bool = True) -> Page | None: ...

    async def must_get_working_page(self) -> Page: ...

    async def set_working_page(self, page: Page | None, index: int = 0) -> None: ...

    async def set_active_page(self, page: Page, *, prune_excess_pages: bool = True) -> None: ...

    async def navigate_to_url(
        self,
        page: Page,
        url: str,
        retry_times: int = NAVIGATION_MAX_RETRY_TIME,
        wait_until: Literal["load", "domcontentloaded", "commit"] = "load",
    ) -> None: ...

    def begin_navigation(self) -> None: ...

    async def record_navigation_result(self, error: BaseException | None, url: str | None = None) -> None: ...

    async def get_or_create_page(
        self,
        url: str | None = None,
        proxy_location: ProxyLocationInput = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_permanent_id: str | None = None,
        script_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
        browser_session_id: str | None = None,
        navigation_run_id: str | None = None,
        before_navigation: Callable[[BrowserState], None] | None = None,
    ) -> Page: ...

    async def list_valid_pages(self, max_pages: int = settings.BROWSER_MAX_PAGES_NUMBER) -> list[Page]: ...

    async def validate_browser_context(self, page: Page) -> bool: ...

    async def close_current_open_page(self) -> bool: ...

    async def stop_page_loading(self) -> None: ...

    async def new_page(self) -> Page: ...

    async def reload_page(self, degradation: bool = False, page: Page | None = None) -> None: ...

    async def wait_for_main_frame_navigation(self, page: Page, deadline: float) -> None:
        """Wait, until at most the monotonic ``deadline``, for ``page``'s in-flight main-frame navigation to settle."""
        ...

    async def close(
        self,
        close_browser_on_completion: bool = True,
        release_driver: bool | None = None,
        *,
        persist_session_state: bool = True,
    ) -> bool: ...

    async def detach_remote_driver(self) -> None:
        """Disable local CDP interception and stop only this process's Playwright driver."""
        ...

    async def take_fullpage_screenshot(self, file_path: str | None = None) -> bytes: ...

    async def take_post_action_screenshot(self, scrolling_number: int, file_path: str | None = None) -> bytes: ...

    async def scrape_website(
        self,
        url: str,
        cleanup_element_tree: CleanupElementTreeFunc,
        num_retry: int = 0,
        max_retries: int = settings.MAX_SCRAPING_RETRIES,
        scrape_exclude: ScrapeExcludeFunc | None = None,
        take_screenshots: bool = True,
        # DEPRECATED: visual bounding box overlays are no longer rendered during scraping.
        # The parameter is retained for backwards compatibility and is scheduled for removal.
        # New call sites must not pass ``draw_boxes=True``.
        draw_boxes: bool = False,
        max_screenshot_number: int = settings.MAX_NUM_SCREENSHOTS,
        scroll: bool = True,
        support_empty_page: bool = False,
        wait_seconds: float = 0,
        must_included_tags: list[str] | None = None,
        allow_transient_ui_suppression: bool = False,
        navigation_deadline: float | None = None,
    ) -> ScrapedPage: ...


def get_browser_state_diagnostic(browser_state: BrowserState | None) -> BrowserStateDiagnostic | None:
    """Read the latched disconnect snapshot without requiring every test double to implement it."""
    if browser_state is None:
        return None
    getter = getattr(browser_state, "get_browser_state_diagnostic", None)
    diagnostic = getter() if callable(getter) else None
    return diagnostic if isinstance(diagnostic, BrowserStateDiagnostic) else None


def get_browser_connection_loss(browser_state: BrowserState | None) -> BrowserStateDiagnostic | None:
    """Read the terminal loss fact without requiring every test double to implement it."""
    if browser_state is None:
        return None
    getter = getattr(browser_state, "get_browser_connection_loss", None)
    loss = getter() if callable(getter) else None
    return loss if isinstance(loss, BrowserStateDiagnostic) else None


BROWSER_CONNECTION_LOST_USER_MESSAGE = (
    "Skyvern lost its connection to the browser partway through this run, so the run stopped where it was. "
    "Anything the run had already submitted in that browser may or may not have gone through. Check the site "
    "before running this again, because a new run repeats the steps from the beginning."
)


def browser_connection_lost_reason(browser_state: BrowserState | None) -> str | None:
    """The failure reason a run records once it has lost its own browser; None while it still holds one."""
    if get_browser_connection_loss(browser_state) is None:
        return None
    return BROWSER_CONNECTION_LOST_USER_MESSAGE


def raise_if_browser_connection_lost(
    browser_state: BrowserState | None, *, task_id: str | None = None, workflow_run_id: str | None = None
) -> None:
    connection_loss = get_browser_connection_loss(browser_state)
    if connection_loss is not None:
        raise MissingBrowserState(
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            diagnostic=connection_loss,
            detected_at=datetime.now(UTC),
            failure_reason=BROWSER_CONNECTION_LOST_ERROR_CODE,
        )
