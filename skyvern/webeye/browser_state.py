from __future__ import annotations

from typing import Protocol

from playwright.async_api import BrowserContext, Page, Playwright

from skyvern.config import settings
from skyvern.constants import NAVIGATION_MAX_RETRY_TIME
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_factory import BrowserCleanupFunc
from skyvern.webeye.scraper.scraped_page import CleanupElementTreeFunc, ScrapedPage, ScrapeExcludeFunc


class BrowserState(Protocol):
    browser_context: BrowserContext | None
    browser_artifacts: BrowserArtifacts
    browser_cleanup: BrowserCleanupFunc
    pw: Playwright

    async def check_and_fix_state(
        self,
        url: str | None = None,
        proxy_location: ProxyLocationInput = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        script_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
    ) -> None: ...

    async def get_working_page(self) -> Page | None: ...

    async def must_get_working_page(self) -> Page: ...

    async def set_working_page(self, page: Page | None, index: int = 0) -> None: ...

    async def navigate_to_url(self, page: Page, url: str, retry_times: int = NAVIGATION_MAX_RETRY_TIME) -> None: ...

    async def get_or_create_page(
        self,
        url: str | None = None,
        proxy_location: ProxyLocationInput = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        script_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
    ) -> Page: ...

    async def list_valid_pages(self, max_pages: int = settings.BROWSER_MAX_PAGES_NUMBER) -> list[Page]: ...

    async def validate_browser_context(self, page: Page) -> bool: ...

    async def close_current_open_page(self) -> bool: ...

    async def stop_page_loading(self) -> None: ...

    async def new_page(self) -> Page: ...

    async def reload_page(self) -> None: ...

    async def close(self, close_browser_on_completion: bool = True) -> None: ...

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
        draw_boxes: bool = True,
        max_screenshot_number: int = settings.MAX_NUM_SCREENSHOTS,
        scroll: bool = True,
        support_empty_page: bool = False,
        wait_seconds: float = 0,
    ) -> ScrapedPage: ...
