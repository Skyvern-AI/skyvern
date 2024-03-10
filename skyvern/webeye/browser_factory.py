from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from typing import Any, Awaitable, Protocol

import structlog
from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright
from pydantic import BaseModel

from skyvern.exceptions import FailedToNavigateToUrl, UnknownBrowserType, UnknownErrorWhileCreatingBrowserContext
from skyvern.forge.sdk.core.skyvern_context import current
from skyvern.forge.sdk.settings_manager import SettingsManager

LOG = structlog.get_logger()


class BrowserContextCreator(Protocol):
    def __call__(
        self, playwright: Playwright, **kwargs: dict[str, Any]
    ) -> Awaitable[tuple[BrowserContext, BrowserArtifacts]]:
        ...


class BrowserContextFactory:
    _creators: dict[str, BrowserContextCreator] = {}

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
            ],
            "ignore_default_args": [
                "--enable-automation",
            ],
            "record_har_path": har_dir,
            "record_video_dir": video_dir,
            "viewport": {"width": 1920, "height": 1080},
        }

    @staticmethod
    def build_browser_artifacts(
        video_path: str | None = None, har_path: str | None = None, video_artifact_id: str | None = None
    ) -> BrowserArtifacts:
        return BrowserArtifacts(video_path=video_path, har_path=har_path, video_artifact_id=video_artifact_id)

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


class BrowserArtifacts(BaseModel):
    video_path: str | None = None
    video_artifact_id: str | None = None
    har_path: str | None = None


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

    async def _close_all_other_pages(self) -> None:
        if not self.browser_context or not self.page:
            return
        pages = self.browser_context.pages
        for page in pages:
            if page != self.page:
                await page.close()

    async def check_and_fix_state(self, url: str | None = None) -> None:
        if self.pw is None:
            LOG.info("Starting playwright")
            self.pw = await async_playwright().start()
            LOG.info("playwright is started")
        if self.browser_context is None:
            LOG.info("creating browser context")
            browser_context, browser_artifacts = await BrowserContextFactory.create_browser_context(self.pw, url=url)
            self.browser_context = browser_context
            self.browser_artifacts = browser_artifacts
            LOG.info("browser context is created")

        assert self.browser_context is not None

        if self.page is None:
            LOG.info("Creating a new page")
            self.page = await self.browser_context.new_page()
            await self._close_all_other_pages()
            LOG.info("A new page is created")
            if url:
                LOG.info(f"Navigating page to {url} and waiting for 5 seconds")
                try:
                    await self.page.goto(url)
                except Error as playright_error:
                    LOG.exception(f"Error while navigating to url: {str(playright_error)}", exc_info=True)
                    raise FailedToNavigateToUrl(url=url, error_message=str(playright_error))
                LOG.info(f"Successfully went to {url}")

        if self.browser_artifacts.video_path is None:
            self.browser_artifacts.video_path = await self.page.video.path()

    async def get_or_create_page(self, url: str | None = None) -> Page:
        await self.check_and_fix_state(url)
        assert self.page is not None
        return self.page

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
