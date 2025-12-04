from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import structlog
from playwright.async_api import BrowserContext, Page, Playwright

from skyvern.config import settings
from skyvern.constants import BROWSER_CLOSE_TIMEOUT, BROWSER_PAGE_CLOSE_TIMEOUT, NAVIGATION_MAX_RETRY_TIME
from skyvern.exceptions import (
    EmptyBrowserContext,
    FailedToNavigateToUrl,
    FailedToReloadPage,
    FailedToStopLoadingPage,
    MissingBrowserStatePage,
)
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.webeye.browser_artifacts import BrowserArtifacts, VideoArtifact
from skyvern.webeye.browser_factory import BrowserCleanupFunc, BrowserContextFactory
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import CleanupElementTreeFunc, ScrapedPage, ScrapeExcludeFunc
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame

LOG = structlog.get_logger()


class RealBrowserState(BrowserState):
    def __init__(
        self,
        pw: Playwright,
        browser_context: BrowserContext | None = None,
        page: Page | None = None,
        browser_artifacts: BrowserArtifacts = BrowserArtifacts(),
        browser_cleanup: BrowserCleanupFunc = None,
    ):
        self.__page = page
        self.pw = pw
        self.browser_context = browser_context
        self.browser_artifacts = browser_artifacts
        self.browser_cleanup = browser_cleanup

    async def __assert_page(self) -> Page:
        page = await self.get_working_page()
        if page is not None:
            return page
        pages = (self.browser_context.pages or []) if self.browser_context else []
        LOG.error("BrowserState has no page", urls=[p.url for p in pages])
        raise MissingBrowserStatePage()

    async def _close_all_other_pages(self) -> None:
        cur_page = await self.get_working_page()
        if not self.browser_context or not cur_page:
            return
        pages = self.browser_context.pages
        for page in pages:
            if page != cur_page:
                try:
                    async with asyncio.timeout(2):
                        await page.close()
                except asyncio.TimeoutError:
                    LOG.warning("Timeout to close the page. Skip closing the page", url=page.url)
                except Exception:
                    LOG.exception("Error while closing the page", url=page.url)

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
    ) -> None:
        if self.browser_context is None:
            LOG.info("creating browser context")
            (
                browser_context,
                browser_artifacts,
                browser_cleanup,
            ) = await BrowserContextFactory.create_browser_context(
                self.pw,
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
            self.browser_context = browser_context
            self.browser_artifacts = browser_artifacts
            self.browser_cleanup = browser_cleanup
            LOG.info("browser context is created")

        if await self.get_working_page() is None:
            page: Page | None = None
            use_existing_page = False
            if browser_address and len(self.browser_context.pages) > 0:
                pages = await self.list_valid_pages()
                if len(pages) > 0:
                    page = pages[-1]
                    use_existing_page = True
            if page is None:
                page = await self.browser_context.new_page()

            await self.set_working_page(page, 0)
            if not use_existing_page:
                await self._close_all_other_pages()

            if url and page.url.rstrip("/") != url.rstrip("/"):
                await self.navigate_to_url(page=page, url=url)

    async def navigate_to_url(self, page: Page, url: str, retry_times: int = NAVIGATION_MAX_RETRY_TIME) -> None:
        try:
            for retry_time in range(retry_times):
                LOG.info(f"Trying to navigate to {url} and waiting for 1 second.", url=url, retry_time=retry_time)
                try:
                    start_time = time.time()
                    await page.goto(url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
                    end_time = time.time()
                    LOG.info(
                        "Page loading time",
                        loading_time=end_time - start_time,
                        url=url,
                    )
                    # Do we need this?
                    await asyncio.sleep(5)
                    LOG.info(f"Successfully went to {url}", url=url, retry_time=retry_time)
                    return

                except Exception as e:
                    if retry_time >= retry_times - 1:
                        raise FailedToNavigateToUrl(url=url, error_message=str(e))

                    LOG.warning(
                        f"Error while navigating to url: {str(e)}",
                        exc_info=True,
                        url=url,
                        retry_time=retry_time,
                    )
                    # Wait for 1 seconds before retrying
                    await asyncio.sleep(1)

        except Exception as e:
            LOG.exception(
                f"Failed to navigate to {url} after {retry_times} retries: {str(e)}",
                url=url,
            )
            raise e

    async def get_working_page(self) -> Page | None:
        # HACK: currently, assuming the last page is always the working page.
        # Need to refactor this logic when we want to manipulate multi pages together
        # TODO: do not use index of pages, it should be more robust if we want to fully support multi pages manipulation
        if self.__page is None or self.browser_context is None:
            return None

        # pick the last and http/https page as the working page
        pages = await self.list_valid_pages()
        if len(pages) == 0:
            LOG.info("No http, https or blank page found in the browser context, return None")
            return None

        last_page = pages[-1]
        if self.__page == last_page:
            return self.__page
        await self.set_working_page(last_page, len(pages) - 1)
        return last_page

    async def list_valid_pages(self, max_pages: int = settings.BROWSER_MAX_PAGES_NUMBER) -> list[Page]:
        # List all valid pages(blank page, and http/https page) in the browser context, up to max_pages
        # MSEdge CDP bug(?)
        # when using CDP connect to a MSEdge, the download hub will be included in the context.pages
        if self.browser_context is None:
            return []

        pages = [
            http_page
            for http_page in self.browser_context.pages
            if (
                http_page.url == "about:blank"
                or http_page.url == "chrome-error://chromewebdata/"
                or urlparse(http_page.url).scheme in ["http", "https"]
            )
        ]

        if max_pages <= 0 or len(pages) <= max_pages:
            return pages

        reserved_pages = pages[-max_pages:]

        closing_pages = pages[: len(pages) - max_pages]
        LOG.warning(
            "The page number exceeds the limit, closing the oldest pages. It might cause the video missing",
            closing_pages=closing_pages,
        )
        for page in closing_pages:
            try:
                async with asyncio.timeout(BROWSER_PAGE_CLOSE_TIMEOUT):
                    await page.close()
            except Exception:
                LOG.warning("Error while closing the page", exc_info=True)

        return reserved_pages

    async def validate_browser_context(self, page: Page) -> bool:
        # validate the content
        try:
            skyvern_frame = await SkyvernFrame.create_instance(frame=page)
            html = await skyvern_frame.get_content()
        except Exception:
            LOG.error(
                "Error happened while getting the first page content",
                exc_info=True,
            )
            return False

        if "Bad gateway error" in html:
            LOG.warning("Bad gateway error on the page, recreate a new browser context with another proxy node")
            return False

        if "client_connect_forbidden_host" in html:
            LOG.warning(
                "capture the client_connect_forbidden_host error on the page, recreate a new browser context with another proxy node"
            )
            return False

        return True

    async def must_get_working_page(self) -> Page:
        page = await self.get_working_page()
        if page is None:
            raise MissingBrowserStatePage()
        return page

    async def set_working_page(self, page: Page | None, index: int = 0) -> None:
        self.__page = page
        if page is None:
            return
        if len(self.browser_artifacts.video_artifacts) > index:
            if self.browser_artifacts.video_artifacts[index].video_path is None:
                try:
                    async with asyncio.timeout(settings.BROWSER_ACTION_TIMEOUT_MS / 1000):
                        if page.video:
                            self.browser_artifacts.video_artifacts[index].video_path = await page.video.path()
                except asyncio.TimeoutError:
                    LOG.info("Timeout to get the page video, skip the exception")
                except Exception:
                    LOG.exception("Error while getting the page video", exc_info=True)
            return

        target_length = index + 1
        self.browser_artifacts.video_artifacts.extend(
            [VideoArtifact()] * (target_length - len(self.browser_artifacts.video_artifacts))
        )
        try:
            async with asyncio.timeout(settings.BROWSER_ACTION_TIMEOUT_MS / 1000):
                if page.video:
                    self.browser_artifacts.video_artifacts[index].video_path = await page.video.path()
        except asyncio.TimeoutError:
            LOG.info("Timeout to get the page video, skip the exception")
        except Exception:
            LOG.exception("Error while getting the page video", exc_info=True)
        return

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
    ) -> Page:
        page = await self.get_working_page()
        if page is not None:
            return page

        try:
            await self.check_and_fix_state(
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
        except Exception as e:
            error_message = str(e)
            if "net::ERR" not in error_message:
                raise e
            if not await self.close_current_open_page():
                LOG.warning("Failed to close the current open page")
                raise e
            await self.check_and_fix_state(
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
        page = await self.__assert_page()

        if not await self.validate_browser_context(await self.get_working_page()):
            if not await self.close_current_open_page():
                LOG.warning("Failed to close the current open page, going to skip the browser context validation")
                return page
            await self.check_and_fix_state(
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
            page = await self.__assert_page()
        return page

    async def close_current_open_page(self) -> bool:
        try:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                await self._close_all_other_pages()
                if self.browser_context is not None:
                    await self.browser_context.close()
                self.browser_context = None
                await self.set_working_page(None)
                return True
        except Exception:
            LOG.warning("Error while closing the current open page", exc_info=True)
            return False

    async def stop_page_loading(self) -> None:
        page = await self.__assert_page()
        try:
            await SkyvernFrame.evaluate(frame=page, expression="window.stop()")
        except Exception as e:
            LOG.exception(f"Error while stop loading the page: {repr(e)}")
            raise FailedToStopLoadingPage(url=page.url, error_message=repr(e))

    async def new_page(self) -> Page:
        if self.browser_context is None:
            raise EmptyBrowserContext()
        return await self.browser_context.new_page()

    async def reload_page(self) -> None:
        page = await self.__assert_page()

        LOG.info(f"Reload page {page.url} and waiting for 5 seconds")
        try:
            start_time = time.time()
            await page.reload(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
            end_time = time.time()
            LOG.info(
                "Page loading time",
                loading_time=end_time - start_time,
            )
            await asyncio.sleep(5)
        except Exception as e:
            LOG.exception(f"Error while reload url: {repr(e)}")
            raise FailedToReloadPage(url=page.url, error_message=repr(e))

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
    ) -> ScrapedPage:
        return await scraper.scrape_website(
            browser_state=self,
            url=url,
            cleanup_element_tree=cleanup_element_tree,
            num_retry=num_retry,
            max_retries=max_retries,
            scrape_exclude=scrape_exclude,
            take_screenshots=take_screenshots,
            draw_boxes=draw_boxes,
            max_screenshot_number=max_screenshot_number,
            scroll=scroll,
            support_empty_page=support_empty_page,
            wait_seconds=wait_seconds,
        )

    async def close(self, close_browser_on_completion: bool = True) -> None:
        LOG.info("Closing browser state")
        try:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                if self.browser_context and close_browser_on_completion:
                    LOG.info("Closing browser context and its pages")
                    try:
                        await self.browser_context.close()
                    except Exception:
                        LOG.warning("Failed to close browser context", exc_info=True)
                    LOG.info("Main browser context and all its pages are closed")
                    if self.browser_cleanup is not None:
                        try:
                            self.browser_cleanup()
                            LOG.info("Main browser cleanup is executed")
                        except Exception:
                            LOG.warning("Failed to execute browser cleanup", exc_info=True)
        except asyncio.TimeoutError:
            LOG.error("Timeout to close browser context, going to stop playwright directly")

        try:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                if self.pw and close_browser_on_completion:
                    try:
                        LOG.info("Stopping playwright")
                        await self.pw.stop()
                        LOG.info("Playwright is stopped")
                    except Exception:
                        LOG.warning("Failed to stop playwright", exc_info=True)
        except asyncio.TimeoutError:
            LOG.error("Timeout to close playwright, might leave the broswer opening forever")

    async def take_fullpage_screenshot(
        self,
        file_path: str | None = None,
    ) -> bytes:
        page = await self.__assert_page()
        return await SkyvernFrame.take_scrolling_screenshot(
            page=page,
            file_path=file_path,
            mode=ScreenshotMode.LITE,
        )

    async def take_post_action_screenshot(
        self,
        scrolling_number: int,
        file_path: str | None = None,
    ) -> bytes:
        page = await self.__assert_page()
        return await SkyvernFrame.take_scrolling_screenshot(
            page=page,
            file_path=file_path,
            mode=ScreenshotMode.LITE,
            scrolling_number=scrolling_number,
        )
