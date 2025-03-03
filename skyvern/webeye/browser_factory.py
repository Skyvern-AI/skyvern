from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import aiofiles
import structlog
from playwright.async_api import BrowserContext, ConsoleMessage, Download, Error, Page, Playwright
from pydantic import BaseModel, PrivateAttr

from skyvern.config import settings
from skyvern.constants import BROWSER_CLOSE_TIMEOUT, BROWSER_DOWNLOAD_TIMEOUT, NAVIGATION_MAX_RETRY_TIME, SKYVERN_DIR
from skyvern.exceptions import (
    FailedToNavigateToUrl,
    FailedToReloadPage,
    FailedToStopLoadingPage,
    MissingBrowserStatePage,
    UnknownBrowserType,
    UnknownErrorWhileCreatingBrowserContext,
)
from skyvern.forge.sdk.api.files import get_download_dir, make_temp_directory
from skyvern.forge.sdk.core.skyvern_context import current, ensure_context
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, get_tzinfo_from_proxy
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()


BrowserCleanupFunc = Callable[[], None] | None


def set_browser_console_log(browser_context: BrowserContext, browser_artifacts: BrowserArtifacts) -> None:
    if browser_artifacts.browser_console_log_path is None:
        log_path = f"{settings.LOG_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}/{uuid.uuid4()}.log"
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            # create the empty log file
            with open(log_path, "w") as _:
                pass
        except Exception:
            LOG.warning(
                "Failed to create browser log file",
                log_path=log_path,
                exc_info=True,
            )
            return
        browser_artifacts.browser_console_log_path = log_path

    async def browser_console_log(msg: ConsoleMessage) -> None:
        current_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        key_values = " ".join([f"{key}={value}" for key, value in msg.location.items()])
        format_log = f"{current_time}[{msg.type}]{msg.text} {key_values}\n"
        await browser_artifacts.append_browser_console_log(format_log)

    LOG.info("browser console log is saved", log_path=browser_artifacts.browser_console_log_path)
    browser_context.on("console", browser_console_log)


def set_download_file_listener(browser_context: BrowserContext, **kwargs: Any) -> None:
    async def listen_to_download(download: Download) -> None:
        workflow_run_id = kwargs.get("workflow_run_id")
        task_id = kwargs.get("task_id")
        try:
            async with asyncio.timeout(BROWSER_DOWNLOAD_TIMEOUT):
                file_path = await download.path()
                if file_path.suffix:
                    return

                LOG.info(
                    "No file extensions, going to add file extension automatically",
                    workflow_run_id=workflow_run_id,
                    task_id=task_id,
                    suggested_filename=download.suggested_filename,
                    url=download.url,
                )
                suffix = Path(download.suggested_filename).suffix
                if suffix:
                    LOG.info(
                        "Add extension according to suggested filename",
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        filepath=str(file_path) + suffix,
                    )
                    file_path.rename(str(file_path) + suffix)
                    return
                suffix = Path(download.url).suffix
                if suffix:
                    LOG.info(
                        "Add extension according to download url",
                        workflow_run_id=workflow_run_id,
                        task_id=task_id,
                        filepath=str(file_path) + suffix,
                    )
                    file_path.rename(str(file_path) + suffix)
                    return
                # TODO: maybe should try to parse it from URL response
        except asyncio.TimeoutError:
            LOG.error(
                "timeout to download file, going to cancel the download",
                workflow_run_id=workflow_run_id,
                task_id=task_id,
            )
            await download.cancel()

        except Exception:
            LOG.exception(
                "Failed to add file extension name to downloaded file",
                workflow_run_id=workflow_run_id,
                task_id=task_id,
            )

    def listen_to_new_page(page: Page) -> None:
        page.on("download", listen_to_download)

    browser_context.on("page", listen_to_new_page)


def initialize_download_dir() -> str:
    context = ensure_context()
    return get_download_dir(context.workflow_run_id, context.task_id)


