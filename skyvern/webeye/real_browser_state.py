from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Literal
from urllib.parse import urlparse

import structlog
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from skyvern.config import settings
from skyvern.constants import (
    BROWSER_CLOSE_TIMEOUT,
    BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
    BROWSER_PAGE_CLOSE_TIMEOUT,
    NAVIGATION_MAX_RETRY_TIME,
)
from skyvern.exceptions import (
    EmptyBrowserContext,
    FailedToNavigateToUrl,
    FailedToReloadPage,
    FailedToStopLoadingPage,
    MissingBrowserStatePage,
)
from skyvern.forge import app
from skyvern.forge.sdk.trace import traced
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_factory import BrowserCleanupFunc, BrowserContextFactory, resolve_video_path
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.cdp_download_interceptor import disable_download_interceptor_for_context
from skyvern.webeye.navigation import is_permanent_navigation_error, navigate_with_retry
from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import CleanupElementTreeFunc, ScrapedPage, ScrapeExcludeFunc
from skyvern.webeye.session_cookies import persist_session_cookies
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame

LOG = structlog.get_logger()

SETTLE_TIME_MS = 750
SETTLE_JITTER_MS = 500
RECOVERABLE_BLANK_PAGE_URLS = {":"}


def _same_page_ignoring_fragment(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
    except Exception:
        return False
    left_url = left_parsed._replace(fragment="").geturl().rstrip("/")
    right_url = right_parsed._replace(fragment="").geturl().rstrip("/")
    return left_url == right_url


class RealBrowserState(BrowserState):
    def __init__(
        self,
        pw: Playwright,
        browser_context: BrowserContext | None = None,
        page: Page | None = None,
        browser_artifacts: BrowserArtifacts = BrowserArtifacts(),
        browser_cleanup: BrowserCleanupFunc = None,
        release_driver_on_close: bool = False,
        engine_selection: BrowserEngineSelection | None = None,
    ):
        self.__page = page
        # An explicitly selected tab (set by NEW_TAB/SWITCH_TAB). When set, it overrides the
        # last-page default in get_working_page so multi-tab targeting is deterministic.
        self.__active_page: Page | None = None
        # Snapshot of the valid pages present when the active tab was pinned. If a page appears
        # that was not in this set, a new tab opened and auto-takes focus (legacy behavior),
        # so the pin is dropped.
        self.__active_page_known_pages: set[Page] = set()
        self.pw = pw
        self.browser_context = browser_context
        self.browser_artifacts = browser_artifacts
        self.browser_cleanup = browser_cleanup
        # Stamped for states attached to a caller-provided remote browser
        # (``browser_address``): the local Playwright driver exists solely for
        # this state and must be released on close even when the remote
        # browser is left running.
        self.release_driver_on_close = release_driver_on_close
        # The engine this state's driver was created with, pinned for the state's lifetime so
        # reconnect starts the same engine and error classification stays this run's identity. None
        # for states built outside the per-run seam (legacy/direct construction).
        self.engine_selection = engine_selection
        # One-shot callbacks fired first inside ``close()``. Cleared after
        # firing so re-entry into ``close()`` is safe.
        self._on_close_callbacks: list[Callable[[], Awaitable[None]]] = []
        # Teardown phases detached because they overran their budget. asyncio only holds tasks
        # weakly, so a still-pending detached drain would be eligible for GC ("Task was destroyed
        # but it is pending!"); we own it strongly here until its done-callback discards it.
        self._detached_teardown_tasks: set[asyncio.Task[None]] = set()

    def add_on_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_close_callbacks.append(callback)

    async def _run_on_close_callbacks(self) -> None:
        callbacks = self._on_close_callbacks
        self._on_close_callbacks = []
        for callback in callbacks:
            try:
                await callback()
            except Exception:
                LOG.debug("on-close callback raised; ignored", exc_info=True)

    async def __assert_page(self) -> Page:
        page = await self.get_working_page()
        if page is not None:
            return page
        pages = (self.browser_context.pages or []) if self.browser_context else []
        LOG.error("BrowserState has no page", urls=[p.url for p in pages])
        raise MissingBrowserStatePage()

    async def _close_all_other_pages(self, discard_orphaned_videos: bool = False) -> None:
        cur_page = await self.get_working_page()
        if not self.browser_context or not cur_page:
            return
        pages = self.browser_context.pages
        for page in pages:
            if page != cur_page:
                if discard_orphaned_videos:
                    # Tombstone before any await: set_popup_video_listener's registration for
                    # this same page may still be in flight, and must observe the tombstone
                    # whenever it resolves rather than re-appending after we remove it below.
                    self.browser_artifacts.discard_page_video(page)
                    await self._discard_video_artifact(page)
                try:
                    async with asyncio.timeout(2):
                        await page.close()
                except asyncio.TimeoutError:
                    LOG.warning("Timeout to close the page. Skip closing the page", url=page.url)
                except Exception:
                    LOG.exception("Error while closing the page", url=page.url)

    async def _discard_video_artifact(self, page: Page) -> None:
        # This page never became the working page — its video must not be registered.
        video = page.video
        if not video:
            return
        page_origin = "unknown"
        try:
            page_origin = urlparse(page.url).hostname or "unknown"
        except Exception:
            pass
        try:
            path = await resolve_video_path(video, settings.POPUP_VIDEO_PATH_TIMEOUT_SECONDS)
        except Exception:
            LOG.warning("Could not get video path to discard orphaned artifact", page_origin=page_origin, exc_info=True)
            return
        if path is None:
            # Best-effort: leave the artifact registered rather than raising — the
            # near-empty video is uploaded as-is instead of silently disappearing.
            LOG.warning("Could not get video path to discard orphaned artifact", page_origin=page_origin)
            return
        video_artifacts = self.browser_artifacts.video_artifacts
        filtered = [va for va in video_artifacts if va.video_path != path]
        if len(filtered) != len(video_artifacts):
            LOG.debug("Discarded orphaned video artifact", video_path=path)
        self.browser_artifacts.video_artifacts = filtered

    async def check_and_fix_state(
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
                workflow_permanent_id=workflow_permanent_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
                engine_selection=self.engine_selection,
            )
            self.browser_context = browser_context
            self.browser_artifacts = browser_artifacts
            self.browser_cleanup = browser_cleanup
            LOG.info("browser context is created")

        if await self.get_working_page() is None:
            page: Page | None = None
            use_existing_page = False
            # Some remote browser sessions bind their capture/streaming to the
            # CDP target that existed at session creation. Opening a new tab
            # and then running _close_all_other_pages detaches that binding
            # from the page the agent actually navigates. Reuse the existing
            # page so the remote session stays aligned with the active target.
            has_remote_browser_session = bool(
                self.browser_artifacts and self.browser_artifacts.remote_browser_session_id
            )
            if (browser_address or has_remote_browser_session) and len(self.browser_context.pages) > 0:
                pages = await self.list_valid_pages()
                if pages:
                    page = pages[-1]
                    use_existing_page = True
            if page is None:
                page = await self.browser_context.new_page()

            await self.set_working_page(page, 0)
            if not use_existing_page:
                await self._close_all_other_pages(discard_orphaned_videos=True)

            if url and not _same_page_ignoring_fragment(page.url, url):
                await self.navigate_to_url(page=page, url=url)

    async def _wait_for_settle(self) -> None:
        total_wait_ms = SETTLE_TIME_MS
        if SETTLE_JITTER_MS > 0:
            total_wait_ms += random.randint(0, SETTLE_JITTER_MS)
        await asyncio.sleep(total_wait_ms / 1000)

    async def navigate_to_url(
        self,
        page: Page,
        url: str,
        retry_times: int = NAVIGATION_MAX_RETRY_TIME,
        wait_until: Literal["load", "domcontentloaded", "commit"] = "load",
    ) -> None:
        await navigate_with_retry(
            navigate=lambda strategy: page.goto(url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS, wait_until=strategy),
            url=url,
            retry_times=retry_times,
            settle=self._wait_for_settle,
            wait_until=wait_until,
        )
        await self._wait_for_challenge_solver(page=page)

    async def _wait_for_challenge_solver(self, page: Page) -> None:
        await app.AGENT_FUNCTION.wait_for_challenge_solver(page=page)

    async def get_working_page(self) -> Page | None:
        if self.__page is None or self.browser_context is None:
            return None

        pages = await self.list_valid_pages()
        if len(pages) == 0:
            LOG.info("No http, https or blank page found in the browser context, return None")
            return None

        # Honor a tab explicitly selected via NEW_TAB/SWITCH_TAB while it is still open.
        # A genuinely new tab auto-takes focus, preserving legacy last-page behavior; a
        # recoverable blank marker from a download flow does not break the selected-tab pin.
        active_page = self.__active_page
        if active_page is not None and not active_page.is_closed() and active_page in pages:
            if all(page in self.__active_page_known_pages for page in pages):
                self.__page = active_page
                return active_page

            new_pages = [page for page in pages if page not in self.__active_page_known_pages]
            if new_pages and all(page.url in RECOVERABLE_BLANK_PAGE_URLS for page in new_pages):
                # Do not add marker pages to known_pages; they should remain ignored until closed.
                self.__page = active_page
                return active_page

        # No (or stale) pin: fall back to the newest valid page.
        self.__active_page = None
        self.__active_page_known_pages = set()
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
                or http_page.url == ":"  # sometimes the page url is ":", which is the blank page
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
            self.__active_page = None
            self.__active_page_known_pages = set()

    async def set_active_page(self, page: Page) -> None:
        self.__active_page = page
        self.__page = page
        self.__active_page_known_pages = set(await self.list_valid_pages())

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
                workflow_permanent_id=workflow_permanent_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
        except Exception as e:
            error_message = e.error_message if isinstance(e, FailedToNavigateToUrl) else str(e)
            if is_permanent_navigation_error(error_message):
                raise
            if "net::ERR" not in error_message:
                raise
            if not await self.close_current_open_page():
                LOG.warning("Failed to close the current open page")
                raise
            await self.check_and_fix_state(
                url=url,
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
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
                workflow_permanent_id=workflow_permanent_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
            page = await self.__assert_page()
        return page

    def is_connected(self) -> bool:
        # A reused browser state (e.g. a persistent debug session) can have a stopped driver
        # after a prior owner's cleanup; page.goto then raises "Connection closed while reading
        # from the driver". A bare pw.stop() leaves browser.is_connected() stale and never flips
        # _close_was_called, so also inspect the shared driver Connection's closed-error.
        context = self.browser_context
        if context is None:
            return False
        impl = getattr(context, "_impl_obj", None)
        if getattr(impl, "_close_was_called", False) is True or getattr(impl, "_closed", False) is True:
            return False
        connection = getattr(impl, "_connection", None)
        if getattr(connection, "_closed_error", None) is not None:
            return False
        browser = getattr(context, "browser", None)
        if browser is None:
            return True
        try:
            return browser.is_connected()
        except Exception:
            return False

    async def reconnect(
        self,
        proxy_location: ProxyLocationInput = None,
        workflow_run_id: str | None = None,
        workflow_permanent_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
    ) -> None:
        # The old driver pipe is gone, so check_and_fix_state must not reuse self.pw; start a
        # fresh Playwright driver and reconnect to the same (still-alive) remote browser.
        stale_pw = self.pw
        self.browser_context = None
        await self.set_working_page(None)
        # Reconnect on the SAME engine this state was pinned to at creation; never silently switch
        # engines underneath a live run. States built outside the per-run seam keep the stock driver.
        if self.engine_selection is not None:
            self.pw = await self.engine_selection.start_driver()
        else:
            self.pw = await async_playwright().start()
        try:
            await self.check_and_fix_state(
                proxy_location=proxy_location,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
            )
        except Exception:
            # The caller abandons this state on failure, so stop the just-started driver too or it leaks.
            try:
                await self.pw.stop()
            except Exception:
                LOG.debug("Failed to stop the new Playwright driver after a failed reconnect", exc_info=True)
            raise
        finally:
            try:
                await stale_pw.stop()
            except Exception:
                LOG.debug("Failed to stop the stale Playwright driver during reconnect", exc_info=True)

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

    async def reload_page(self, degradation: bool = False) -> None:
        page = await self.__assert_page()
        url = page.url

        if not degradation:
            LOG.info("Reload page", url=url)
            try:
                start_time = time.time()
                await page.reload(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
                LOG.info("Page loading time", loading_time=time.time() - start_time)
                await self._wait_for_settle()
                await self._wait_for_challenge_solver(page=page)
            except Exception as e:
                LOG.exception("Error while reload url", error=repr(e))
                raise FailedToReloadPage(url=url, error_message=repr(e))
            return

        strategies: list[str] = ["load", "domcontentloaded", "commit"]
        for i, strategy in enumerate(strategies):
            try:
                LOG.info("Reload page", url=url, wait_until=strategy, degradation_attempt=i)
                start_time = time.time()
                await page.reload(timeout=settings.BROWSER_LOADING_TIMEOUT_MS, wait_until=strategy)
                LOG.info(
                    "Page loading time",
                    loading_time=time.time() - start_time,
                    wait_until=strategy,
                    degraded=i > 0,
                )
                await self._wait_for_settle()
                await self._wait_for_challenge_solver(page=page)
                return
            except Exception as e:
                if i < len(strategies) - 1:
                    LOG.warning(
                        "Reload timed out, degrading wait strategy",
                        url=url,
                        wait_until=strategy,
                        next_strategy=strategies[i + 1],
                        error=repr(e),
                    )
                    continue
                LOG.exception("Error while reload url after degradation", error=repr(e))
                raise FailedToReloadPage(url=url, error_message=repr(e))

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
    ) -> ScrapedPage:
        page = await self.get_working_page()
        if page is not None:
            await self._wait_for_challenge_solver(page=page)

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
            must_included_tags=must_included_tags,
        )

    async def close(self, close_browser_on_completion: bool = True, release_driver: bool | None = None) -> None:
        # ``release_driver`` decouples the local Playwright driver's lifetime
        # from the remote browser's: callers that retain this state for reuse
        # (persistent sessions, parent/child sharing) must pass False; None
        # defers to ``close_browser_on_completion`` plus the creation-time
        # ``release_driver_on_close`` marker.
        if release_driver is None:
            release_driver = close_browser_on_completion or self.release_driver_on_close
        LOG.info("Closing browser state", sampling=True)

        # Each teardown phase runs in its OWN bounded region so a phase that hangs — a
        # cancellation-resistant download drain, a stuck context close, a raising callback —
        # can never consume the budget a later phase needs. In particular the paid-provider
        # cleanup (Browser Use / Anchor / remote-CDP stop/delete) always gets its own attempt,
        # even when interceptor disable, cookie persistence, or context close hangs or fails.
        # Worst-case wall time is the sum of the per-phase budgets:
        # BROWSER_INTERCEPTOR_DISABLE_TIMEOUT + 3 * BROWSER_CLOSE_TIMEOUT.
        if close_browser_on_completion:
            if self.browser_context is not None:
                await self._run_bounded_detachable(
                    disable_download_interceptor_for_context(self.browser_context),
                    BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
                    "download interceptor disable",
                )
            await self._run_bounded_detachable(
                self._teardown_context(),
                BROWSER_CLOSE_TIMEOUT,
                "browser context teardown",
            )
            await self._run_browser_cleanup_bounded()

        await self._stop_driver_bounded(release_driver)

    async def _run_bounded_detachable(self, coro: Awaitable[None], timeout: float, description: str) -> None:
        # Bound a teardown phase WITHOUT relying on cancellation: a stuck download drain or a real
        # Playwright ``context.close`` blocked by an unresolved paused request can ignore the cancel a
        # plain ``asyncio.timeout`` delivers. We race the phase against ``timeout`` and, if it does not
        # finish, best-effort cancel it and move on so the next phase (crucially the paid-provider
        # cleanup) always runs. The detached phase stays explicitly owned so it is never an orphan.
        task = asyncio.ensure_future(coro)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except BaseException:
            # close() itself was cancelled; keep owning the phase so it is not orphaned, then re-raise.
            task.cancel()
            self._own_detached_task(task, description)
            raise
        if task not in done:
            LOG.warning(
                "Teardown phase exceeded its budget; detaching so later teardown still runs",
                phase=description,
                timeout=timeout,
            )
            task.cancel()
            self._own_detached_task(task, description)
            return
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.warning("Teardown phase failed", phase=description, error_type=type(error).__name__)

    def _own_detached_task(self, task: asyncio.Task[None], description: str) -> None:
        # A cancellation-resistant phase can outlive close(). We hold a strong reference until it
        # finishes (asyncio holds tasks only weakly), and the done-callback retrieves its eventual
        # exception so it is neither an orphan, a GC'd pending task, nor a source of "Task exception
        # was never retrieved". The strong ref is discarded only after completion.
        self._detached_teardown_tasks.add(task)

        def _retrieve(finished: asyncio.Task[None]) -> None:
            try:
                if finished.cancelled():
                    return
                error = finished.exception()
                if error is not None:
                    LOG.debug(
                        "Detached teardown phase raised after detach",
                        phase=description,
                        error_type=type(error).__name__,
                    )
            finally:
                self._detached_teardown_tasks.discard(finished)

        task.add_done_callback(_retrieve)

    async def _teardown_context(self) -> None:
        # Only fire on-close observers on a real teardown. Shared / parent-child close calls pass
        # ``close_browser_on_completion=False`` to leave the browser alive for another run; firing
        # callbacks then would stop the surviving run's publisher and freeze its livestream.
        await self._run_on_close_callbacks()
        if self.browser_context is None:
            return
        LOG.info("Closing browser context and its pages")
        session_dir = self.browser_artifacts.browser_session_dir if self.browser_artifacts else None
        try:
            await persist_session_cookies(self.browser_context, session_dir)
        except Exception:
            LOG.warning("Failed to persist session cookies during teardown", exc_info=True)
        try:
            await self.browser_context.close()
        except Exception:
            LOG.warning("Failed to close browser context", exc_info=True)
        LOG.info("Main browser context and all its pages are closed")

    async def _run_browser_cleanup_bounded(self) -> None:
        cleanup = self.browser_cleanup
        if cleanup is None or self.browser_context is None:
            return
        # One-shot: a re-entrant close() must not stop/delete the paid provider twice.
        self.browser_cleanup = None
        try:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                try:
                    await cleanup()
                    LOG.info("Main browser cleanup is executed")
                except Exception:
                    LOG.warning("Failed to execute browser cleanup", exc_info=True)
        except asyncio.TimeoutError:
            LOG.error("Timeout executing browser cleanup")

    async def _stop_driver_bounded(self, release_driver: bool) -> None:
        try:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                if self.pw and release_driver:
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

    @traced(name="skyvern.browser.post_action_screenshot")
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
