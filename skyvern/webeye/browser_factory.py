from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

import structlog
from playwright._impl._errors import TimeoutError
from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.exceptions import (
    FailedToNavigateToUrl,
    FailedToReloadPage,
    FailedToStopLoadingPage,
    FailedToTakeScreenshot,
    MissingBrowserStatePage,
    UnknownBrowserType,
    UnknownErrorWhileCreatingBrowserContext,
)
from skyvern.forge.sdk.core.skyvern_context import current
from skyvern.forge.sdk.schemas.tasks import ProxyLocation
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class BrowserContextCreator(Protocol):
    def __call__(
        self, playwright: Playwright, **kwargs: dict[str, Any]
    ) -> Awaitable[tuple[BrowserContext, BrowserArtifacts]]: ...


class BrowserContextFactory:
    _creators: dict[str, BrowserContextCreator] = {}
    _validator: Callable[[Page], Awaitable[bool]] | None = None

    @staticmethod
    def get_subdir() -> str:
        curr_context = current()
        if curr_context and curr_context.task_id:
            return curr_context.task_id
        elif curr_context and curr_context.request_id:
            return curr_context.request_id
        return str(uuid.uuid4())

    @staticmethod
    def build_browser_args() -> dict[str, Any]:
        video_dir = f"{SettingsManager.get_settings().VIDEO_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}"
        har_dir = f"{SettingsManager.get_settings().HAR_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}/{BrowserContextFactory.get_subdir()}.har"
        return {
            "user_data_dir": tempfile.mkdtemp(prefix="skyvern_browser_"),
            "locale": SettingsManager.get_settings().BROWSER_LOCALE,
            "timezone_id": SettingsManager.get_settings().BROWSER_TIMEZONE,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disk-cache-size=1",
                "--start-maximized",
            ],
            "ignore_default_args": [
                "--enable-automation",
            ],
            "record_har_path": har_dir,
            "record_video_dir": video_dir,
            "viewport": {
                "width": settings.BROWSER_WIDTH,
                "height": settings.BROWSER_HEIGHT,
            },
        }

    @staticmethod
    def build_browser_artifacts(
        video_path: str | None = None,
        har_path: str | None = None,
        video_artifact_id: str | None = None,
        traces_dir: str | None = None,
    ) -> BrowserArtifacts:
        return BrowserArtifacts(
            video_path=video_path,
            har_path=har_path,
            video_artifact_id=video_artifact_id,
            traces_dir=traces_dir,
        )

    @classmethod
    def register_type(cls, browser_type: str, creator: BrowserContextCreator) -> None:
        cls._creators[browser_type] = creator

    @classmethod
    async def create_browser_context(
        cls, playwright: Playwright, **kwargs: Any
    ) -> tuple[BrowserContext, BrowserArtifacts]:
        browser_type = SettingsManager.get_settings().BROWSER_TYPE
        try:
            creator = cls._creators.get(browser_type)
            if not creator:
                raise UnknownBrowserType(browser_type)
            return await creator(playwright, **kwargs)
        except UnknownBrowserType as e:
            raise e
        except Exception as e:
            raise UnknownErrorWhileCreatingBrowserContext(browser_type, e) from e

    @classmethod
    def set_validate_browser_context(cls, validator: Callable[[Page], Awaitable[bool]]) -> None:
        cls._validator = validator

    @classmethod
    async def validate_browser_context(cls, page: Page) -> bool:
        if cls._validator is None:
            return True
        return await cls._validator(page)


class BrowserArtifacts(BaseModel):
    video_path: str | None = None
    video_artifact_id: str | None = None
    har_path: str | None = None
    traces_dir: str | None = None


async def _create_headless_chromium(playwright: Playwright, **kwargs: dict) -> tuple[BrowserContext, BrowserArtifacts]:
    browser_args = BrowserContextFactory.build_browser_args()
    browser_artifacts = BrowserContextFactory.build_browser_artifacts(har_path=browser_args["record_har_path"])
    browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    return browser_context, browser_artifacts


async def _create_headful_chromium(playwright: Playwright, **kwargs: dict) -> tuple[BrowserContext, BrowserArtifacts]:
    browser_args = BrowserContextFactory.build_browser_args()
    browser_args.update(
        {
            "headless": False,
        }
    )
    browser_artifacts = BrowserContextFactory.build_browser_artifacts(har_path=browser_args["record_har_path"])
    browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    return browser_context, browser_artifacts


BrowserContextFactory.register_type("chromium-headless", _create_headless_chromium)
BrowserContextFactory.register_type("chromium-headful", _create_headful_chromium)


