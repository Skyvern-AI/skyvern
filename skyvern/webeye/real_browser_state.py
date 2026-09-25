from __future__ import annotations

import asyncio
import random
import time
import weakref
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict
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
    UnresolvableNavigationHost,
)
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.trace import traced
from skyvern.schemas.runs import ProxyLocationInput
from skyvern.webeye.browser_acquisition_sample import (
    begin_browser_acquisition_sample,
    close_browser_acquisition_sample,
    current_browser_acquisition_sample,
)
from skyvern.webeye.browser_artifacts import BrowserArtifacts, DownloadBinding
from skyvern.webeye.browser_engine import BrowserEngineSelection
from skyvern.webeye.browser_factory import BrowserCleanupFunc, BrowserContextFactory, resolve_artifact_path
from skyvern.webeye.browser_health import BrowserOperation
from skyvern.webeye.browser_runtime_events import (
    AcquireMode,
    BrowserRuntimeLogContext,
    DisconnectEvidence,
    DisconnectKind,
    RunPhase,
    RuntimeEndReason,
    RuntimeEventReason,
    log_browser_runtime_event,
    with_acquired_browser_runtime,
)
from skyvern.webeye.browser_state import BLANK_PAGE_URLS, BrowserState
from skyvern.webeye.cdp_download_interceptor import (
    disable_download_interceptor_for_context,
    has_download_interceptor_for_context,
)
from skyvern.webeye.display_recorder import DisplayRecorder, release_display_recorder
from skyvern.webeye.driver_connection import close_driver_connection_on_transport_loss, driver_transport_error_future
from skyvern.webeye.navigation import is_permanent_navigation_error, navigate_with_retry
from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import CleanupElementTreeFunc, ScrapedPage, ScrapeExcludeFunc
from skyvern.webeye.session_cookies import persist_session_cookies
from skyvern.webeye.utils.page import ScreenshotMode, SkyvernFrame, is_engine_timeout

LOG = structlog.get_logger()

SETTLE_TIME_MS = 750
SETTLE_JITTER_MS = 500
RECOVERABLE_BLANK_PAGE_URLS = {":"}


class _BrowserConnectionProbeFailed(str):
    """Diagnostic reason whose stale driver may be stopped before replacement."""


class _ReadyLatency(TypedDict, total=False):
    browser_request_to_ready_seconds: float


@dataclass(frozen=True)
class _RuntimeEndObservation:
    context: BrowserContext | None
    driver: Playwright
    diagnostic: BrowserStateDiagnostic
    reason: RuntimeEventReason
    source: Literal["liveness_probe", "browser_event", "driver_event"]
    expected: bool
    kind: DisconnectKind
    evidence: DisconnectEvidence
    close_requested: bool
    run_phase: RunPhase


def browser_context_stopped_reason(context: BrowserContext | None) -> str | None:
    """Why this context's driver is unusable, or None while it still looks live.

    A reused browser state (e.g. a persistent debug session) can have a stopped driver after a
    prior owner's cleanup; page.goto then raises "Connection closed while reading from the
    driver". A bare pw.stop() leaves browser.is_connected() stale and never flips
    _close_was_called, so the shared driver Connection's closed-error is inspected too.
    """
    if context is None:
        return "browser_context_missing"
    impl = getattr(context, "_impl_obj", None)
    if getattr(impl, "_close_was_called", False) is True:
        return "browser_context_close_called"
    if getattr(impl, "_closed", False) is True:
        return "browser_context_closed"
    connection = getattr(impl, "_connection", None)
    if getattr(connection, "_closed_error", None) is not None:
        return "playwright_driver_connection_closed"
    browser = getattr(context, "browser", None)
    if browser is None:
        return None
    try:
        connected = bool(browser.is_connected())
    except Exception as exc:
        return _BrowserConnectionProbeFailed(f"browser_connection_probe_failed:{type(exc).__name__}")
    return None if connected else "browser_context_disconnected"


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


_LIVE_BROWSER_STATES: weakref.WeakSet[RealBrowserState] = weakref.WeakSet()


def local_paths_of_open_browsers() -> list[str]:
    """Local files and folders that a browser still open in this process writes to, whichever run opened it."""
    paths: list[str] = []
    for state in list(_LIVE_BROWSER_STATES):
        try:
            reason = browser_context_stopped_reason(state.browser_context)
            # A failed connection probe cannot tell open from closed, so its files are kept.
            if reason is not None and not isinstance(reason, _BrowserConnectionProbeFailed):
                continue
            paths.extend(state.browser_artifacts.local_paths())
        except Exception:
            LOG.debug("Failed to read an open browser's local paths", exc_info=True)
    return paths