class BrowserContextCreator(Protocol):
    def __call__(
        self, playwright: Playwright, proxy_location: ProxyLocation | None = None, **kwargs: dict[str, Any]
    ) -> Awaitable[tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]]: ...


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
    def update_chromium_browser_preferences(user_data_dir: str, download_dir: str) -> None:
        preference_dst_folder = f"{user_data_dir}/Default"
        os.makedirs(preference_dst_folder, exist_ok=True)

        preference_dst_file = f"{preference_dst_folder}/Preferences"
        preference_template = f"{SKYVERN_DIR}/webeye/chromium_preferences.json"

        preference_file_content = ""
        with open(preference_template, "r") as f:
            preference_file_content = f.read()
            preference_file_content = preference_file_content.replace("MASK_SAVEFILE_DEFAULT_DIRECTORY", download_dir)
            preference_file_content = preference_file_content.replace("MASK_DOWNLOAD_DEFAULT_DIRECTORY", download_dir)
        with open(preference_dst_file, "w") as f:
            f.write(preference_file_content)

    @staticmethod
    def build_browser_args(proxy_location: ProxyLocation | None = None, cdp_port: int | None = None) -> dict[str, Any]:
        video_dir = f"{settings.VIDEO_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}"
        har_dir = (
            f"{settings.HAR_PATH}/{datetime.utcnow().strftime('%Y-%m-%d')}/{BrowserContextFactory.get_subdir()}.har"
        )

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disk-cache-size=1",
            "--start-maximized",
            "--kiosk-printing",
        ]

        if cdp_port:
            browser_args.append(f"--remote-debugging-port={cdp_port}")

        args = {
            "locale": settings.BROWSER_LOCALE,
            "color_scheme": "no-preference",
            "args": browser_args,
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

        if proxy_location:
            if tz_info := get_tzinfo_from_proxy(proxy_location=proxy_location):
                args["timezone_id"] = tz_info.key
        return args

    @staticmethod
    def build_browser_artifacts(
        video_artifacts: list[VideoArtifact] | None = None,
        har_path: str | None = None,
        traces_dir: str | None = None,
        browser_session_dir: str | None = None,
        browser_console_log_path: str | None = None,
    ) -> BrowserArtifacts:
        return BrowserArtifacts(
            video_artifacts=video_artifacts or [],
            har_path=har_path,
            traces_dir=traces_dir,
            browser_session_dir=browser_session_dir,
            browser_console_log_path=browser_console_log_path,
        )

    @classmethod
    def register_type(cls, browser_type: str, creator: BrowserContextCreator) -> None:
        cls._creators[browser_type] = creator

    @classmethod
    async def create_browser_context(
        cls, playwright: Playwright, **kwargs: Any
    ) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
        browser_type = settings.BROWSER_TYPE
        browser_context: BrowserContext | None = None
        try:
            creator = cls._creators.get(browser_type)
            if not creator:
                raise UnknownBrowserType(browser_type)
            browser_context, browser_artifacts, cleanup_func = await creator(playwright, **kwargs)
            set_browser_console_log(browser_context=browser_context, browser_artifacts=browser_artifacts)
            set_download_file_listener(browser_context=browser_context, **kwargs)

            proxy_location: ProxyLocation | None = kwargs.get("proxy_location")
            if proxy_location is not None:
                context = ensure_context()
                context.tz_info = get_tzinfo_from_proxy(proxy_location)

            return browser_context, browser_artifacts, cleanup_func
        except Exception as e:
            if browser_context is not None:
                # FIXME: sometimes it can't close the browser context?
                LOG.error("unexpected error happens after created browser context, going to close the context")
                await browser_context.close()

            if isinstance(e, UnknownBrowserType):
                raise e

            raise UnknownErrorWhileCreatingBrowserContext(browser_type, e) from e

    @classmethod
    def set_validate_browser_context(cls, validator: Callable[[Page], Awaitable[bool]]) -> None:
        cls._validator = validator

    @classmethod
    async def validate_browser_context(cls, page: Page) -> bool:
        if cls._validator is None:
            return True
        return await cls._validator(page)


class VideoArtifact(BaseModel):
    video_path: str | None = None
    video_artifact_id: str | None = None
    video_data: bytes = bytes()


class BrowserArtifacts(BaseModel):
    video_artifacts: list[VideoArtifact] = []
    har_path: str | None = None
    traces_dir: str | None = None
    browser_session_dir: str | None = None
    browser_console_log_path: str | None = None
    _browser_console_log_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

    async def append_browser_console_log(self, msg: str) -> int:
        if self.browser_console_log_path is None:
            return 0

        async with self._browser_console_log_lock:
            async with aiofiles.open(self.browser_console_log_path, "a") as f:
                return await f.write(msg)

    async def read_browser_console_log(self) -> bytes:
        if self.browser_console_log_path is None:
            return b""

        async with self._browser_console_log_lock:
            if not os.path.exists(self.browser_console_log_path):
                return b""

            async with aiofiles.open(self.browser_console_log_path, "rb") as f:
                return await f.read()


def _get_cdp_port(kwargs: dict) -> int | None:
    raw_cdp_port = kwargs.get("cdp_port")
    if isinstance(raw_cdp_port, (int, str)):
        try:
            return int(raw_cdp_port)
        except (ValueError, TypeError):
            return None
    return None


async def _create_headless_chromium(
    playwright: Playwright, proxy_location: ProxyLocation | None = None, **kwargs: dict
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    user_data_dir = make_temp_directory(prefix="skyvern_browser_")
    download_dir = initialize_download_dir()
    BrowserContextFactory.update_chromium_browser_preferences(
        user_data_dir=user_data_dir,
        download_dir=download_dir,
    )
    cdp_port: int | None = _get_cdp_port(kwargs)
    browser_args = BrowserContextFactory.build_browser_args(proxy_location=proxy_location, cdp_port=cdp_port)
    browser_args.update(
        {
            "user_data_dir": user_data_dir,
            "downloads_path": download_dir,
        }
    )

    browser_artifacts = BrowserContextFactory.build_browser_artifacts(har_path=browser_args["record_har_path"])
    browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    return browser_context, browser_artifacts, None


async def _create_headful_chromium(
    playwright: Playwright, proxy_location: ProxyLocation | None = None, **kwargs: dict
) -> tuple[BrowserContext, BrowserArtifacts, BrowserCleanupFunc]:
    user_data_dir = make_temp_directory(prefix="skyvern_browser_")
    download_dir = initialize_download_dir()
    BrowserContextFactory.update_chromium_browser_preferences(
        user_data_dir=user_data_dir,
        download_dir=download_dir,
    )
    cdp_port: int | None = _get_cdp_port(kwargs)
    browser_args = BrowserContextFactory.build_browser_args(proxy_location=proxy_location, cdp_port=cdp_port)
    browser_args.update(
        {
            "user_data_dir": user_data_dir,
            "downloads_path": download_dir,
            "headless": False,
        }
    )
    browser_artifacts = BrowserContextFactory.build_browser_artifacts(har_path=browser_args["record_har_path"])
    browser_context = await playwright.chromium.launch_persistent_context(**browser_args)
    return browser_context, browser_artifacts, None


BrowserContextFactory.register_type("chromium-headless", _create_headless_chromium)
BrowserContextFactory.register_type("chromium-headful", _create_headful_chromium)


class BrowserState:
    instance = None

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
        LOG.error("BrowserState has no page")
        raise MissingBrowserStatePage()

    async def _close_all_other_pages(self) -> None:
        cur_page = await self.get_working_page()
        if not self.browser_context or not cur_page:
            return
        pages = self.browser_context.pages
        for page in pages:
            if page != cur_page:
                await page.close()

    async def check_and_fix_state(
        self,
        url: str | None = None,
        proxy_location: ProxyLocation | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
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
                organization_id=organization_id,
            )
            self.browser_context = browser_context
            self.browser_artifacts = browser_artifacts
            self.browser_cleanup = browser_cleanup
            LOG.info("browser context is created")

        if await self.get_working_page() is None:
            page = await self.browser_context.new_page()
            await self.set_working_page(page, 0)
            await self._close_all_other_pages()

            if url:
                await self.navigate_to_url(page=page, url=url)

    async def navigate_to_url(self, page: Page, url: str, retry_times: int = NAVIGATION_MAX_RETRY_TIME) -> None:
        navigation_error: Exception = FailedToNavigateToUrl(url=url, error_message="")
        for retry_time in range(retry_times):
            LOG.info(f"Trying to navigate to {url} and waiting for 5 seconds.", url=url, retry_time=retry_time)
            try:
                start_time = time.time()
                await page.goto(url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
                end_time = time.time()
                LOG.info(
                    "Page loading time",
                    loading_time=end_time - start_time,
                    url=url,
                )
                await asyncio.sleep(5)
                LOG.info(f"Successfully went to {url}", url=url, retry_time=retry_time)
                return

            except Exception as e:
                navigation_error = e
                LOG.warning(
                    f"Error while navigating to url: {str(navigation_error)}",
                    exc_info=True,
                    url=url,
                    retry_time=retry_time,
                )
                # Wait for 5 seconds before retrying
                await asyncio.sleep(5)
        else:
            LOG.exception(
                f"Failed to navigate to {url} after {retry_times} retries: {str(navigation_error)}",
                url=url,
            )
            if isinstance(navigation_error, Error):
                raise FailedToNavigateToUrl(url=url, error_message=str(navigation_error))
            raise navigation_error

    async def get_working_page(self) -> Page | None:
        # HACK: currently, assuming the last page is always the working page.
        # Need to refactor this logic when we want to manipulate multi pages together
        if self.__page is None or self.browser_context is None or len(self.browser_context.pages) == 0:
            return None

        last_page = self.browser_context.pages[-1]
        if self.__page == last_page:
            return self.__page
        await self.set_working_page(last_page, len(self.browser_context.pages) - 1)
        return last_page

    async def must_get_working_page(self) -> Page:
        page = await self.get_working_page()
        assert page is not None
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

        target_lenght = index + 1
        self.browser_artifacts.video_artifacts.extend(
            [VideoArtifact()] * (target_lenght - len(self.browser_artifacts.video_artifacts))
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
        proxy_location: ProxyLocation | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
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
                organization_id=organization_id,
            )
        except Exception as e:
            error_message = str(e)
            if "net::ERR" not in error_message:
                raise e
            await self.close_current_open_page()
            await self.check_and_fix_state(
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
        await self.__assert_page()

        if not await BrowserContextFactory.validate_browser_context(await self.get_working_page()):
            await self.close_current_open_page()
            await self.check_and_fix_state(
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            await self.__assert_page()

        return page

    async def close_current_open_page(self) -> None:
        await self._close_all_other_pages()
        if self.browser_context is not None:
            await self.browser_context.close()
        self.browser_context = None
        await self.set_working_page(None)

    async def stop_page_loading(self) -> None:
        page = await self.__assert_page()
        try:
            await SkyvernFrame.evaluate(frame=page, expression="window.stop()")
        except Exception as e:
            LOG.exception(f"Error while stop loading the page: {repr(e)}")
            raise FailedToStopLoadingPage(url=page.url, error_message=repr(e))

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
                            LOG.info("Main browser cleanup is excuted")
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

    async def take_screenshot(self, full_page: bool = False, file_path: str | None = None) -> bytes:
        page = await self.__assert_page()
        return await SkyvernFrame.take_screenshot(page=page, full_page=full_page, file_path=file_path)