class BrowserState:
    instance = None

    def __init__(
        self,
        pw: Playwright | None = None,
        browser_context: BrowserContext | None = None,
        page: Page | None = None,
        browser_artifacts: BrowserArtifacts = BrowserArtifacts(),
    ):
        self.pw = pw
        self.browser_context = browser_context
        self.page = page
        self.browser_artifacts = browser_artifacts

    def __assert_page(self) -> Page:
        if self.page is not None:
            return self.page
        LOG.error("BrowserState has no page")
        raise MissingBrowserStatePage()

    async def _close_all_other_pages(self) -> None:
        if not self.browser_context or not self.page:
            return
        pages = self.browser_context.pages
        for page in pages:
            if page != self.page:
                await page.close()

    async def check_and_fix_state(
        self,
        url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        task_id: str | None = None,
    ) -> None:
        if self.pw is None:
            LOG.info("Starting playwright")
            self.pw = await async_playwright().start()
            LOG.info("playwright is started")
        if self.browser_context is None:
            LOG.info("creating browser context")
            (
                browser_context,
                browser_artifacts,
            ) = await BrowserContextFactory.create_browser_context(
                self.pw,
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
            )
            self.browser_context = browser_context
            self.browser_artifacts = browser_artifacts
            LOG.info("browser context is created")

        assert self.browser_context is not None

        if self.page is None:
            success = False
            retries = 0

            while not success and retries < 3:
                try:
                    LOG.info("Creating a new page")
                    self.page = await self.browser_context.new_page()
                    await self._close_all_other_pages()
                    LOG.info("A new page is created")
                    if url:
                        LOG.info(f"Navigating page to {url} and waiting for 5 seconds")
                        try:
                            start_time = time.time()
                            await self.page.goto(url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
                            end_time = time.time()
                            LOG.info(
                                "Page loading time",
                                loading_time=end_time - start_time,
                                url=url,
                            )
                            await asyncio.sleep(5)
                        except Error as playright_error:
                            LOG.exception(f"Error while navigating to url: {str(playright_error)}")
                            raise FailedToNavigateToUrl(url=url, error_message=str(playright_error))
                        success = True
                        LOG.info(f"Successfully went to {url}")
                    else:
                        success = True
                except Exception as e:
                    LOG.exception(
                        f"Error while creating or navigating to a new page. Waiting for 5 seconds. Error: {str(e)}",
                    )
                    retries += 1
                    # Wait for 5 seconds before retrying
                    await asyncio.sleep(5)
                    if retries >= 3:
                        LOG.exception(f"Failed to create a new page after 3 retries: {str(e)}")
                        raise e
                    LOG.info(f"Retrying to create a new page. Retry count: {retries}")

        if self.browser_artifacts.video_path is None:
            self.browser_artifacts.video_path = await self.page.video.path() if self.page and self.page.video else None

    async def get_or_create_page(
        self,
        url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        task_id: str | None = None,
    ) -> Page:
        if self.page is not None:
            return self.page

        await self.check_and_fix_state(url=url, proxy_location=proxy_location, task_id=task_id)
        assert self.page is not None

        if not await BrowserContextFactory.validate_browser_context(self.page):
            await self._close_all_other_pages()
            if self.browser_context is not None:
                await self.browser_context.close()
            self.browser_context = None
            self.page = None
            await self.check_and_fix_state(url=url, proxy_location=proxy_location, task_id=task_id)
            assert self.page is not None

        return self.page

    async def stop_page_loading(self) -> None:
        page = self.__assert_page()
        try:
            await page.evaluate("window.stop()")
        except Exception as e:
            LOG.exception(f"Error while stop loading the page: {repr(e)}")
            raise FailedToStopLoadingPage(url=page.url, error_message=repr(e))

    async def reload_page(self) -> None:
        page = self.__assert_page()

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

    async def close(self, close_browser_on_completion: bool = True) -> None:
        LOG.info("Closing browser state")
        if self.browser_context and close_browser_on_completion:
            LOG.info("Closing browser context and its pages")
            await self.browser_context.close()
            LOG.info("Main browser context and all its pages are closed")
        if self.pw and close_browser_on_completion:
            LOG.info("Stopping playwright")
            await self.pw.stop()
            LOG.info("Playwright is stopped")

    @staticmethod
    async def take_screenshot_from_page(page: Page, full_page: bool = False, file_path: str | None = None) -> bytes:
        try:
            await page.wait_for_load_state(timeout=SettingsManager.get_settings().BROWSER_LOADING_TIMEOUT_MS)
            LOG.info("Page is fully loaded, agent is about to take screenshots")
            start_time = time.time()
            screenshot: bytes = bytes()
            if file_path:
                screenshot = await page.screenshot(
                    path=file_path,
                    full_page=full_page,
                    timeout=SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
                )
            else:
                screenshot = await page.screenshot(
                    full_page=full_page,
                    timeout=SettingsManager.get_settings().BROWSER_SCREENSHOT_TIMEOUT_MS,
                    animations="disabled",
                )
            end_time = time.time()
            LOG.info(
                "Screenshot taking time",
                screenshot_time=end_time - start_time,
                full_page=full_page,
                file_path=file_path,
            )
            return screenshot
        except TimeoutError as e:
            LOG.exception(f"Timeout error while taking screenshot: {str(e)}")
            raise FailedToTakeScreenshot(error_message=str(e)) from e
        except Exception as e:
            LOG.exception(f"Unknown error while taking screenshot: {str(e)}")
            raise FailedToTakeScreenshot(error_message=str(e)) from e

    async def take_screenshot(self, full_page: bool = False, file_path: str | None = None) -> bytes:
        page = self.__assert_page()
        return await self.take_screenshot_from_page(page, full_page, file_path)