def expect_process_driver_teardown() -> None:
    # A released persistent session keeps its driver cached for reuse, so without this intent a process-wide
    # driver kill reads as a driver that died on its own.
    for state in list(_LIVE_BROWSER_STATES):
        try:
            if state.browser_context is None or not state._connection_status()[0]:
                continue
            state.mark_run_released()
            state._expect_runtime_end("driver_release", state.browser_context)
        except Exception:
            LOG.debug("Failed to record driver teardown intent", exc_info=True)


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
        browser_context_route_policy_url: str | None = None,
        runtime_event_context: BrowserRuntimeLogContext | None = None,
        defer_runtime_events: bool = False,
    ):
        self._runtime_event_context = runtime_event_context or BrowserRuntimeLogContext.current()
        self._run_released = False
        if engine_selection is not None:
            self._runtime_event_context = replace(self._runtime_event_context, browser_engine=engine_selection.name)
        self._acquisition_reported_context: BrowserRuntimeLogContext | None = None
        self._expected_runtime_ends: weakref.WeakKeyDictionary[BrowserContext, RuntimeEndReason] = (
            weakref.WeakKeyDictionary()
        )
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
        self._driver_listener_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()
        self._disconnect_listener_browser: Browser | None = None
        self._disconnect_listener_browser_context: BrowserContext | None = None
        self._crash_listener_pages: weakref.WeakSet[Page] = weakref.WeakSet()
        self._crashed_pages: weakref.WeakSet[Page] = weakref.WeakSet()
        self._crashed_targets_closing: weakref.WeakSet[Page] = weakref.WeakSet()
        # Cleared while a recovery is producing the working page (opening it, closing crashed targets,
        # restoring its URL): consumers wait for the finished page instead of driving about:blank or
        # opening a tab of their own.
        self._working_page_ready = asyncio.Event()
        self._working_page_ready.set()
        # The crash reaper and the caller whose action just failed both reach for a replacement page
        # at the same moment; single-flight keeps that one page, not one per racer.
        self._reopen_working_page_lock = asyncio.Lock()
        # Browsers without a persistent-session id cannot use the S3 registry. The state is still
        # the exclusive owner across an in-process remote-driver reconnect, so it retains the
        # trusted registration records until the state itself closes.
        self._sessionless_init_script_registrations: list[Any] = []
        # The URL participates in route-policy feature targeting. Keep the value that created the
        # context so a later driver reconnect evaluates the replacement with the same inputs.
        self.browser_context_route_policy_url = browser_context_route_policy_url
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
        # The URL that status came back on, recorded from the same response so the pair cannot disagree.
        self.last_navigation_url: str | None = None
        # The proxy this browser was actually built with. Downstream layers see only a flattened
        # sentence, which cannot say which hop it went through.
        self.built_with_proxy_location: ProxyLocationInput = None
        self._ever_connected = browser_context is not None
        self._close_requested = False
        self._runtime_events_deferred = defer_runtime_events
        self._deferred_runtime_end: _RuntimeEndObservation | None = None
        self._deferred_retired_runtime_ends: list[tuple[_RuntimeEndObservation, BrowserRuntimeLogContext]] = []
        self._reconnect_stale_observer: (
            Callable[[BrowserContext | Browser | Playwright, Literal["context", "browser", "driver"]], None] | None
        ) = None
        if browser_context is not None:
            self._register_disconnect_listeners(browser_context)
        _LIVE_BROWSER_STATES.add(self)

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

    def bind_runtime_event_context(self, context: BrowserRuntimeLogContext) -> None:
        self._run_released = False
        self._runtime_event_context = replace(
            context,
            browser_session_id=context.browser_session_id or self._runtime_event_context.browser_session_id,
        ).with_browser_dimensions_of(self._runtime_event_context)

    @property
    def runtime_event_context(self) -> BrowserRuntimeLogContext:
        return self._runtime_event_context

    def mark_run_released(self) -> None:
        self._run_released = True

    @property
    def _run_phase(self) -> RunPhase:
        return "after_release" if self._run_released else "active"

    def record_browser_acquisition(
        self, acquire_mode: AcquireMode, requested_at_monotonic: float | None = None
    ) -> None:
        previous = self._acquisition_reported_context
        current = self._runtime_event_context
        if (
            previous is not None
            and previous.workflow_run_id == current.workflow_run_id
            and (current.workflow_run_id is not None or previous.task_id == current.task_id)
            and previous.browser_session_id == current.browser_session_id
        ):
            return
        self._acquisition_reported_context = self._runtime_event_context
        ready_latency: _ReadyLatency = {}
        # A reuse hands over a browser that was already ready, so it has no request-to-ready latency.
        if requested_at_monotonic is not None and acquire_mode != "reuse":
            ready_latency["browser_request_to_ready_seconds"] = round(time.monotonic() - requested_at_monotonic, 3)
        log_browser_runtime_event(
            LOG,
            "Browser acquired",
            context=self._runtime_event_context,
            event="acquire_result",
            outcome="success",
            acquire_mode=acquire_mode,
            reason="ready",
            observation_source="acquisition",
            expected=True,
            **ready_latency,
        )
        self.publish_runtime_events()

    def publish_runtime_events(self) -> None:
        self._runtime_events_deferred = False
        self._replay_deferred_runtime_end()
        self._observe_current_driver_loss()

    def _expect_runtime_end(self, reason: RuntimeEndReason, context: BrowserContext | None) -> None:
        if context is not None:
            if context is self.browser_context:
                # A completed failure may still have its callback queued when teardown starts.
                self._observe_current_driver_loss()
                connected, stopped_reason = self._connection_status()
                if not connected and stopped_reason is not None:
                    self._record_disconnect(stopped_reason)
            self._expected_runtime_ends[context] = reason

    def add_sessionless_init_script_registration(self, registration: Any) -> None:
        self._sessionless_init_script_registrations.append(registration)

    @property
    def sessionless_init_script_registrations(self) -> tuple[Any, ...]:
        return tuple(self._sessionless_init_script_registrations)

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
        async with self._reopen_working_page_lock:
            already_reopened = await self.get_working_page()
            if already_reopened is not None:
                await self._close_crashed_targets()
                return already_reopened
            lost_page = self.__page
            restore_url = lost_page.url if lost_page is not None else ""
            # A fresh event per recovery rather than clear(): the browser-API census counts every
            # .clear() call site as a page sink.
            ready = self._working_page_ready = asyncio.Event()
            try:
                # Bounded: every get_working_page consumer waits on this recovery, so a hung CDP call must
                # fail it within the bound rather than hold them all.
                try:
                    async with asyncio.timeout(BROWSER_PAGE_CLOSE_TIMEOUT):
                        page = await self.browser_context.new_page()
                except Exception:
                    LOG.warning("Failed to re-open a working page after the previous one was lost", exc_info=True)
                    return None
                await self.set_working_page(page)
                # Whoever opens the replacement closes the crashed targets, and does it before a restore
                # that can retry for minutes: the reaper may be queued behind this lock.
                await self._close_crashed_targets()
                if restore_url and restore_url not in BLANK_PAGE_URLS:
                    try:
                        await self.navigate_to_url(page=page, url=restore_url)
                    except Exception:
                        LOG.warning(
                            "Re-opened the working page but could not restore the URL it was on",
                            url=restore_url,
                            exc_info=True,
                        )
            finally:
                ready.set()
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
        download_binding: DownloadBinding | None = None,
        display_recording_owner_id: str | None = None,
        reconcile_persistent_init_scripts: bool = False,
        sessionless_init_script_registrations: Collection[Any] = (),
    ) -> None:
        if self.browser_context is None:
            LOG.info("creating browser context")
            context = skyvern_context.current()
            effective_route_policy_url = (
                browser_context_route_policy_url or self.browser_context_route_policy_url or url
            )
            reconcile_persistent_init_scripts = reconcile_persistent_init_scripts or browser_session_id is not None
            # When recreation omits a binding, preserve the prior artifacts' binding instead of
            # downgrading to RUN_DIR.
            effective_download_binding = download_binding
            if effective_download_binding is None:
                effective_download_binding = (
                    self.browser_artifacts.download_binding if self.browser_artifacts else DownloadBinding.RUN_DIR
                )
            # A replacement can run elsewhere than the browser it replaces (a vendor creator degrading to a
            # local launch), so restamp from what its creator records instead of keeping the old runtime.
            sample_token = None if current_browser_acquisition_sample() else begin_browser_acquisition_sample()
            try:
                (
                    browser_context,
                    browser_artifacts,
                    browser_cleanup,
                ) = await BrowserContextFactory.create_browser_context(
                    self.pw,
                    url=url,
                    _browser_context_route_policy_url=effective_route_policy_url,
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
                    browser_session_id=browser_session_id,
                    engine_selection=self.engine_selection,
                    download_binding=effective_download_binding,
                    display_recording_owner_id=display_recording_owner_id,
                    _reconcile_persistent_init_scripts=reconcile_persistent_init_scripts,
                    _sessionless_init_script_registrations=tuple(sessionless_init_script_registrations),
                )
                self._runtime_event_context = with_acquired_browser_runtime(self._runtime_event_context)
            finally:
                close_browser_acquisition_sample(sample_token)
            self.built_with_proxy_location = proxy_location
            self.browser_context = browser_context
            self.browser_context_route_policy_url = effective_route_policy_url
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
                try:
                    await self.navigate_to_url(page=page, url=url)
                finally:
                    # Arm ONLY around the optional initial navigate: a successful first navigation records the
                    # target page; a permanent navigation failure still records the current diagnostic/error
                    # page; the original navigation exception propagates unchanged (arm never masks it).
                    self._arm_display_recorder()
                return

        # No initial navigation ran (page already existed, or no/same URL): arm now that a working page exists.
        self._arm_display_recorder()

    def _arm_display_recorder(self) -> None:
        """Begin the whole-display recorder's timeline once a working page exists (two-phase capture,
        SKY-15466). Exactly-once and ownership-neutral in the recorder; a failed arm is surfaced, never raised."""
        recorder = self.browser_artifacts._display_recorder if self.browser_artifacts else None
        if isinstance(recorder, DisplayRecorder) and not recorder.arm_capture():
            LOG.warning(
                "Whole-display recorder arm signal was not delivered; run may record no frames",
                display_recording_owner_id=recorder.owner_id,
            )

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
        landed_url: str | None = None

        async def goto(strategy: str) -> object:
            nonlocal landed_url
            response = await page.goto(url, timeout=settings.BROWSER_LOADING_TIMEOUT_MS, wait_until=strategy)
            # Read off the SAME response the status is read from (duck-typed like _navigation_status:
            # whichever engine's response object this is, its url is the last redirect hop's). The settle
            # wait inside navigate_with_retry and the challenge-solver wait below both give a client-side
            # redirect time to move page.url off the URL the status belongs to, so a later read of the
            # page would pair the status with a page the status was never about (SKY-16271).
            response_url = getattr(response, "url", None)
            landed_url = response_url if isinstance(response_url, str) else None
            return response

        self.last_navigation_status = await navigate_with_retry(
            navigate=goto,
            url=url,
            retry_times=retry_times,
            settle=self._wait_for_settle,
            wait_until=wait_until,
        )
        self.last_navigation_url = landed_url
        await self._wait_for_challenge_solver(page=page)

    async def _wait_for_challenge_solver(self, page: Page) -> None:
        await app.AGENT_FUNCTION.wait_for_challenge_solver(page=page)

    async def get_working_page(self, *, prune_excess_pages: bool = True) -> Page | None:
        await self._working_page_ready.wait()
        if self.__page is None or self.browser_context is None:
            return None

        # A caller reading the selection on someone else's behalf passes False so the read itself
        # never closes a tab; max_pages <= 0 lists without pruning, as set_active_page's opt-out does.
        pages = await self.list_valid_pages(settings.BROWSER_MAX_PAGES_NUMBER if prune_excess_pages else 0)
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

        # Oldest first, skipping the selected tab: a code block can switch to an older tab, and
        # closing it here would leave get_working_page resuming on the newest one. Skipping it costs
        # the next-oldest page instead, so the list still comes back within the cap.
        active_page = self.__active_page
        excess = len(pages) - max_pages
        closing_pages: list[Page] = []
        for page in pages:
            if len(closing_pages) == excess:
                break
            if page is not active_page:
                closing_pages.append(page)
        closing_ids = {id(page) for page in closing_pages}
        reserved_pages = [page for page in pages if id(page) not in closing_ids]
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

    async def set_active_page(self, page: Page, *, prune_excess_pages: bool = True) -> None:
        self.__active_page = page
        self.__page = page
        # list_valid_pages closes the oldest tabs past the cap, and the snapshot below only needs to
        # know which pages existed. A caller that is pinning a tab on someone else's behalf passes
        # False so pinning never closes a tab; max_pages <= 0 lists without pruning.
        max_pages = settings.BROWSER_MAX_PAGES_NUMBER if prune_excess_pages else 0
        self.__active_page_known_pages = set(await self.list_valid_pages(max_pages))

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
                browser_session_id=browser_session_id,
            )
        except Exception as e:
            # Recreating the context draws a different proxy node, and no proxy node can invent an
            # address record for a host that has none.
            if isinstance(e, UnresolvableNavigationHost):
                raise
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
                browser_session_id=browser_session_id,
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
                browser_session_id=browser_session_id,
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
        future = driver_transport_error_future(self.pw)
        if future is not None and context not in self._driver_listener_contexts:
            state_ref, context_ref = weakref.ref(self), weakref.ref(context)
            driver = self.pw

            def observe_transport_loss(finished: asyncio.Future[None]) -> None:
                if finished.cancelled() or finished.exception() is None:
                    return
                state, observed_context = state_ref(), context_ref()
                if state is not None and observed_context is not None:
                    state._on_driver_transport_lost(driver, observed_context)

            future.add_done_callback(observe_transport_loss)
            self._driver_listener_contexts.add(context)
        try:
            browser = context.browser
        except Exception:
            LOG.debug("Failed to read browser for disconnect listener registration", exc_info=True)
            return
        if browser is None:
            return
        try:
            if browser is not self._disconnect_listener_browser:
                browser.on("disconnected", self._on_browser_disconnected)
            self._disconnect_listener_browser = browser
            self._disconnect_listener_browser_context = context
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
        if page not in self._crashed_pages and page.context is self.browser_context:
            log_browser_runtime_event(
                LOG,
                "Page crashed; closing the crashed tab so a replacement can be opened",
                context=self._runtime_event_context,
                event="page_crash",
                reason="page_crash",
                observation_source="page_event",
                expected=False,
                run_phase=self._run_phase,
            )
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
        self._crashed_pages.add(page)
        context = self.browser_context
        if context is None or any(other is not page and other not in self._crashed_pages for other in context.pages):
            await self._close_crashed_target(page)
            return
        # A headed Chromium on Linux exits when its last tab closes, and a persistent-session pod runs
        # exactly that: closing a crashed only-tab took the whole browser and its CDP endpoint with it,
        # leaving the pod answering 502 until its timeout. Reopen first so the browser always keeps a
        # window; the recovery closes the crashed target the moment a replacement exists, whichever
        # caller opened it. If none can be opened, leave the crashed tab: it is already excluded from
        # selection, and a stranded tab is recoverable where a dead browser is not.
        if await self._reopen_lost_working_page() is None:
            LOG.warning(
                "Could not open a replacement for the crashed last tab; leaving it open so the browser stays alive",
                url=page.url,
            )

    async def _close_crashed_targets(self) -> None:
        for page in list(self._crashed_pages):
            await self._close_crashed_target(page)

    async def _close_crashed_target(self, page: Page) -> None:
        if page in self._crashed_targets_closing:
            return
        self._crashed_targets_closing.add(page)
        # The crash event fires once. Excluding the page from selection keeps this process healthy, but
        # only closing the target unblocks later CDP attaches, so a hung close is retried up to
        # CRASHED_PAGE_CLOSE_ATTEMPTS before the target is reported stranded. Bounded by racing the
        # close rather than by asyncio.timeout, for the reason _run_bounded_detachable already
        # documents: a Playwright close can ignore the cancel a timeout delivers, and that would look
        # like success here and skip the retry. A close that raises has finished, so it is not retried.
        closing: asyncio.Task[None] | None = None
        try:
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
        except asyncio.CancelledError:
            # The owner (a cancelled caller's recovery) leaves mid-close: hand the attempt off and unmark
            # the target so the next reaper or recovery retries instead of skipping a close nobody drives.
            if closing is not None and not closing.done():
                closing.cancel()
                self._own_detached_task(closing, "close_crashed_page_attempt")
            self._crashed_targets_closing.discard(page)
            raise

    def _on_browser_context_closed(self, context: BrowserContext) -> None:
        if context is not self.browser_context:
            if self._reconnect_stale_observer is not None:
                self._reconnect_stale_observer(context, "context")
            return
        self._sessionless_init_script_registrations = []
        self._record_disconnect(
            "browser_context_close_event",
            event="browser_context_close",
            observation_source="browser_event",
        )

    def _on_browser_disconnected(self, browser: Browser) -> None:
        context = self.browser_context
        current_generation = False
        if browser is self._disconnect_listener_browser and context is not None:
            try:
                current_generation = context.browser is browser
            except Exception:
                # The registered event remains authoritative when Playwright's property fails during disconnect.
                current_generation = context is self._disconnect_listener_browser_context
        if not current_generation:
            if self._reconnect_stale_observer is not None:
                self._reconnect_stale_observer(browser, "browser")
            return
        self._record_disconnect(
            "browser_disconnected_event",
            event="browser_disconnected",
            observation_source="browser_event",
        )

    def _on_driver_transport_lost(self, driver: Playwright, context: BrowserContext) -> None:
        if driver is not self.pw or context is not self.browser_context:
            if self._reconnect_stale_observer is not None:
                self._reconnect_stale_observer(driver, "driver")
            return
        self._record_disconnect("playwright_driver_transport_lost", observation_source="driver_event")

    def _observe_current_driver_loss(self) -> None:
        future = driver_transport_error_future(self.pw)
        if (
            self.browser_context is not None
            and future is not None
            and future.done()
            and not future.cancelled()
            and future.exception() is not None
        ):
            self._on_driver_transport_lost(self.pw, self.browser_context)

    def record_connection_probe_failure(
        self, context: BrowserContext | None, driver: Playwright, *, timed_out: bool = False
    ) -> None:
        if context is not self.browser_context or driver is not self.pw:
            return
        self._record_disconnect(
            "browser_round_trip_timeout" if timed_out else "browser_round_trip_failed",
            disconnect_evidence="round_trip_timeout" if timed_out else "round_trip_failure",
        )

    def _classify_disconnect(
        self, reason: str, event: str, evidence: DisconnectEvidence, expected: bool, driver: Playwright
    ) -> tuple[DisconnectKind, DisconnectEvidence]:
        if expected:
            return "intentional_teardown", "teardown_intent"
        future = driver_transport_error_future(driver)
        if future is not None and future.done() and not future.cancelled() and future.exception() is not None:
            return "driver_transport_loss", "transport_error_future"
        if event == "browser_disconnected":
            return "browser_disconnected", "browser_event"
        if event == "browser_context_close":
            return "context_closed", "context_event"
        if reason == "browser_context_disconnected":
            return "browser_disconnected", evidence
        if reason in {"browser_context_close_called", "browser_context_closed"}:
            return "context_closed", evidence
        return "connection_unusable", evidence

    def _record_disconnect(
        self,
        reason: str,
        *,
        event: str = "browser_context_disconnected",
        observation_source: Literal["liveness_probe", "browser_event", "driver_event"] = "liveness_probe",
        disconnect_evidence: DisconnectEvidence = "liveness_state",
    ) -> None:
        if self._browser_state_diagnostic is not None or not self._ever_connected:
            return
        pending = self._deferred_runtime_end
        if (
            self._runtime_events_deferred
            and pending is not None
            and pending.context is self.browser_context
            and pending.driver is self.pw
        ):
            return
        observation = self._make_runtime_end_observation(
            reason,
            context=self.browser_context,
            driver=self.pw,
            browser_session_id=self.browser_artifacts.remote_browser_session_id if self.browser_artifacts else None,
            close_requested=self._close_requested,
            event=event,
            observation_source=observation_source,
            disconnect_evidence=disconnect_evidence,
        )
        if self._runtime_events_deferred:
            self._deferred_runtime_end = observation
            return
        self._publish_runtime_end(observation)

    def _make_runtime_end_observation(
        self,
        reason: str,
        *,
        context: BrowserContext | None,
        driver: Playwright,
        browser_session_id: str | None,
        close_requested: bool,
        event: str = "browser_context_disconnected",
        observation_source: Literal["liveness_probe", "browser_event", "driver_event"] = "liveness_probe",
        disconnect_evidence: DisconnectEvidence = "liveness_state",
    ) -> _RuntimeEndObservation:
        disconnect_observed_at = datetime.now(UTC)
        diagnostic = BrowserStateDiagnostic(
            reason=reason,
            disconnect_observed_at=disconnect_observed_at,
            browser_session_id=browser_session_id,
            event=event,
            observation_source=observation_source,
        )
        expected_reason = self._expected_runtime_ends.get(context) if context else None
        runtime_reason: RuntimeEventReason
        if expected_reason is not None:
            runtime_reason = expected_reason
        elif close_requested:
            runtime_reason = "normal_close"
        elif event == "browser_context_close":
            runtime_reason = "context_closed"
        elif event == "browser_disconnected":
            runtime_reason = "browser_disconnected"
        else:
            runtime_reason = "connection_unusable"
        expected = expected_reason is not None or close_requested
        disconnect_kind, disconnect_evidence = self._classify_disconnect(
            reason, event, disconnect_evidence, expected, driver
        )
        return _RuntimeEndObservation(
            context=context,
            driver=driver,
            diagnostic=diagnostic,
            reason=runtime_reason,
            source=observation_source,
            expected=expected,
            kind=disconnect_kind,
            evidence=disconnect_evidence,
            close_requested=close_requested,
            run_phase=self._run_phase,
        )

    def _replay_deferred_runtime_end(self) -> None:
        if self._runtime_events_deferred:
            return
        retired = self._deferred_retired_runtime_ends
        self._deferred_retired_runtime_ends = []
        for observation, context in retired:
            self._log_runtime_end(observation, context)
        pending = self._deferred_runtime_end
        self._deferred_runtime_end = None
        if pending is not None:
            self._publish_runtime_end(pending)

    def _record_retired_runtime_end(
        self, observation: _RuntimeEndObservation, context: BrowserRuntimeLogContext
    ) -> None:
        # A retired generation keeps its owner but must never latch a diagnostic on the replacement.
        if self._runtime_events_deferred:
            self._deferred_retired_runtime_ends.append((observation, context))
        else:
            self._log_runtime_end(observation, context)

    def _publish_runtime_end(self, observation: _RuntimeEndObservation) -> None:
        if (
            self._browser_state_diagnostic is not None
            or observation.context is not self.browser_context
            or observation.driver is not self.pw
        ):
            return
        self._browser_state_diagnostic = observation.diagnostic
        self._log_runtime_end(observation, self._runtime_event_context)

    def _log_runtime_end(self, observation: _RuntimeEndObservation, context: BrowserRuntimeLogContext) -> None:
        diagnostic = observation.diagnostic
        log_browser_runtime_event(
            LOG,
            "Browser state disconnected",
            context=context,
            event="runtime_ended",
            reason=observation.reason,
            observation_source=observation.source,
            expected=observation.expected,
            run_phase=observation.run_phase,
            disconnect_kind=observation.kind,
            disconnect_evidence=observation.evidence,
            disconnect_reason=diagnostic.reason,
            disconnect_event=diagnostic.event,
            disconnect_observed_at=diagnostic.disconnect_observed_at.isoformat(),
            disconnect_observation_source=observation.source,
            close_requested=observation.close_requested,
            remote_browser_session_id=diagnostic.browser_session_id,
        )

    def get_browser_state_diagnostic(self) -> BrowserStateDiagnostic | None:
        return self._browser_state_diagnostic

    def _connection_status(self) -> tuple[bool, str | None]:
        stopped = browser_context_stopped_reason(self.browser_context)
        return stopped is None, stopped

    def is_connected(self) -> bool:
        connected, reason = self._connection_status()
        if not connected and reason is not None:
            self._record_disconnect(reason)
        return connected

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
    ) -> None:
        # Rebuild through a fresh Playwright driver only after the old connection is known dead or
        # an explicitly unusable connection has completed bounded shutdown.
        stale_pw = self.pw
        stale_context = self.browser_context
        stale_page = self.__page
        stale_active_page = self.__active_page
        stale_active_page_known_pages = self.__active_page_known_pages.copy()
        stale_browser_artifacts = self.browser_artifacts
        stale_browser_cleanup = self.browser_cleanup
        stale_route_policy_url = self.browser_context_route_policy_url
        stale_sessionless_init_script_registrations = self._sessionless_init_script_registrations.copy()
        stale_disconnect_listener_browser = self._disconnect_listener_browser
        stale_disconnect_listener_browser_context = self._disconnect_listener_browser_context
        stale_diagnostic = self._browser_state_diagnostic
        stale_ever_connected = self._ever_connected
        stale_close_requested = self._close_requested
        stale_runtime_events_deferred = self._runtime_events_deferred
        stale_deferred_runtime_end = self._deferred_runtime_end
        stale_runtime_event_context = self._runtime_event_context

        def clear_stale_replacement_intent() -> None:
            if stale_context is not None and self._expected_runtime_ends.get(stale_context) == "driver_replacement":
                self._expected_runtime_ends.pop(stale_context, None)

        # check_and_fix_state rebuilds through the factory; forward this session's download binding so the
        # creator seam preserves the provider-selected destination on reconnect. The binding is carried
        # forward, never overridden after the fact, so a genuine provider change is not mislabeled.
        prior_download_binding = (
            self.browser_artifacts.download_binding if self.browser_artifacts else DownloadBinding.RUN_DIR
        )
        # Preserve the active whole-display recorder's owner across the rebuild, derived from THIS state's own
        # recorder (never a caller id), so a standalone task/script reconnect re-acquires the SAME recorder
        # instead of a fresh Playwright recording. A different run cannot inherit it (id is this run's own).
        recorder = self.browser_artifacts._display_recorder if self.browser_artifacts else None
        display_recording_owner_id = recorder.owner_id if isinstance(recorder, DisplayRecorder) else None
        effective_route_policy_url = browser_context_route_policy_url or self.browser_context_route_policy_url
        _stale_driver_connected, stale_driver_disconnect_reason = self._connection_status()
        stale_driver_connection_probe_failed = isinstance(stale_driver_disconnect_reason, _BrowserConnectionProbeFailed)
        stale_driver_is_known_disconnected = stale_driver_disconnect_reason in {
            "playwright_driver_connection_closed",
            "browser_context_disconnected",
        }
        if stale_driver_is_known_disconnected and stale_driver_disconnect_reason is not None:
            self._record_disconnect(stale_driver_disconnect_reason)
        stale_context_is_known_unusable = (
            stale_driver_disconnect_reason
            in {
                "browser_context_missing",
                "browser_context_close_called",
                "browser_context_closed",
            }
            or stale_driver_connection_probe_failed
        )
        stale_driver_may_be_live = not stale_driver_is_known_disconnected
        if stale_driver_may_be_live and not (stale_context_is_unusable or stale_context_is_known_unusable):
            raise RuntimeError("Cannot replace a Playwright driver while its connection may still be live")
        if has_download_interceptor_for_context(stale_context):
            if not await self._run_bounded_detachable(
                disable_download_interceptor_for_context(stale_context),
                BROWSER_INTERCEPTOR_DISABLE_TIMEOUT,
                "stale download interceptor disable before reconnect",
            ):
                raise RuntimeError("Failed to retire stale browser download interceptor")
        retain_guard_until_replacement = (
            stale_driver_may_be_live
            and (stale_context_is_unusable or stale_driver_connection_probe_failed)
            and await app.AGENT_FUNCTION.has_retained_browser_egress_guard(stale_context)
        )
        stop_stale_before_replacement = stale_driver_may_be_live and not retain_guard_until_replacement
        retry_stale_shutdown_after_replacement = False
        if stop_stale_before_replacement:
            stale_driver_pre_shutdown_error: BaseException | None = None

            async def stop_stale_driver_before_replacement() -> None:
                nonlocal stale_driver_pre_shutdown_error
                try:
                    self._expect_runtime_end("driver_replacement", stale_context)
                    await stale_pw.stop()
                except BaseException as exc:
                    stale_driver_pre_shutdown_error = exc
                    clear_stale_replacement_intent()
                    raise
                finally:
                    # A cancellation-resistant stop may deliver the close event after
                    # reconnect has already propagated cancellation to its caller.
                    self._sessionless_init_script_registrations = stale_sessionless_init_script_registrations.copy()

            try:
                stale_driver_stopped = await self._run_bounded_detachable(
                    stop_stale_driver_before_replacement(),
                    BROWSER_CLOSE_TIMEOUT,
                    "unusable stale Playwright driver shutdown before reconnect",
                    accept_failure_as_completion=True,
                )
            except BaseException:
                clear_stale_replacement_intent()
                raise
            finally:
                # The stop can synchronously deliver this context's close event before it
                # times out, fails, or observes caller cancellation. Preserve the snapshot
                # for this wrapper's next reconnect even when no replacement is started.
                self._sessionless_init_script_registrations = stale_sessionless_init_script_registrations.copy()
            if stale_driver_pre_shutdown_error is not None:
                retry_stale_shutdown_after_replacement = True
            elif not stale_driver_stopped:
                clear_stale_replacement_intent()
                raise RuntimeError("Failed to stop unusable stale Playwright driver before reconnect")

        def restore_stale_state() -> None:
            self._reconnect_stale_observer = None
            clear_stale_replacement_intent()
            self.pw = stale_pw
            self.browser_context = stale_context
            self.__page = stale_page
            self.__active_page = stale_active_page
            self.__active_page_known_pages = stale_active_page_known_pages
            self.browser_artifacts = stale_browser_artifacts
            self.browser_cleanup = stale_browser_cleanup
            self.browser_context_route_policy_url = stale_route_policy_url
            self._sessionless_init_script_registrations = stale_sessionless_init_script_registrations
            self._disconnect_listener_browser = stale_disconnect_listener_browser
            self._disconnect_listener_browser_context = stale_disconnect_listener_browser_context
            self._browser_state_diagnostic = stale_diagnostic
            self._ever_connected = stale_ever_connected
            self._close_requested = stale_close_requested
            self._runtime_events_deferred = stale_runtime_events_deferred
            self._deferred_runtime_end = stale_deferred_runtime_end
            self._replay_deferred_runtime_end()
            self._observe_current_driver_loss()
            connected, reason = self._connection_status()
            if not connected and reason is not None:
                self._record_disconnect(reason)

        def observe_stale_generation(
            observed: BrowserContext | Browser | Playwright, signal: Literal["context", "browser", "driver", "liveness"]
        ) -> None:
            nonlocal stale_deferred_runtime_end
            if stale_diagnostic is not None or stale_deferred_runtime_end is not None or not stale_ever_connected:
                return
            source: Literal["liveness_probe", "browser_event", "driver_event"] = "browser_event"
            if signal == "context" and observed is stale_context:
                reason, event = "browser_context_close_event", "browser_context_close"
            elif signal == "browser" and observed is stale_disconnect_listener_browser:
                reason, event = "browser_disconnected_event", "browser_disconnected"
            elif signal == "driver" and observed is stale_pw:
                reason, event = "playwright_driver_transport_lost", "browser_context_disconnected"
                source = "driver_event"
            elif signal == "liveness" and observed is stale_context:
                stopped_reason = browser_context_stopped_reason(stale_context)
                if stopped_reason is None:
                    return
                reason, event = stopped_reason, "browser_context_disconnected"
                source = "liveness_probe"
            else:
                return
            stale_deferred_runtime_end = self._make_runtime_end_observation(
                reason,
                context=stale_context,
                driver=stale_pw,
                browser_session_id=stale_browser_artifacts.remote_browser_session_id
                if stale_browser_artifacts
                else None,
                close_requested=stale_close_requested,
                event=event,
                observation_source=source,
            )

        try:
            if self.engine_selection is not None:
                fresh_pw = await self.engine_selection.start_driver()
            else:
                fresh_pw = await async_playwright().start()
                close_driver_connection_on_transport_loss(fresh_pw)
        except BaseException:
            stale_diagnostic = self._browser_state_diagnostic
            stale_deferred_runtime_end = self._deferred_runtime_end
            restore_stale_state()
            raise

        # Reconciliation installs the replacement guard before replaying idempotent registrations.
        # A retained guard remains attached until its replacement is ready so an unusable context
        # cannot expose scripts during recovery. Unguarded unusable drivers are stopped above.
        stale_diagnostic = self._browser_state_diagnostic
        stale_deferred_runtime_end = self._deferred_runtime_end
        self._runtime_events_deferred = True
        self._deferred_runtime_end = None
        self._reconnect_stale_observer = observe_stale_generation
        self.pw = fresh_pw
        self.browser_context = None
        try:
            await self.set_working_page(None)
            await self.check_and_fix_state(
                proxy_location=proxy_location,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                browser_context_route_policy_url=effective_route_policy_url,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
                browser_session_id=browser_session_id,
                download_binding=prior_download_binding,
                display_recording_owner_id=display_recording_owner_id,
                reconcile_persistent_init_scripts=True,
                sessionless_init_script_registrations=tuple(stale_sessionless_init_script_registrations),
            )
        except BaseException:
            # Restore the old wrapper before any await so cancellation during replacement cleanup
            # cannot leave the partially initialized replacement published on this state.
            restore_stale_state()
            await self._run_bounded_detachable(
                fresh_pw.stop(), BROWSER_CLOSE_TIMEOUT, "new Playwright driver shutdown after failed reconnect"
            )
            raise

        def observe_stale_loss() -> None:
            stale_transport_error = driver_transport_error_future(stale_pw)
            if (
                stale_transport_error is not None
                and stale_transport_error.done()
                and not stale_transport_error.cancelled()
                and stale_transport_error.exception() is not None
            ):
                observe_stale_generation(stale_pw, "driver")
            elif stale_context is not None:
                observe_stale_generation(stale_context, "liveness")

        # Recheck both at handoff and when the scheduled stop starts; either can precede queued callbacks.
        observe_stale_loss()
        self._reconnect_stale_observer = None
        self._runtime_events_deferred = stale_runtime_events_deferred
        if stale_deferred_runtime_end is not None:
            self._record_retired_runtime_end(stale_deferred_runtime_end, stale_runtime_event_context)
        self._replay_deferred_runtime_end()
        self._observe_current_driver_loss()
        # Pre-handoff shutdown can synchronously deliver the stale context's close event while it
        # is still this state's current context, clearing the live list. The replacement replayed
        # the snapshot above, so retain that same durable state for later reconnects.
        self._sessionless_init_script_registrations = stale_sessionless_init_script_registrations.copy()
        if stop_stale_before_replacement and not retry_stale_shutdown_after_replacement:
            return

        stale_runtime_end_reported = stale_diagnostic is not None or stale_deferred_runtime_end is not None

        async def stop_stale_driver() -> None:
            nonlocal stale_runtime_end_reported
            observe_stale_loss()
            if stale_deferred_runtime_end is not None and not stale_runtime_end_reported:
                stale_runtime_end_reported = True
                self._record_retired_runtime_end(stale_deferred_runtime_end, stale_runtime_event_context)
            self._expect_runtime_end("driver_replacement", stale_context)
            try:
                await stale_pw.stop()
                if (
                    stale_context is not None
                    and stale_driver_may_be_live
                    and stale_ever_connected
                    and not stale_runtime_end_reported
                ):
                    stale_runtime_end_reported = True
                    # The replacement owns the listeners now; successful stop is terminal evidence
                    # for the retired generation, never a diagnostic on the replacement.
                    self._record_retired_runtime_end(
                        self._make_runtime_end_observation(
                            "playwright_driver_stopped",
                            context=stale_context,
                            driver=stale_pw,
                            browser_session_id=(
                                stale_browser_artifacts.remote_browser_session_id if stale_browser_artifacts else None
                            ),
                            close_requested=stale_close_requested,
                        ),
                        stale_runtime_event_context,
                    )
            finally:
                clear_stale_replacement_intent()

        stale_driver_shutdown_retry_scheduled = False

        def schedule_stale_driver_shutdown_retry() -> None:
            nonlocal stale_driver_shutdown_retry_scheduled
            if stale_driver_shutdown_retry_scheduled:
                return
            stale_driver_shutdown_retry_scheduled = True
            LOG.warning("Scheduling stale Playwright driver shutdown retry after reconnect")

            async def retry_stale_driver_shutdown() -> None:
                await self._run_bounded_detachable(
                    stop_stale_driver(),
                    BROWSER_CLOSE_TIMEOUT,
                    "retry stale Playwright driver shutdown after reconnect",
                    accept_failure_as_completion=True,
                )

            retry_task = asyncio.create_task(retry_stale_driver_shutdown())
            self._own_detached_task(retry_task, "retry stale Playwright driver shutdown after reconnect")

        def retry_failed_stale_driver_shutdown(finished: asyncio.Task[None]) -> None:
            if finished.cancelled() or finished.exception() is not None:
                # Scheduling from completion serializes the calls even when the first stop ignores
                # cancellation, while a late successful completion needs no retry.
                schedule_stale_driver_shutdown_retry()

        stale_driver_shutdown_task = asyncio.create_task(stop_stale_driver())
        stale_driver_shutdown_task.add_done_callback(retry_failed_stale_driver_shutdown)
        await self._run_bounded_detachable(
            stale_driver_shutdown_task,
            BROWSER_CLOSE_TIMEOUT,
            "stale Playwright driver shutdown after guarded reconnect",
            # The replacement is already authoritative. The done callback owns any needed
            # retry, so a stale-driver stop failure must not turn the successful handoff into
            # a reconnect failure.
            accept_failure_as_completion=True,
        )

    async def close_current_open_page(self) -> bool:
        context = None
        try:
            async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                await self._close_all_other_pages()
                if self.browser_context is not None:
                    context = self.browser_context
                    self._expect_runtime_end("context_recreation", context)
                    await context.close()
                self._sessionless_init_script_registrations = []
                self.browser_context = None
                await self.set_working_page(None)
                return True
        except Exception:
            LOG.warning("Error while closing the current open page", exc_info=True)
            return False
        finally:
            if context is not None and self.browser_context is context:
                self._expected_runtime_ends.pop(context, None)

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
            context = self.browser_context
            self._expect_runtime_end("normal_close", context)
            self._close_requested = True
            teardown_active = False
            teardown_settled = False
            close_phases_finished = False

            def clear_unfinished_close_intent() -> None:
                if (
                    close_phases_finished
                    and context is not None
                    and self.browser_context is context
                    and self._browser_state_diagnostic is None
                    and getattr(getattr(context, "_impl_obj", None), "_closed", False) is not True
                    and self._expected_runtime_ends.get(context) == "normal_close"
                ):
                    self._expected_runtime_ends.pop(context, None)
                    self._close_requested = False

            async def teardown_context() -> None:
                nonlocal teardown_active, teardown_settled
                teardown_active = True
                try:
                    await self._teardown_context()
                finally:
                    teardown_active = False
                    teardown_settled = True
                    clear_unfinished_close_intent()

            try:
                recording_finalized = await self._run_bounded_detachable(
                    teardown_context(),
                    BROWSER_CLOSE_TIMEOUT,
                    "browser context teardown",
                )
                # The display recorder is stopped/finalized on its own, decoupled from context teardown:
                # ``recording_finalized`` reports only that the browser context closed, which is what gates
                # profile persistence. A recorder that exited non-zero mid-run (or needed a forced kill)
                # must not clear that flag and suppress an otherwise-clean run's profile write-back, and a
                # context-teardown failure must not stop us from finalizing the WebM for upload. The stop
                # runs even when teardown failed above, so the recording is always finalized best-effort.
                recorder = self.browser_artifacts._display_recorder if self.browser_artifacts else None
                if isinstance(recorder, DisplayRecorder):
                    await self._run_bounded_detachable(
                        self._stop_display_recorder(recorder),
                        BROWSER_CLOSE_TIMEOUT,
                        "whole-display recorder stop",
                    )
                await self._run_browser_cleanup_bounded()
            finally:
                close_phases_finished = True
                if not teardown_active and (teardown_settled or not recording_finalized):
                    clear_unfinished_close_intent()

        await self._stop_driver_bounded(release_driver)
        return recording_finalized

    async def _stop_display_recorder(self, recorder: DisplayRecorder) -> None:
        if not await release_display_recorder(recorder):
            raise RuntimeError("Whole-display recorder required forced termination")

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
            context = self.browser_context
            try:
                async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                    self._expect_runtime_end("deliberate_detach", context)
                    await self.pw.stop()
            except BaseException:
                if context is not None:
                    self._expected_runtime_ends.pop(context, None)
                raise
        self._remote_driver_detached = True

    async def _run_bounded_detachable(
        self,
        coro: Awaitable[None],
        timeout: float,
        description: str,
        *,
        accept_failure_as_completion: bool = False,
    ) -> bool:
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
            return accept_failure_as_completion
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
        self._sessionless_init_script_registrations = []
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
                    context = self.browser_context if not self._close_requested else None
                    try:
                        self._expect_runtime_end("driver_release", context)
                        LOG.info("Stopping playwright")
                        try:
                            await self.pw.stop()
                        except BaseException:
                            if context is not None and self.browser_context is context:
                                self._expected_runtime_ends.pop(context, None)
                            raise
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
            runtime_context=self._runtime_event_context,
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
            runtime_context=self._runtime_event_context,
        )
