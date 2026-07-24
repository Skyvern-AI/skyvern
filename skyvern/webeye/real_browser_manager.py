from __future__ import annotations

import asyncio
import functools
import os
from dataclasses import replace

import structlog

from skyvern.config import settings
from skyvern.constants import BROWSER_CLOSE_TIMEOUT
from skyvern.exceptions import (
    FailedToNavigateToUrl,
    MissingBrowserState,
    MissingBrowserStateForBrowserSession,
    MissingOrganizationForBrowserSession,
)
from skyvern.forge import app
from skyvern.forge.sdk.api.files import resolve_run_download_id
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.routes.streaming.registries import set_deferred_close_params, stream_ref_active
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_artifacts import VideoArtifact
from skyvern.webeye.browser_engine import (
    BrowserEngineContext,
    BrowserEngineSelection,
    resolve_browser_engine,
)
from skyvern.webeye.browser_factory import BrowserContextFactory, rebind_download_dir
from skyvern.webeye.browser_manager import BrowserManager
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.cdp_frame_publisher import (
    CDPFramePublisher,
    stream_key_for_task,
    stream_key_for_workflow_run,
)
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.session_cookies import persist_session_cookies
from skyvern.webeye.video_utils import finalize_webm

LOG = structlog.get_logger()

# Only driver/transport-level CDP drops trigger the cached-PBS evict + reconnect path.
# Playwright also surfaces page/context-only closes ("Target page, context or browser
# has been closed") with text that overlaps a transport drop; treating those as cached
# CDP drops would tear down a healthy PBS over a recoverable page-level state.
_CACHED_CDP_DROP_ERROR_SUBSTRINGS = ("Connection closed while reading from the driver",)


def _is_cached_cdp_drop_error(exc: FailedToNavigateToUrl) -> bool:
    message = exc.error_message or ""
    return any(needle in message for needle in _CACHED_CDP_DROP_ERROR_SUBSTRINGS)


async def _rebind_pbs_download_dir(
    browser_state: BrowserState,
    workflow_run: WorkflowRun,
    browser_session_id: str,
) -> None:
    browser_context = browser_state.browser_context
    adopted_browser = browser_context.browser if browser_context else None
    if adopted_browser is None:
        return
    try:
        rebind_run_id = resolve_run_download_id(skyvern_context.current(), fallback_run_id=workflow_run.workflow_run_id)
        await rebind_download_dir(adopted_browser, run_id=rebind_run_id)
    except Exception:
        LOG.warning(
            "Failed to rebind download dir on adopted browser session",
            browser_session_id=browser_session_id,
            workflow_run_id=workflow_run.workflow_run_id,
            exc_info=True,
        )


def _merge_proxy_session_headers(
    extra_http_headers: dict[str, str] | None,
    proxy_session_id: str | None,
) -> dict[str, str] | None:
    if not proxy_session_id:
        return extra_http_headers
    return app.AGENT_FUNCTION.merge_proxy_session_extra_http_headers(extra_http_headers, proxy_session_id)


def _resolve_stream_key(*, workflow_run_id: str | None, task_id: str | None) -> str | None:
    """Pick the stream key that the API-side WebSocket polls for this entity.

    Workflow-run streams always read ``{workflow_run_id}.png``; task streams use
    that same key when the task belongs to a workflow run, and fall back to
    ``{task_id}.png`` only for standalone tasks. See
    ``skyvern/forge/sdk/routes/streaming/screenshot.py``.
    """
    if workflow_run_id:
        return stream_key_for_workflow_run(workflow_run_id)
    if task_id:
        return stream_key_for_task(task_id)
    return None


def canonical_run_key(
    *,
    workflow_run_id: str | None = None,
    task_id: str | None = None,
    script_id: str | None = None,
) -> str | None:
    """The one stable key a logical run's engine selection is pinned under. ``workflow_run_id`` wins
    so a workflow-owned task and its workflow share a single selection owner (never two). Returns
    None when the run has no durable identity (e.g. a standalone script with no id), in which case
    the resource is ephemeral and its engine is not pinned/cached."""
    return workflow_run_id or task_id or script_id


class _EngineSelectionOwner:
    """Per-run single-flight owner of the pinned engine selection.

    The resolution runs inside a shared ``asyncio.Task``: concurrent first acquisitions for one run
    await the same task and receive the same frozen selection. Waiters await it through
    ``asyncio.shield`` (see ``get_or_resolve_engine_selection``); the shield is what keeps one waiter's
    cancellation from aborting the shared resolution — awaiting a task WITHOUT shielding would propagate
    the waiter's cancellation to it. The resolved value lives on THIS owner object, not a bare per-key
    dict, so a resolver whose owner was already dropped by terminal cleanup cannot resurrect the run's
    selection — its result is simply unreferenced. ``terminal`` is set by ``_drop_engine_owner`` before it
    cancels the resolver: it marks the owner as being torn down so a same-key acquisition waits it out
    instead of starting a second resolver, and so the done-callback evicts it whatever the outcome.
    """

    __slots__ = ("task", "terminal")

    def __init__(self, task: asyncio.Task[BrowserEngineSelection]) -> None:
        self.task = task
        self.terminal = False


