from __future__ import annotations

import asyncio
import random
import time
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from skyvern.config import settings
from skyvern.constants import (
    BROWSER_CLOSE_TIMEOUT,
    BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
    BROWSER_PAGE_CLOSE_TIMEOUT,
    CRASHED_PAGE_CLOSE_ATTEMPTS,
    NAVIGATION_MAX_RETRY_TIME,
)
from skyvern.exceptions import (
    BrowserStateDiagnostic,
    EmptyBrowserContext,
    FailedToNavigateToUrl,
    FailedToReloadPage,
    FailedToStopLoadingPage,
    MissingBrowserStatePage,
)
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.trace import traced
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.webeye.browser_artifacts import BrowserArtifacts, DownloadBinding
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_factory import BrowserCleanupFunc, BrowserContextFactory, resolve_artifact_path
from skyvern.webeye.browser_health import BrowserOperation
from skyvern.webeye.browser_state import BLANK_PAGE_URLS, BrowserState
from skyvern.webeye.cdp_download_interceptor import disable_download_interceptor_for_context
from skyvern.webeye.driver_connection import close_driver_connection_on_transport_loss
from skyvern.webeye.navigation import is_permanent_navigation_error, navigate_with_retry
from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import CleanupElementTreeFunc, ScrapedPage, ScrapeExcludeFunc
from skyvern.webeye.session_cookies import persist_session_cookies
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame, is_engine_timeout

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
        self._disconnect_listener_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()
        self._disconnect_listener_browser: Browser | None = None
        self._crash_listener_pages: weakref.WeakSet[Page] = weakref.WeakSet()
        self._crashed_pages: weakref.WeakSet[Page] = weakref.WeakSet()
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
        # A release retry after a transient DB failure must not repeat local CDP teardown.
        self._remote_driver_detached = False
        self._browser_state_diagnostic: BrowserStateDiagnostic | None = None
        # HTTP status of the most recent navigate_to_url (None until one runs, or when it produced no
        # response). Read by the Task V3 loop to classify a dead/removed starting URL; v1 ignores it.
        self.last_navigation_status: int | None = None
        self._ever_connected = browser_context is not None
        self._close_requested = False
        if browser_context is not None:
            self._register_disconnect_listeners(browser_context)

    @property
    def browser_context(self) -> BrowserContext | None:
        return self._browser_context

    @browser_context.setter
    def browser_context(self, context: BrowserContext | None) -> None:
        # Every attachment must arm crash reaping, including swaps made from outside this class:
        # an unwatched crashed tab keeps winning get_working_page and strands every later attach.
        self._browser_context = context
        if context is not None:
            self._watch_context_for_crashes(context)

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
        recovered_page = await self._reopen_lost_working_page()
        if recovered_page is not None:
            return recovered_page
        pages = (self.browser_context.pages or []) if self.browser_context else []
        detected_at = datetime.now(UTC)
        diagnostic = self.get_browser_state_diagnostic()
        LOG.error(
            "BrowserState has no page",
            urls=[p.url for p in pages],
            browser_session_id=getattr(self.browser_artifacts, "remote_browser_session_id", None),
            detected_at=detected_at.isoformat(),
            disconnect_reason=diagnostic.reason if diagnostic else None,
            disconnect_observed_at=diagnostic.disconnect_observed_at.isoformat() if diagnostic else None,
            disconnect_observation_source=diagnostic.observation_source if diagnostic else None,
        )
        raise MissingBrowserStatePage(diagnostic=diagnostic, detected_at=detected_at)

    async def _reopen_lost_working_page(self) -> Page | None:
        # A tab can die on its own (a download-turned-navigation, a renderer crash) while the
        # context survives. Recover here rather than in each consumer: every caller that needs a
        # page treats "no page" as fatal, so one of them recovering only relocates the failure.
        if self.browser_context is None or not self.is_connected():
            return None
        lost_page = self.__page
        restore_url = lost_page.url if lost_page is not None else ""
        try:
            page = await self.browser_context.new_page()
        except Exception:
            LOG.warning("Failed to re-open a working page after the previous one was lost", exc_info=True)
            return None
        await self.set_working_page(page)
        if restore_url and restore_url not in BLANK_PAGE_URLS:
            try:
                await self.navigate_to_url(page=page, url=restore_url)
            except Exception:
                LOG.warning(
                    "Re-opened the working page but could not restore the URL it was on",
                    url=restore_url,
                    exc_info=True,
                )
        LOG.info("Re-opened the working page after it was lost", url=restore_url)
        return page

    async def _close_all_other_pages(self, discard_orphaned_videos: bool = False) -> None:
        cur_page = await self.get_working_page()
        if not self.browser_context or not cur_page:
            return
        pages = self.browser_context.pages
        for page in pages:
            if page != cur_page:
                # Every crashed page keeps its recording, not just the working one: a crashed tab can
                # still be here with its close in flight or timed out, and there is no way to tell
                # from here which one the run was driving. That keeps a spurious second RECORDING for
                # a crashed non-working tab, which is the cheaper of the two errors — the other is
                # deleting the recording of the run that just crashed.
                if discard_orphaned_videos and page not in self._crashed_pages:
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

    def open_pages(self) -> list[Page]:
        return list(self.browser_context.pages) if self.browser_context else []

    async def close_pages_opened_after(self, baseline_pages: set[Page]) -> None:
        # Close only pages opened after baseline_pages was captured; preserve the baseline set
        # (e.g. a pre-loop deliverable tab) and the current working page.
        if not self.browser_context:
            return
        working_page = await self.get_working_page()
        for page in list(self.browser_context.pages):
            if page in baseline_pages or page is working_page:
                continue
            try:
                async with asyncio.timeout(BROWSER_PAGE_CLOSE_TIMEOUT):
                    await page.close()
            except Exception:
                LOG.warning("Error while closing loop-iteration page", url=page.url)

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
            path = await resolve_artifact_path(video, settings.POPUP_VIDEO_PATH_TIMEOUT_SECONDS)
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
        download_binding: DownloadBinding | None = None,
    ) -> None:
        if self.browser_context is None:
            LOG.info("creating browser context")
            context = skyvern_context.current()
            # When recreation omits a binding, preserve the prior artifacts' binding instead of
            # downgrading to RUN_DIR.
            effective_download_binding = download_binding
            if effective_download_binding is None:
                effective_download_binding = (
                    self.browser_artifacts.download_binding if self.browser_artifacts else DownloadBinding.RUN_DIR
                )
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
                browser_address_is_server_assigned=bool(context and context.browser_address_is_server_assigned),
                browser_profile_id=browser_profile_id,
                engine_selection=self.engine_selection,
                download_binding=effective_download_binding,
            )
            self.browser_context = browser_context
            self.browser_artifacts = browser_artifacts
            self.browser_cleanup = browser_cleanup
            self._browser_state_diagnostic = None
            self._ever_connected = True
            self._close_requested = False
            self._register_disconnect_listeners(browser_context)
            # Strikes describe the browser that earned them; a replacement starts even.
            skyvern_context.record_browser_success()
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
        self.last_navigation_status = await navigate_with_retry(
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
            if http_page not in self._crashed_pages
            and (
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
            skyvern_frame = await SkyvernFrame.create_instance(frame=page, engine_selection=self.engine_selection)
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
        return await self.__assert_page()

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

    def _watch_context_for_crashes(self, context: BrowserContext) -> None:
        if context not in self._disconnect_listener_contexts:
            try:
                context.on("close", self._on_browser_context_closed)
                context.on("page", self._watch_page_for_crash)
                self._disconnect_listener_contexts.add(context)
            except Exception:
                LOG.debug("Failed to register browser context disconnect listener", exc_info=True)

        # The "page" event only fires for tabs opened after registration, and both call sites can
        # attach to a context that already has pages.
        try:
            for existing_page in list(context.pages):
                self._watch_page_for_crash(existing_page)
        except Exception:
            LOG.debug("Failed to sweep existing pages for crash listeners", exc_info=True)

    def _register_disconnect_listeners(self, context: BrowserContext) -> None:
        self._watch_context_for_crashes(context)
        try:
            browser = context.browser
        except Exception:
            LOG.debug("Failed to read browser for disconnect listener registration", exc_info=True)
            return
        if browser is None or browser is self._disconnect_listener_browser:
            return
        try:
            browser.on("disconnected", self._on_browser_disconnected)
            self._disconnect_listener_browser = browser
        except Exception:
            LOG.debug("Failed to register browser disconnect listener", exc_info=True)

    def _watch_page_for_crash(self, page: Page) -> None:
        if page in self._crash_listener_pages:
            return
        try:
            page.on("crash", self._on_page_crashed)
            self._crash_listener_pages.add(page)
        except Exception:
            LOG.debug("Failed to register page crash listener", exc_info=True)

    def _on_page_crashed(self, page: Page) -> None:
        # A crashed tab stays in context.pages and keeps winning get_working_page, so later operations
        # and CDP attaches land on a dead target. Closing it lets _reopen_lost_working_page recover.
        LOG.warning("Page crashed; closing the crashed tab so a replacement can be opened", url=page.url)
        # Recorded before the close is scheduled: the close is a detached task, and until it lands the
        # crashed page is still in context.pages. Callers in that window must not be handed it.
        self._crashed_pages.add(page)
        try:
            task = asyncio.get_running_loop().create_task(self._close_crashed_page(page))
        except RuntimeError:
            LOG.debug("No running event loop to close the crashed page on", exc_info=True)
            return
        self._own_detached_task(task, "close_crashed_page")

    async def _close_crashed_page(self, page: Page) -> None:
        # The crash event fires once. Excluding the page from selection keeps this process healthy, but
        # only closing the target unblocks later CDP attaches, so a hung close is retried up to
        # CRASHED_PAGE_CLOSE_ATTEMPTS before the target is reported stranded. Bounded by racing the
        # close rather than by asyncio.timeout, for the reason _run_bounded_detachable already
        # documents: a Playwright close can ignore the cancel a timeout delivers, and that would look
        # like success here and skip the retry. A close that raises has finished, so it is not retried.
        for _ in range(CRASHED_PAGE_CLOSE_ATTEMPTS):
            closing = asyncio.ensure_future(page.close())
            done, _pending = await asyncio.wait({closing}, timeout=BROWSER_PAGE_CLOSE_TIMEOUT)
            if closing not in done:
                closing.cancel()
                self._own_detached_task(closing, "close_crashed_page_attempt")
                continue
            error = closing.exception() if not closing.cancelled() else None
            if error is not None:
                LOG.warning("Error while closing a crashed page", url=page.url, error_type=type(error).__name__)
            return
        LOG.warning("Timeout closing a crashed page; the target stays stranded", url=page.url)

    def _on_browser_context_closed(self, context: BrowserContext) -> None:
        if context is not self.browser_context:
            return
        self._record_disconnect(
            "browser_context_close_event",
            event="browser_context_close",
            observation_source="browser_event",
        )

    def _on_browser_disconnected(self, browser: Browser) -> None:
        if browser is not self._disconnect_listener_browser:
            return
        self._record_disconnect(
            "browser_disconnected_event",
            event="browser_disconnected",
            observation_source="browser_event",
        )

    def _record_disconnect(
        self,
        reason: str,
        *,
        event: str = "browser_context_disconnected",
        observation_source: str = "liveness_probe",
    ) -> None:
        if self._browser_state_diagnostic is not None or not self._ever_connected:
            return
        disconnect_observed_at = datetime.now(UTC)
        browser_session_id = getattr(self.browser_artifacts, "remote_browser_session_id", None)
        self._browser_state_diagnostic = BrowserStateDiagnostic(
            reason=reason,
            disconnect_observed_at=disconnect_observed_at,
            browser_session_id=browser_session_id,
            event=event,
            observation_source=observation_source,
        )
        # A disconnect observed after close() was requested is the teardown we asked for; only an
        # unrequested one is worth a warning.
        log = LOG.info if self._close_requested else LOG.warning
        log(
            "Browser state disconnected",
            browser_session_id=browser_session_id,
            disconnect_reason=reason,
            disconnect_event=event,
            disconnect_observed_at=disconnect_observed_at.isoformat(),
            disconnect_observation_source=observation_source,
            close_requested=self._close_requested,
        )

    def get_browser_state_diagnostic(self) -> BrowserStateDiagnostic | None:
        return self._browser_state_diagnostic

    def _connection_status(self) -> tuple[bool, str | None]:
        # A reused browser state (e.g. a persistent debug session) can have a stopped driver
        # after a prior owner's cleanup; page.goto then raises "Connection closed while reading
        # from the driver". A bare pw.stop() leaves browser.is_connected() stale and never flips
        # _close_was_called, so also inspect the shared driver Connection's closed-error.
        context = self.browser_context
        if context is None:
            return False, "browser_context_missing"
        impl = getattr(context, "_impl_obj", None)
        if getattr(impl, "_close_was_called", False) is True:
            return False, "browser_context_close_called"
        if getattr(impl, "_closed", False) is True:
            return False, "browser_context_closed"
        connection = getattr(impl, "_connection", None)
        if getattr(connection, "_closed_error", None) is not None:
            return False, "playwright_driver_connection_closed"
        browser = getattr(context, "browser", None)
        if browser is None:
            return True, None
        try:
            connected = bool(browser.is_connected())
        except Exception as exc:
            return False, f"browser_connection_probe_failed:{type(exc).__name__}"
        return connected, None if connected else "browser_context_disconnected"

    def is_connected(self) -> bool:
        connected, reason = self._connection_status()
        if not connected and reason is not None:
            self._record_disconnect(reason)
        return connected

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
        # check_and_fix_state rebuilds through the factory; forward this session's download binding so the
        # creator seam preserves the provider-selected destination on reconnect. The binding is carried
        # forward, never overridden after the fact, so a genuine provider change is not mislabeled.
        prior_download_binding = (
            self.browser_artifacts.download_binding if self.browser_artifacts else DownloadBinding.RUN_DIR
        )
        self.browser_context = None
        await self.set_working_page(None)
        # Reconnect on the SAME engine this state was pinned to at creation; never silently switch
        # engines underneath a live run. States built outside the per-run seam keep the stock driver.
        if self.engine_selection is not None:
            self.pw = await self.engine_selection.start_driver()
        else:
            self.pw = await async_playwright().start()
            close_driver_connection_on_transport_loss(self.pw)
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
                download_binding=prior_download_binding,
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

    def _record_reload_timeout(self, error: Exception) -> None:
        """Only a deadline counts toward browser health. A reload that fails for a nameable reason —
        a navigation error, a closed target — says the browser answered."""
        if is_engine_timeout(error, self.engine_selection):
            skyvern_context.record_browser_timeout(BrowserOperation.RELOAD)

    async def reload_page(self, degradation: bool = False, page: Page | None = None) -> None:
        # A caller that pins its own page passes it, since the working-page accessor would reload
        # (and repoint to) the newest tab instead.
        if page is None:
            page = await self.__assert_page()
        url = page.url

        if not degradation:
            LOG.info("Reload page", url=url)
            try:
                start_time = time.time()
                await page.reload(timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
                LOG.info("Page loading time", loading_time=time.time() - start_time)
                skyvern_context.record_browser_success()
                await self._wait_for_settle()
                await self._wait_for_challenge_solver(page=page)
            except Exception as e:
                self._record_reload_timeout(e)
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
                skyvern_context.record_browser_success()
                await self._wait_for_settle()
                await self._wait_for_challenge_solver(page=page)
                return
            except Exception as e:
                self._record_reload_timeout(e)
                if i < len(strategies) - 1:
                    LOG.warning(
                        "Reload timed out, degrading wait strategy",
                        url=url,
                        wait_until=strategy,
                        next_strategy=strategies[i + 1],
                        error=repr(e),
                    )
                    continue
                LOG.warning("Error while reload url after degradation", error=repr(e), exc_info=True)
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
        allow_transient_ui_suppression: bool = False,
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
            allow_transient_ui_suppression=allow_transient_ui_suppression,
        )

    async def close(self, close_browser_on_completion: bool = True, release_driver: bool | None = None) -> bool:
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
        recording_finalized = False
        if close_browser_on_completion or release_driver:
            if self.browser_context is not None:
                await self._run_bounded_detachable(
                    disable_download_interceptor_for_context(self.browser_context),
                    BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
                    "download interceptor disable",
                )
        if close_browser_on_completion:
            # Only a teardown that closes the context is a requested disconnect; the keep-alive path
            # leaves the browser to a later owner, whose disconnects are still unrequested.
            self._close_requested = True
            recording_finalized = await self._run_bounded_detachable(
                self._teardown_context(),
                BROWSER_CLOSE_TIMEOUT,
                "browser context teardown",
            )
            await self._run_browser_cleanup_bounded()

        await self._stop_driver_bounded(release_driver)
        return recording_finalized

    async def detach_remote_driver(self) -> None:
        """Release this process's adopted-remote resources without closing the remote browser.

        Unlike ``close(..., release_driver=True)``, failures propagate so the persistent-session
        owner can leave database occupancy intact. The operation is idempotent: a successful
        interceptor disable removes its context binding, while a successful Playwright stop is
        recorded locally for a release retry that only needs to finish the database CAS.
        """
        if self._remote_driver_detached:
            return
        if self.browser_context is not None:
            interceptor = getattr(self.browser_context, "_skyvern_cdp_download_interceptor", None)
            if interceptor is not None:
                await interceptor.disable()
                if getattr(self.browser_context, "_skyvern_cdp_download_interceptor", None) is interceptor:
                    self.browser_context._skyvern_cdp_download_interceptor = None  # type: ignore[attr-defined]
        if self.pw is not None:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                await self.pw.stop()
        self._remote_driver_detached = True

    async def _run_bounded_detachable(self, coro: Awaitable[None], timeout: float, description: str) -> bool:
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
            return False
        if task.cancelled():
            return False
        error = task.exception()
        if error is not None:
            LOG.warning("Teardown phase failed", phase=description, error_type=type(error).__name__)
            return False
        return True

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
        await self.browser_context.close()
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
            engine_selection=self.engine_selection,
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
            engine_selection=self.engine_selection,
        )