class RealBrowserManager(BrowserManager):
    def __init__(self) -> None:
        self.pages: dict[str, BrowserState] = {}
        # Engine pinned per logical run, keyed by run id (workflow_run_id / task_id / script_id) via a
        # per-key single-flight owner. Resolved once at the first browser-resource creation for a run
        # and reused for every later resource/recreation in that run, so recreation can never
        # re-resolve to a different engine (e.g. after a flag change). Dropped — with its in-flight
        # resolution cancelled — when the run's browser state is cleaned up.
        self._engine_owners: dict[str, _EngineSelectionOwner] = {}
        # CDP frame publishers keyed by stream key (``{wr}.png`` / ``{task}.png``).
        self._frame_publishers: dict[str, CDPFramePublisher] = {}
        # Serializes the check/create/start/store/register sequence in
        # ``_start_frame_publisher`` so concurrent attaches for one stream key
        # cannot orphan a publisher loop.
        self._publisher_lock = asyncio.Lock()

    async def _start_frame_publisher(
        self,
        *,
        browser_state: BrowserState,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        """Best-effort start a CDP frame publisher for this entity.

        Gated on ``browser_state.browser_artifacts.needs_cdp_frame_publisher``,
        which remote-CDP creators stamp. Local Playwright contexts leave it
        False and skip publishing. Never raises.
        """
        # Strict equality; MagicMock attributes are truthy by default.
        if browser_state.browser_artifacts.needs_cdp_frame_publisher is not True:
            return
        stream_key = _resolve_stream_key(workflow_run_id=workflow_run_id, task_id=task_id)
        if not stream_key or not organization_id:
            return
        async with self._publisher_lock:
            if stream_key in self._frame_publishers:
                return
            try:
                publisher = CDPFramePublisher(
                    browser_state=browser_state,
                    stream_key=stream_key,
                    organization_id=organization_id,
                )
                await publisher.start()
                self._frame_publishers[stream_key] = publisher
            except Exception:
                LOG.warning(
                    "Failed to start CDP frame publisher; livestream may be unavailable",
                    stream_key=stream_key,
                    organization_id=organization_id,
                    exc_info=True,
                )
                return
            # Tie publisher lifetime to BrowserState.close() so any close path
            # stops it without needing to know about the publisher registry.
            captured_stream_key = stream_key

            async def _on_browser_state_close() -> None:
                # Pop under the same lock that guards ``_start_frame_publisher``
                # so a concurrent restart cannot slip past the registry check
                # and orphan a second publisher. ``pub.stop()`` runs outside
                # the lock — it awaits the task's exit and must not block
                # other publishers from starting.
                async with self._publisher_lock:
                    pub = self._frame_publishers.pop(captured_stream_key, None)
                if pub is None:
                    return
                try:
                    await pub.stop()
                except Exception:
                    LOG.debug(
                        "CDP frame publisher stop raised during browser-state close; ignored",
                        stream_key=captured_stream_key,
                        exc_info=True,
                    )

            browser_state.add_on_close(_on_browser_state_close)

    async def _stop_frame_publisher(
        self,
        *,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Best-effort: stop the publisher matching this entity. Idempotent."""
        stream_key = _resolve_stream_key(workflow_run_id=workflow_run_id, task_id=task_id)
        if not stream_key:
            return
        publisher = self._frame_publishers.pop(stream_key, None)
        if publisher is None:
            return
        try:
            await publisher.stop()
        except Exception:
            LOG.debug(
                "CDP frame publisher stop raised; ignored",
                stream_key=stream_key,
                exc_info=True,
            )

    async def get_or_resolve_engine_selection(
        self,
        *,
        run_key: str | None,
        context: BrowserEngineContext,
    ) -> BrowserEngineSelection:
        """Single owner of a logical run's engine selection: resolve it once under ``run_key`` and
        reuse it for EVERY browser resource in the run — this manager's states and the persistent
        session attach alike — so all paths for one run share one pinned engine and recreation never
        re-resolves to a different one (e.g. after a flag flip). Resolution per key is single-flighted
        via a per-key owner task, so concurrent first acquisitions await one resolution and receive
        the same frozen selection while different keys resolve concurrently. ``run_key`` None means an
        ephemeral resource with no durable run identity: it is not pinned or cached (the resolver still
        fails closed on capability). The source-capability check runs on every return, including cache
        hits, so a capability-restricted run fails closed the moment it reaches an unsupported source.

        Failure-bounded: if the shared resolution finishes exceptionally (resolver raised, or terminal
        cleanup cancelled it) the owner is dropped so nothing is stored and a later acquisition
        re-resolves cleanly — no orphan owner accumulates on failed run keys."""
        if run_key is None:
            return await resolve_browser_engine(context)
        # Resolve the flag under the SAME key the owner is pinned by, so a run selects one engine no
        # matter which resource creates the browser first — a caller may pin under workflow_run_id while
        # deliberately leaving it out of the context (task-first creation keeps its download-dir scoping).
        if context.run_key != run_key:
            context = replace(context, run_key=run_key)
        while True:
            owner = self._engine_owners.get(run_key)
            if owner is None:
                # No await between the miss and the store, so concurrent first acquisitions for one key
                # observe the same owner and share its single resolution task (single-flight).
                owner = _EngineSelectionOwner(asyncio.ensure_future(resolve_browser_engine(context)))
                self._engine_owners[run_key] = owner
                # Reap the owner if its resolution ends with nothing selectable (failed/cancelled, or
                # marked dropping) and no live waiter, so it neither lingers nor leaks an exception.
                owner.task.add_done_callback(functools.partial(self._reap_failed_owner, run_key, owner))
                break
            if not owner.terminal:
                break
            # Terminal owner mid-teardown: wait it out, evict once it ends, then loop for a fresh owner.
            try:
                await asyncio.shield(owner.task)
            except asyncio.CancelledError:
                if (t := asyncio.current_task()) is not None and t.cancelling() > 0:
                    raise  # this acquirer itself was cancelled — leave the still-running owner untouched
            except Exception:
                pass
            if owner.task.done() and self._engine_owners.get(run_key) is owner:
                del self._engine_owners[run_key]
        # shield so cancelling THIS waiter cannot cancel the shared resolution task (asyncio otherwise
        # propagates it); only terminal cleanup cancels the task. Evicting a failed/cancelled/terminal owner
        # is the done-callback's job, so a waiter cancel never drops a healthy owner here (even after success).
        selection = await asyncio.shield(owner.task)
        selection.ensure_supports(context.browser_source)
        return selection

    def _reap_failed_owner(self, run_key: str, owner: _EngineSelectionOwner, task: asyncio.Task) -> None:
        """Done-callback for an owner's resolution task. Consumes the outcome so a failed/cancelled
        resolution never surfaces an unretrieved-exception warning, and evicts the owner when it should
        no longer be selectable — finished exceptionally/cancelled OR marked terminal by cleanup —
        provided it is still the current owner for ``run_key`` (``is owner`` guards a reused key). A
        successful, non-terminal selection is kept."""
        failed = task.cancelled() or task.exception() is not None
        if (failed or owner.terminal) and self._engine_owners.get(run_key) is owner:
            del self._engine_owners[run_key]

    async def _drop_engine_owner(self, run_key: str | None) -> None:
        """Terminally remove a run's pinned engine owner. Idempotent. Marks the owner terminal, cancels
        the in-flight resolution, and AWAITS its termination (keeping the owner registered as it unwinds)
        so no second same-key resolver starts until the first definitively ends — even one that
        suppresses/delays cancellation. If THIS cleanup coroutine is itself cancelled while the resolver
        still runs, the cancellation propagates and the terminal owner stays registered. Removes by id."""
        if run_key is None:
            return
        owner = self._engine_owners.get(run_key)
        if owner is None:
            return
        owner.terminal = True
        if not owner.task.done():
            owner.task.cancel()
        try:
            # shield keeps the resolver alive if this cleanup coroutine is cancelled mid-await. Propagate
            # only OUR cancellation (via the current task's cancellation count) — never the resolver
            # task's own cancellation surfacing through shield — so an external cancel is never swallowed
            # (even if the resolver finishes in the same tick); the owner stays for its done-callback.
            await asyncio.shield(owner.task)
        except asyncio.CancelledError:
            if (t := asyncio.current_task()) is not None and t.cancelling() > 0:
                raise
        except Exception:
            pass
        if self._engine_owners.get(run_key) is owner:
            del self._engine_owners[run_key]

    async def _create_browser_state(
        self,
        proxy_location: ProxyLocationInput = None,
        url: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_permanent_id: str | None = None,
        script_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        cdp_connect_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
        engine_run_key: str | None = None,
        engine_workflow_run_id: str | None = None,
    ) -> BrowserState:
        engine_selection = await self.get_or_resolve_engine_selection(
            run_key=engine_run_key
            or canonical_run_key(workflow_run_id=workflow_run_id, task_id=task_id, script_id=script_id),
            context=BrowserEngineContext(
                organization_id=organization_id,
                # Engine-flag identity only: a caller that pins under workflow_run_id while keeping it out
                # of browser-context creation (task-first, for download-dir scoping) passes it via
                # engine_workflow_run_id, so the flag's distinct_id AND its workflow_run_id property both
                # match the pinned run. The browser context below still uses the raw workflow_run_id.
                workflow_run_id=engine_workflow_run_id or workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                task_id=task_id,
                script_id=script_id,
                browser_source=settings.BROWSER_TYPE,
            ),
        )
        LOG.info(
            "Creating browser state",
            task_id=task_id,
            workflow_run_id=workflow_run_id,
            browser_source=settings.BROWSER_TYPE,
            **engine_selection.attribution(),
        )
        pw = await engine_selection.start_driver()
        try:
            (
                browser_context,
                browser_artifacts,
                browser_cleanup,
            ) = await BrowserContextFactory.create_browser_context(
                pw,
                proxy_location=proxy_location,
                url=url,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                workflow_permanent_id=workflow_permanent_id,
                script_id=script_id,
                organization_id=organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=cdp_connect_headers,
                browser_address=browser_address,
                browser_profile_id=browser_profile_id,
                engine_selection=engine_selection,
            )
        except BaseException:
            # start() already launched the local Node driver, so a failed context
            # creation (e.g. a remote-CDP connect_over_cdp error) would leak that driver
            # per attempt; stop it here, time-bounded like RealBrowserState.close() so a
            # hung stop() cannot stall the original error. BaseException so a cancellation
            # also releases the driver; a stop() error/timeout must never mask the original.
            try:
                async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT):
                    await pw.stop()
            except Exception:
                LOG.warning(
                    "Failed to stop Playwright driver after browser-context creation failure",
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                    exc_info=True,
                )
            raise
        return RealBrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            browser_cleanup=browser_cleanup,
            release_driver_on_close=browser_address is not None,
            engine_selection=engine_selection,
        )

    def evict_page(self, page_id: str) -> None:
        self.pages.pop(page_id, None)

    def get_for_task(self, task_id: str, workflow_run_id: str | None = None) -> BrowserState | None:
        if task_id in self.pages:
            return self.pages[task_id]

        if workflow_run_id and workflow_run_id in self.pages:
            LOG.info(
                "Browser state for task not found. Using browser state for workflow run",
                sampling=True,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
            )
            self.pages[task_id] = self.pages[workflow_run_id]
            return self.pages[task_id]

        return None

    async def get_or_create_for_task(
        self,
        task: Task,
        browser_session_id: str | None = None,
    ) -> BrowserState:
        browser_state = self.get_for_task(task_id=task.task_id, workflow_run_id=task.workflow_run_id)
        if browser_state is not None:
            return browser_state

        if browser_session_id:
            LOG.info(
                "Getting browser state for task from persistent sessions manager",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                browser_session_id, organization_id=task.organization_id
            )
            if browser_state is None:
                LOG.warning(
                    "Browser state not found in persistent sessions manager",
                    browser_session_id=browser_session_id,
                )
            else:
                if task.organization_id:
                    LOG.info("User to occupy browser session here", browser_session_id=browser_session_id)
                else:
                    LOG.warning("Organization ID is not set for task", task_id=task.task_id)
                page = await browser_state.get_working_page()
                if page:
                    await browser_state.navigate_to_url(page=page, url=task.url)
                else:
                    LOG.warning("Browser state has no page", workflow_run_id=task.workflow_run_id)

        proxy_location = task.proxy_location
        extra_http_headers = task.extra_http_headers
        if browser_state is None:
            LOG.info("Creating browser state for task", task_id=task.task_id)
            if browser_session_id and task.organization_id:
                session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(browser_session_id, task.organization_id)
                if session:
                    if session.proxy_location is not None:
                        proxy_location = session.proxy_location
                    extra_http_headers = _merge_proxy_session_headers(extra_http_headers, session.proxy_session_id)
            browser_state = await self._create_browser_state(
                proxy_location=proxy_location,
                url=task.url,
                task_id=task.task_id,
                # Pin the engine under the workflow_run_id for a workflow-owned task so it shares one
                # selection owner (and one flag distinct_id/property) with the workflow path. Both go to
                # engine-flag resolution only — workflow_run_id is still kept out of browser-context
                # creation here to preserve the task path's existing download-dir / artifact behavior.
                engine_run_key=canonical_run_key(workflow_run_id=task.workflow_run_id, task_id=task.task_id),
                engine_workflow_run_id=task.workflow_run_id,
                workflow_permanent_id=task.workflow_permanent_id,
                organization_id=task.organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=task.cdp_connect_headers,
                browser_address=task.browser_address,
            )

            if browser_session_id:
                await app.PERSISTENT_SESSIONS_MANAGER.set_browser_state(
                    browser_session_id,
                    browser_state,
                    organization_id=task.organization_id,
                )

        self.pages[task.task_id] = browser_state
        if task.workflow_run_id:
            self.pages[task.workflow_run_id] = browser_state

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        await browser_state.get_or_create_page(
            url=task.url,
            proxy_location=proxy_location,
            task_id=task.task_id,
            workflow_permanent_id=task.workflow_permanent_id,
            organization_id=task.organization_id,
            extra_http_headers=extra_http_headers,
            cdp_connect_headers=task.cdp_connect_headers,
            browser_address=task.browser_address,
        )
        await self._start_frame_publisher(
            browser_state=browser_state,
            workflow_run_id=task.workflow_run_id,
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        return browser_state

    async def get_or_create_for_workflow_run(
        self,
        workflow_run: WorkflowRun,
        url: str | None = None,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
        navigate: bool = True,
    ) -> BrowserState:
        parent_workflow_run_id = workflow_run.parent_workflow_run_id
        workflow_run_id = workflow_run.workflow_run_id
        if browser_profile_id is None:
            browser_profile_id = workflow_run.browser_profile_id

        # Check own cache entry first so navigate_to_url is only called on the first step.
        # Don't pass parent_workflow_run_id here — that lookup is deferred to the block
        # below so PBS runs don't accidentally inherit the parent's browser.
        browser_state = self.get_for_workflow_run(workflow_run_id=workflow_run_id)
        if browser_state:
            LOG.debug("Returning cached browser state for workflow run", workflow_run_id=workflow_run_id)
            return browser_state

        # When an explicit browser_session_id is provided (e.g. from a workflow
        # trigger block), skip the parent workflow lookup so the child uses the
        # specified persistent session instead of inheriting the parent's browser.
        # Note: at this point workflow_run_id is guaranteed not in self.pages (caught above),
        # so the call below can only match via parent_workflow_run_id.
        if not browser_session_id:
            browser_state = self.get_for_workflow_run(
                workflow_run_id=workflow_run_id, parent_workflow_run_id=parent_workflow_run_id
            )
            if browser_state:
                # always keep the browser state for the workflow run and the parent workflow run synced
                self.pages[workflow_run_id] = browser_state
                if parent_workflow_run_id:
                    self.pages[parent_workflow_run_id] = browser_state
                # The workflow-run streaming endpoint reads ``{workflow_run_id}.png``, so the
                # child needs its own publisher even when reusing the parent's browser state —
                # the parent's publisher writes a different key.
                await self._start_frame_publisher(
                    browser_state=browser_state,
                    workflow_run_id=workflow_run_id,
                    organization_id=workflow_run.organization_id,
                )
                return browser_state

        if browser_session_id:
            LOG.info(
                "Getting browser state for workflow run from persistent sessions manager",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                browser_session_id, organization_id=workflow_run.organization_id
            )
            if browser_state is None:
                LOG.warning(
                    "Browser state not found in persistent sessions manager", browser_session_id=browser_session_id
                )
            else:
                LOG.info("Used to occupy browser session here", browser_session_id=browser_session_id)
                await _rebind_pbs_download_dir(browser_state, workflow_run, browser_session_id)
                page = await browser_state.get_working_page()
                if page:
                    if url and navigate:
                        try:
                            await browser_state.navigate_to_url(page=page, url=url)
                        except FailedToNavigateToUrl as nav_exc:
                            if not _is_cached_cdp_drop_error(nav_exc):
                                raise
                            if not app.PERSISTENT_SESSIONS_MANAGER.supports_evict_and_reconnect():
                                # Default OSS impl: ``get_browser_state`` is an in-memory
                                # dict lookup, so an evict would tear down the only cached
                                # BrowserState without any way to reconnect — and would
                                # break profile/video cleanup at ``close_session`` later.
                                # Re-raise the original navigation error untouched.
                                raise
                            LOG.warning(
                                "Cached browser CDP appears dead at first goto — evicting and reconnecting once",
                                browser_session_id=browser_session_id,
                                workflow_run_id=workflow_run.workflow_run_id,
                                error_message=nav_exc.error_message,
                            )
                            await app.PERSISTENT_SESSIONS_MANAGER.evict_cached_browser_state(
                                browser_session_id,
                                organization_id=workflow_run.organization_id,
                                expected=browser_state,
                            )
                            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                                browser_session_id,
                                organization_id=workflow_run.organization_id,
                            )
                            if browser_state is None:
                                raise
                            await _rebind_pbs_download_dir(browser_state, workflow_run, browser_session_id)
                            page = await browser_state.get_working_page()
                            if page is not None:
                                await browser_state.navigate_to_url(page=page, url=url)
                            else:
                                # The fresh CDP connection has no working page (e.g. the
                                # prior context closed its last tab during the dead-CDP
                                # window). The outer ``get_or_create_page`` below mirrors
                                # the normal-path behavior and will produce a page +
                                # navigate to ``url``, so don't fail a recoverable
                                # session here — fall through.
                                LOG.info(
                                    "Recovered PBS reconnect has no working page — deferring to get_or_create_page",
                                    browser_session_id=browser_session_id,
                                    workflow_run_id=workflow_run.workflow_run_id,
                                )
                else:
                    LOG.warning("Browser state has no page", workflow_run_id=workflow_run.workflow_run_id)

        proxy_location = workflow_run.proxy_location
        extra_http_headers = workflow_run.extra_http_headers
        if browser_state is None:
            LOG.info(
                "Creating browser state for workflow run",
                sampling=True,
                workflow_run_id=workflow_run.workflow_run_id,
            )
            if browser_session_id and workflow_run.organization_id:
                session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
                    browser_session_id, workflow_run.organization_id
                )
                if session:
                    if session.proxy_location is not None:
                        proxy_location = session.proxy_location
                    extra_http_headers = _merge_proxy_session_headers(extra_http_headers, session.proxy_session_id)
            browser_state = await self._create_browser_state(
                proxy_location=proxy_location,
                url=url,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                organization_id=workflow_run.organization_id,
                extra_http_headers=extra_http_headers,
                cdp_connect_headers=workflow_run.cdp_connect_headers,
                browser_address=workflow_run.browser_address,
                browser_profile_id=browser_profile_id,
            )

            if browser_session_id:
                await app.PERSISTENT_SESSIONS_MANAGER.set_browser_state(
                    browser_session_id,
                    browser_state,
                    organization_id=workflow_run.organization_id,
                )

        self.pages[workflow_run_id] = browser_state
        # Only sync the parent's entry when the child is sharing the parent's
        # browser.  When an explicit browser_session_id is provided the child
        # has its own browser, and overwriting the parent's entry would break
        # subsequent parent blocks.
        if parent_workflow_run_id and not browser_session_id:
            self.pages[parent_workflow_run_id] = browser_state

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        # When navigate is False, the URL has already been used for proxy selection in
        # _create_browser_state above; we skip navigation so the caller (e.g. a generated
        # script) performs the first goto itself, avoiding a redundant page load.
        await browser_state.get_or_create_page(
            url=url if navigate else None,
            proxy_location=proxy_location,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            organization_id=workflow_run.organization_id,
            extra_http_headers=extra_http_headers,
            cdp_connect_headers=workflow_run.cdp_connect_headers,
            browser_address=workflow_run.browser_address,
            browser_profile_id=browser_profile_id,
        )
        await self._start_frame_publisher(
            browser_state=browser_state,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
        )
        return browser_state

    def get_for_workflow_run(
        self, workflow_run_id: str, parent_workflow_run_id: str | None = None
    ) -> BrowserState | None:
        # Priority: parent first, then own entry.
        # Callers that need to avoid parent inheritance must omit parent_workflow_run_id.
        # See get_or_create_for_workflow_run() for the two-phase lookup pattern.
        if parent_workflow_run_id and parent_workflow_run_id in self.pages:
            return self.pages[parent_workflow_run_id]

        if workflow_run_id in self.pages:
            return self.pages[workflow_run_id]

        return None

    def set_video_artifact_for_task(self, task: Task, artifacts: list[VideoArtifact]) -> None:
        if task.workflow_run_id and task.workflow_run_id in self.pages:
            self.pages[task.workflow_run_id].browser_artifacts.video_artifacts = artifacts
            return
        if task.task_id in self.pages:
            self.pages[task.task_id].browser_artifacts.video_artifacts = artifacts
            return

        raise MissingBrowserState(task_id=task.task_id)

    async def get_video_artifacts(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
        finalize: bool = True,
    ) -> list[VideoArtifact]:
        if len(browser_state.browser_artifacts.video_artifacts) == 0:
            LOG.warning(
                "Video data not found for task",
                task_id=task_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
            )
            return []

        for i, video_artifact in enumerate(browser_state.browser_artifacts.video_artifacts):
            path = video_artifact.video_path
            if path and os.path.exists(path=path):
                # Only the local Playwright-launched recording path produces WebM
                # that needs the remux fix-up. Other producers (e.g. fully formed
                # MP4 downloaded from a remote source) are already container-valid
                # and would be corrupted by ``finalize_webm`` — read those raw.
                is_webm = path.lower().endswith(".webm")
                if finalize and is_webm:
                    # Remux via ffmpeg so the WebM container has a valid Duration + Cues,
                    # even when browser_context.close() was killed mid-finalization.
                    browser_state.browser_artifacts.video_artifacts[i].video_data = await finalize_webm(path)
                else:
                    # Per-step snapshot while recording is still open — skip ffmpeg: the file is
                    # partial, so remux would either fail or be thrown away by the final pass.
                    with open(path, "rb") as f:
                        browser_state.browser_artifacts.video_artifacts[i].video_data = f.read()
            else:
                LOG.debug(
                    "Video path not found",
                    task_id=task_id,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                    video_path=path,
                )

        return browser_state.browser_artifacts.video_artifacts

    async def get_har_data(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> bytes:
        if browser_state:
            path = browser_state.browser_artifacts.har_path
            if path and os.path.exists(path=path):
                with open(path, "rb") as f:
                    return f.read()
        LOG.warning(
            "HAR data not found for task",
            task_id=task_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
        )
        return b""

    async def get_browser_console_log(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> bytes:
        if browser_state.browser_artifacts.browser_console_log_path is None:
            LOG.warning(
                "browser console log not found for task",
                task_id=task_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
            )
            return b""

        return await browser_state.browser_artifacts.read_browser_console_log()

    async def close(self) -> None:
        LOG.info("Closing BrowserManager")
        # Stop all streaming frame publishers before closing browsers so CDP
        # sessions detach cleanly. Cancellation here is best-effort and must
        # not block manager shutdown.
        for stream_key in list(self._frame_publishers.keys()):
            publisher = self._frame_publishers.pop(stream_key, None)
            if publisher is None:
                continue
            try:
                await publisher.stop()
            except Exception:
                LOG.debug(
                    "CDP frame publisher stop raised during manager close; ignored",
                    stream_key=stream_key,
                    exc_info=True,
                )
        for browser_state in self.pages.values():
            await browser_state.close()
        self.pages = dict()
        for run_key in list(self._engine_owners):
            await self._drop_engine_owner(run_key)
        LOG.info("BrowserManger is closed")

    async def cleanup_for_task(
        self,
        task_id: str,
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState | None:
        """
        Developer notes: handle errors here. Do not raise error from this function.
        If error occurs, log it and address the cleanup error.
        """
        LOG.info("Cleaning up for task")
        await self._drop_engine_owner(task_id)
        browser_state_to_close = self.pages.pop(task_id, None)
        if browser_state_to_close:
            # Stop tracing before closing the browser if tracing is enabled
            if browser_state_to_close.browser_context and browser_state_to_close.browser_artifacts.traces_dir:
                trace_path = f"{browser_state_to_close.browser_artifacts.traces_dir}/{task_id}.zip"
                await browser_state_to_close.browser_context.tracing.stop(path=trace_path)
                LOG.info("Stopped tracing", trace_path=trace_path)
            # Standalone-task only: a workflow-owned task's publisher is keyed
            # by ``workflow_run_id`` (see ``_resolve_stream_key``) and is stopped
            # by ``cleanup_for_workflow_run``. Passing ``task_id`` here is the
            # honest signal — it hits ``{task_id}.png`` for standalone tasks
            # and is a deliberate no-op for workflow tasks.
            await self._stop_frame_publisher(task_id=task_id)
            # A state backing a persistent session stays cached in the sessions
            # manager for reuse; its driver is released when the session closes.
            await browser_state_to_close.close(
                close_browser_on_completion=close_browser_on_completion,
                release_driver=False if browser_session_id else None,
            )
        LOG.info("Task is cleaned up")

        if browser_session_id:
            if organization_id:
                await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
                    browser_session_id, organization_id=organization_id
                )
                LOG.info("Released browser session", browser_session_id=browser_session_id)
            else:
                LOG.warning("Organization ID not specified, cannot release browser session", task_id=task_id)

        return browser_state_to_close

    async def cleanup_for_workflow_run(
        self,
        workflow_run_id: str,
        task_ids: list[str],
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
        child_workflow_run_ids: list[str] | None = None,
    ) -> BrowserState | None:
        LOG.info("Cleaning up for workflow run", sampling=True)
        browser_state_to_close = self.pages.get(workflow_run_id)

        # Drop the run's pinned engine — the run is ending, so no further browser resource will be
        # created for it. Covers the run, its inherited children, and its tasks.
        for run_key in (workflow_run_id, *(child_workflow_run_ids or ()), *task_ids):
            await self._drop_engine_owner(run_key)

        # Pop child workflow_run entries first — these are orphaned because child
        # workflows skip clean_up_workflow. Must happen before the shared check
        # so the task loop can correctly detect when the browser is no longer shared.
        if child_workflow_run_ids:
            for child_id in child_workflow_run_ids:
                self.pages.pop(child_id, None)
                # Child workflows skip their own cleanup, so the publishers
                # started for inherited child runs would otherwise leak until
                # process shutdown. Stop them here.
                await self._stop_frame_publisher(workflow_run_id=child_id)

        # Dual-stop is intentional and safe: both the explicit
        # ``_stop_frame_publisher`` above and the ``add_on_close`` callback
        # registered in ``_start_frame_publisher`` may fire for the same
        # stream key. ``dict.pop(key, None)`` makes the second pop a no-op.
        streams_active = stream_ref_active(workflow_run_id)

        if browser_state_to_close:
            # If another workflow run still references this browser state (e.g. a
            # parent whose in-memory browser was shared via use_parent_browser_session),
            # skip closing the browser so the parent can continue using it.
            shared = any(bs is browser_state_to_close for bs in self.pages.values())
            effective_close = close_browser_on_completion and not shared
            if shared:
                LOG.info(
                    "Browser state is shared with another workflow run, skipping browser close",
                    sampling=True,
                    workflow_run_id=workflow_run_id,
                )

            # Stop tracing before closing the browser if tracing is enabled.
            # Skip when the browser is shared — Playwright supports only one active
            # tracing session per context, so stopping here would kill the parent's trace.
            if (
                browser_state_to_close.browser_context
                and browser_state_to_close.browser_artifacts.traces_dir
                and not shared
            ):
                trace_path = f"{browser_state_to_close.browser_artifacts.traces_dir}/{workflow_run_id}.zip"
                await browser_state_to_close.browser_context.tracing.stop(path=trace_path)
                LOG.info("Stopped tracing", trace_path=trace_path)

            if streams_active:
                # Defer close until the last stream disconnects. Persist session cookies first: the
                # deferred close() runs after store_browser_session archives the dir, too late for it.
                await persist_session_cookies(
                    browser_state_to_close.browser_context,
                    browser_state_to_close.browser_artifacts.browser_session_dir,
                )
                LOG.info(
                    "Deferring browser close — active CDP streams",
                    workflow_run_id=workflow_run_id,
                )
                set_deferred_close_params(workflow_run_id, close_browser_on_completion)
                # Keep the publisher running while streams are attached. The
                # eventual ``close(True)`` fires the on-close callback that
                # stops it; ``close(False)`` is covered by the publisher's
                # own disconnect-driven self-termination.
            else:
                # Detach the publisher's CDP session before the Playwright context
                # closes; otherwise the stale session can race the teardown.
                await self._stop_frame_publisher(workflow_run_id=workflow_run_id)
                await browser_state_to_close.close(
                    close_browser_on_completion=effective_close,
                    release_driver=False if (shared or browser_session_id) else None,
                )

        if not streams_active:
            self.pages.pop(workflow_run_id, None)
        for task_id in task_ids:
            task_browser_state = self.pages.pop(task_id, None)
            if task_browser_state is None or streams_active:
                continue
            # Same shared-state check for task-level entries
            shared = any(bs is task_browser_state for bs in self.pages.values())
            effective_close = close_browser_on_completion and not shared
            if shared:
                LOG.info(
                    "Browser state is shared with another workflow run, skipping browser close",
                    sampling=True,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                )
            try:
                await task_browser_state.close(
                    close_browser_on_completion=effective_close,
                    release_driver=False if (shared or browser_session_id) else None,
                )
            except Exception:
                LOG.info(
                    "Failed to close the browser state from the task block, might because it's already closed.",
                    exc_info=True,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                )
        LOG.info("Workflow run is cleaned up", sampling=True)

        if browser_session_id:
            if organization_id:
                await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
                    browser_session_id, organization_id=organization_id
                )
                LOG.info("Released browser session", browser_session_id=browser_session_id)
            else:
                LOG.warning(
                    "Organization ID not specified, cannot release browser session", workflow_run_id=workflow_run_id
                )

        return browser_state_to_close

    async def get_or_create_for_script(
        self,
        script_id: str | None = None,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState:
        browser_state = self.get_for_script(script_id=script_id)
        if browser_state:
            return browser_state

        if browser_session_id:
            # Fail closed: look the session up under its real organization_id (release's symmetric key).
            if not organization_id:
                raise MissingOrganizationForBrowserSession(browser_session_id)
            LOG.info(
                "Getting browser state for script",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                browser_session_id, organization_id=organization_id
            )
            if browser_state is None:
                # Fail closed: a cold/evicted session has no reusable state. Silently creating a local
                # browser below would produce an unregistered state that terminal cleanup misclassifies as
                # a reusable persistent session (keyed off browser_session_id) and leaks instead of closes.
                raise MissingBrowserStateForBrowserSession(browser_session_id)
            page = await browser_state.get_working_page()
            if not page:
                LOG.warning("Browser state has no page to run the script", script_id=script_id)
        proxy_location = ProxyLocation.RESIDENTIAL
        if not browser_state:
            browser_state = await self._create_browser_state(
                proxy_location=proxy_location,
                script_id=script_id,
            )

        if script_id:
            self.pages[script_id] = browser_state
        await browser_state.get_or_create_page(
            proxy_location=proxy_location,
            script_id=script_id,
        )

        return browser_state

    async def cleanup_for_script(
        self,
        script_id: str,
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState | None:
        """Terminal reclamation of a standalone script's browser resources, mirroring
        ``cleanup_for_task``. Drops the run's pinned engine owner and the script-keyed page together
        so a completed script leaves no page, engine selection, or coordination entry behind. Called
        once at the script run's terminal boundary — never on a transient page/resource close, so a
        script that reconnects/reuses its state within a run keeps it. A state backing a persistent
        browser session is released (not driver-closed) so the session can be reused; its driver
        closes with the session. Errors are logged, not raised.
        """
        LOG.info("Cleaning up for script", script_id=script_id)
        pending_cancel: asyncio.CancelledError | None = None
        try:
            await self._drop_engine_owner(script_id)
        except asyncio.CancelledError as exc:
            # Our own cancellation surfaced while awaiting the owner's termination. Still reclaim the page and
            # session below — a cancelled terminal run must not leak them — then re-raise so the caller's
            # cancellation stays native. Only this first await parks on an in-flight resolver; the remaining
            # cleanup awaits run to completion because the delivered cancellation was already consumed here.
            pending_cancel = exc
        except Exception:
            # Contain an ordinary owner-drop failure so page/trace/close/release cleanup below is still
            # attempted.
            LOG.warning("Failed to drop engine owner during script cleanup", script_id=script_id, exc_info=True)

        async def _reclaim() -> BrowserState | None:
            browser_state_to_close = self.pages.pop(script_id, None)
            if browser_state_to_close:
                if browser_state_to_close.browser_context and browser_state_to_close.browser_artifacts.traces_dir:
                    trace_path = f"{browser_state_to_close.browser_artifacts.traces_dir}/{script_id}.zip"
                    try:
                        await browser_state_to_close.browser_context.tracing.stop(path=trace_path)
                        LOG.info("Stopped tracing", trace_path=trace_path)
                    except Exception:
                        LOG.warning("Failed to stop tracing during script cleanup", script_id=script_id, exc_info=True)
                try:
                    # Persistent session survives cleanup for reuse: don't close its context/driver, only release.
                    effective_close = close_browser_on_completion and not browser_session_id
                    await browser_state_to_close.close(
                        close_browser_on_completion=effective_close,
                        release_driver=False if browser_session_id else None,
                    )
                except Exception:
                    LOG.warning("Failed to close script browser state", script_id=script_id, exc_info=True)
            if browser_session_id and organization_id:
                # Best-effort per the "errors are logged, not raised" contract: a release failure must not
                # escape cleanup and mask the script's own exception (this runs in run_script's finally).
                try:
                    await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
                        browser_session_id, organization_id=organization_id
                    )
                    LOG.info("Released browser session", browser_session_id=browser_session_id)
                except Exception:
                    LOG.warning(
                        "Failed to release browser session during script cleanup",
                        script_id=script_id,
                        browser_session_id=browser_session_id,
                        exc_info=True,
                    )
            elif browser_session_id:
                LOG.warning("Organization ID not specified, cannot release browser session", script_id=script_id)
            return browser_state_to_close

        # Shield the page/trace/close/release reclamation as one unit: a caller cancellation (shutdown or
        # timeout) arriving mid-trace/close/release must not skip the rest and leak, so let it finish, then
        # re-raise so the caller's cancellation stays native. (_drop_engine_owner above keeps its own
        # cancellation handling — it must not block on a suppressing resolver.)
        reclaim = asyncio.ensure_future(_reclaim())
        try:
            browser_state_to_close = await asyncio.shield(reclaim)
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
            # Keep shielding across further cancellations while draining: a second cancel (e.g. a shutdown
            # re-cancel) must not cancel the reclamation and recreate the leak. Preserve the FIRST
            # cancellation for the native re-raise.
            while not reclaim.done():
                try:
                    await asyncio.shield(reclaim)
                except asyncio.CancelledError:
                    pass
            browser_state_to_close = reclaim.result()
        if pending_cancel is not None:
            raise pending_cancel
        return browser_state_to_close

    def get_for_script(self, script_id: str | None = None) -> BrowserState | None:
        if script_id and script_id in self.pages:
            return self.pages[script_id]
        return None
