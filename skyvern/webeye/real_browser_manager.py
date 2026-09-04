from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

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
from skyvern.forge.sdk.db.id import WORKFLOW_RUN_PREFIX
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.streaming.registries import (
    complete_stream_teardown,
    mark_stream_closing,
    set_deferred_close_params,
    stream_ref_active,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_artifacts import DownloadBinding, RecordingPrefixSnapshot, VideoArtifact
from skyvern.webeye.browser_engine import (
    BrowserEngineBootstrapError,
    BrowserEngineContext,
    BrowserEngineSelection,
    resolve_browser_engine,
)
from skyvern.webeye.browser_factory import BrowserContextFactory, rebind_download_dir
from skyvern.webeye.browser_manager import BrowserCleanupResult, BrowserManager
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.cdp_frame_publisher import (
    CDPFramePublisher,
    stream_key_for_task,
    stream_key_for_workflow_run,
)
from skyvern.webeye.persistent_sessions_manager import PBS_TASK_RUNNABLE_TYPE
from skyvern.webeye.real_browser_state import RealBrowserState
from skyvern.webeye.session_cookies import persist_session_cookies
from skyvern.webeye.video_utils import prepare_recording_for_upload

LOG = structlog.get_logger()

_WORKFLOW_RUN_KEY_PREFIX = f"{WORKFLOW_RUN_PREFIX}_"

# Matched to the CDP proxy's own stamp throttle: the idle budget it feeds is minutes (MIN_TIMEOUT is
# 5), so a stamp this stale still resolves the session active.
SESSION_ACTIVITY_RENEWAL_INTERVAL_SECONDS = 30.0

# Only driver/transport-level CDP drops trigger the cached-PBS evict + reconnect path.
# Playwright also surfaces page/context-only closes ("Target page, context or browser
# has been closed") with text that overlaps a transport drop; treating those as cached
# CDP drops would tear down a healthy PBS over a recoverable page-level state.
_CACHED_CDP_DROP_ERROR_SUBSTRINGS = ("Connection closed while reading from the driver",)


def _is_cached_cdp_drop_error(exc: FailedToNavigateToUrl) -> bool:
    message = exc.error_message or ""
    return any(needle in message for needle in _CACHED_CDP_DROP_ERROR_SUBSTRINGS)


# A bounded, read-only probe confirms an inherited browser's transport is really alive before
# the same-context (#14311) recovery runs new_page() on it. Kept small so the rare page-less
# inheritance path never stalls; a live cookies() round-trip returns well under this.
_INHERITED_BROWSER_LIVENESS_PROBE_TIMEOUT_SECONDS = 5.0

# Closed-transport signatures matched by message substring and by type NAME, never by imported
# class identity: scripts/patch_browser.sh rewrites playwright -> patchright for the agent image
# but not for cloud/persistent_browsers, so the two packages expose distinct TargetClosedError
# classes (see skyvern/webeye/cdp_frame_publisher.py for the same name-based matching).
_CLOSED_TRANSPORT_ERROR_SUBSTRINGS = (
    "Connection closed while reading from the driver",
    "Target page, context or browser has been closed",
    "Target closed",
    "Browser closed",
)
_CLOSED_TRANSPORT_ERROR_TYPE_NAMES = ("TargetClosedError",)


def _is_closed_transport_error(exc: BaseException) -> bool:
    if type(exc).__name__ in _CLOSED_TRANSPORT_ERROR_TYPE_NAMES:
        return True
    message = str(exc)
    return any(needle in message for needle in _CLOSED_TRANSPORT_ERROR_SUBSTRINGS)


async def _inherited_browser_transport_alive(browser_state: BrowserState) -> bool:
    """Truthfully classify an inherited, page-less browser as connected before same-context recovery.

    ``is_connected()`` only inspects cached client-side flags, so a driver/CDP transport that died
    with no I/O since (a long sequential-gate wait, a reaped remote browser, a NAT/LB idle timeout,
    a TCP half-close) still reports connected. Confirm with one bounded, read-only round-trip:
    a genuinely live tab-less context answers and is reused (preserving cookies/session), while a
    dead transport is classified disconnected here and routed to the existing fresh-browser path,
    instead of crashing the #14311 ``new_page()`` recovery with
    "Connection closed while reading from the driver" (SKY-13389).
    """
    if not browser_state.is_connected():
        return False
    context = browser_state.browser_context
    if context is None:
        return False
    try:
        async with asyncio.timeout(_INHERITED_BROWSER_LIVENESS_PROBE_TIMEOUT_SECONDS):
            await context.cookies()
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        # Process-control signals are never a browser-liveness verdict — propagate untouched.
        raise
    except (asyncio.TimeoutError, TimeoutError):
        LOG.info("Inherited browser liveness probe timed out; treating browser as disconnected")
        return False
    except Exception as exc:
        # Only a genuinely closed transport counts as disconnected; anything else is an unexpected
        # programming error that must not be silently swallowed into a fresh-browser fallback.
        if _is_closed_transport_error(exc):
            LOG.info(
                "Inherited browser liveness probe hit a closed transport; treating browser as disconnected",
                error=str(exc),
            )
            return False
        raise
    return True


async def _rebind_pbs_download_dir(
    browser_state: BrowserState,
    download_run_id: str,
    browser_session_id: str,
) -> None:
    if browser_state.browser_artifacts.download_binding == DownloadBinding.SESSION_DIR:
        # Provider-owned remote binding: preserve the provider-selected destination. Re-pointing to a
        # run-scoped dir would overwrite it, so skip the rebind.
        LOG.info(
            "Skipping download-dir rebind: preserving provider-selected destination",
            browser_session_id=browser_session_id,
            download_run_id=download_run_id,
        )
        return
    browser_context = browser_state.browser_context
    if browser_context is None:
        return
    try:
        adopted_browser = browser_context.browser
        rebind_page = None if adopted_browser is not None else await browser_state.get_working_page()
        if adopted_browser is None and rebind_page is None:
            return
        if getattr(browser_context, "_skyvern_download_run_id", None) == download_run_id:
            return
        # Not gated on the interceptor: a Skyvern-hosted context has none, and rebind_download_dir
        # is what repoints its Browser.setDownloadBehavior off the session-scoped connect-time path.
        if adopted_browser is not None:
            await rebind_download_dir(adopted_browser, run_id=download_run_id)
        else:
            await rebind_download_dir(None, run_id=download_run_id, page=rebind_page)
        browser_context._skyvern_download_run_id = download_run_id  # type: ignore[attr-defined]
    except Exception:
        LOG.warning(
            "Failed to rebind download dir on adopted browser session",
            browser_session_id=browser_session_id,
            download_run_id=download_run_id,
            exc_info=True,
        )


async def _on_browser_state_acquired(
    browser_state: BrowserState,
    workflow_run_id: str | None,
) -> BrowserState:
    browser_context = browser_state.browser_context
    if browser_context is not None:
        await app.AGENT_FUNCTION.on_browser_context_acquired(browser_context, workflow_run_id)
    return browser_state


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

    def __init__(self, task: asyncio.Future[BrowserEngineSelection]) -> None:
        # A resolver Task (single-flight) or an already-resolved Future (a guarded-repinned effective
        # selection). Both satisfy the await/shield/done/cancel surface waiters use.
        self.task = task
        self.terminal = False


@dataclass(frozen=True)
class _PersistentSessionLease:
    session_id: str
    organization_id: str
    # Only a runnable that owns the session may create a cleanup lease.
    runnable_id: str
    browser_state: BrowserState
    runnable_generation_id: str | None = None


class RealBrowserManager(BrowserManager):
    def __init__(self) -> None:
        self.pages: dict[str, BrowserState] = {}
        # Engine pinned per logical run, keyed by run id (workflow_run_id / task_id / script_id) via a
        # per-key single-flight owner. Resolved once at the first browser-resource creation for a run
        # and reused for every later resource/recreation in that run, so recreation can never
        # re-resolve to a different engine (e.g. after a flag change). Dropped — with its in-flight
        # resolution cancelled — when the run's browser state is cleaned up.
        self._engine_owners: dict[str, _EngineSelectionOwner] = {}
        # The runnable identity accepted by begin_session, carried unchanged into teardown. Cleanup
        # reads this lease instead of reconstructing identity from Task/Workflow fields.
        self._persistent_session_leases: dict[str, _PersistentSessionLease] = {}
        # Runnables between begin_session and their lease, refcounted by runnable id. Occupancy is
        # published first and occupy does not extend the session's timeout, so without this a
        # reused-but-expired session is unprotected for the whole attach. Refcounts keep overlapping
        # acquisitions for the same owner live until their last span exits.
        self._acquiring_session_runnables: dict[str, int] = {}
        # CDP frame publishers keyed by stream key (``{wr}.png`` / ``{task}.png``).
        self._frame_publishers: dict[str, CDPFramePublisher] = {}
        # Serializes the check/create/start/store/register sequence in
        # ``_start_frame_publisher`` so concurrent attaches for one stream key
        # cannot orphan a publisher loop.
        self._publisher_lock = asyncio.Lock()
        # Started at the first lease: no event loop runs when the process-wide manager is built.
        self._session_activity_renewer: asyncio.Task[None] | None = None
        # A failed release retains its lease for cleanup attribution but must stop renewing: the
        # stamp outlives this process and would hold a browser past every deadline that reclaims it.
        self._released_session_ids: set[str] = set()

    @staticmethod
    def _matching_session_lease(
        lease: _PersistentSessionLease | None,
        session_id: str,
        organization_id: str,
    ) -> _PersistentSessionLease | None:
        if lease is None or lease.session_id != session_id or lease.organization_id != organization_id:
            return None
        return lease

    async def _release_persistent_session(
        self,
        session_id: str,
        organization_id: str,
        lease: _PersistentSessionLease | None,
    ) -> bool:
        self._released_session_ids.add(session_id)
        owner = self._matching_session_lease(lease, session_id, organization_id)
        context = skyvern_context.current()
        expected_runnable_id: str | None = context.browser_session_runnable_id if context else None
        expected_runnable_generation_id: str | None = None
        expected_browser_state: BrowserState | None = owner.browser_state if owner else None

        if owner is not None:
            expected_runnable_id = owner.runnable_id
            expected_runnable_generation_id = owner.runnable_generation_id
        elif context and context.browser_session_runnable_generation_id is not None:
            expected_runnable_generation_id = context.browser_session_runnable_generation_id

        return await app.PERSISTENT_SESSIONS_MANAGER.release_browser_session(
            session_id=session_id,
            organization_id=organization_id,
            expected_runnable_id=expected_runnable_id,
            expected_runnable_generation_id=expected_runnable_generation_id,
            expected_browser_state=expected_browser_state,
        )

    def _discard_session_lease(self, run_id: str, lease: _PersistentSessionLease | None) -> None:
        if lease is not None and self._persistent_session_leases.get(run_id) is lease:
            self._persistent_session_leases.pop(run_id, None)
            self._released_session_ids.discard(lease.session_id)

    @asynccontextmanager
    async def acquiring_session_runnable(self, runnable_id: str | None) -> AsyncIterator[None]:
        acquiring_runnables = getattr(self, "_acquiring_session_runnables", None)
        if acquiring_runnables is None:
            acquiring_runnables = self._acquiring_session_runnables = {}
        if runnable_id is not None:
            acquiring_runnables[runnable_id] = acquiring_runnables.get(runnable_id, 0) + 1
        try:
            yield
        finally:
            if runnable_id is not None:
                remaining = acquiring_runnables.get(runnable_id, 0) - 1
                if remaining > 0:
                    acquiring_runnables[runnable_id] = remaining
                else:
                    acquiring_runnables.pop(runnable_id, None)

    def _store_session_lease(self, run_id: str, lease: _PersistentSessionLease) -> None:
        self._persistent_session_leases[run_id] = lease
        self._released_session_ids.discard(lease.session_id)
        if self._session_activity_renewer is None or self._session_activity_renewer.done():
            self._session_activity_renewer = asyncio.create_task(
                self._renew_session_activity_leases(), name="persistent_session_activity_renewal"
            )

    def _renewable_session_ids(self) -> set[str]:
        held = {lease.session_id for lease in self._persistent_session_leases.values()}
        return held - self._released_session_ids

    async def _renew_session_activity_leases(self) -> None:
        # The session Pod ends a session on last_activity_at alone, and only the CDP proxy wrote it, so
        # a run driving a browser it did not reach through the proxy was closed mid-step (SKY-15568).
        # Sleeps before the first stamp: the base budget covers a fresh attach, and a run shorter than
        # the interval never writes. Exits once nothing is renewable; the next lease starts a fresh task.
        while self._renewable_session_ids():
            await asyncio.sleep(SESSION_ACTIVITY_RENEWAL_INTERVAL_SECONDS)
            for session_id in self._renewable_session_ids():
                try:
                    await app.DATABASE.browser_sessions.touch_last_activity(session_id)
                except Exception:
                    LOG.warning(
                        "Failed to renew browser session activity lease",
                        browser_session_id=session_id,
                        exc_info=True,
                    )

    def live_session_runnable_ids(self) -> set[str]:
        lease_runnable_ids = {lease.runnable_id for lease in self._persistent_session_leases.values()}
        acquiring_runnable_ids = {
            runnable_id for runnable_id, count in self._acquiring_session_runnables.items() if count > 0
        }
        return set(self._persistent_session_leases) | lease_runnable_ids | acquiring_runnable_ids

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

    def _repin_engine_selection(self, run_key: str | None, selection: BrowserEngineSelection) -> None:
        """Replace a run's pinned engine owner with an already-resolved ``selection`` so later
        browser-resource creation reuses it. Guarded no-op for an ephemeral (``run_key`` None),
        already-removed, or ``terminal`` owner — never resurrects a missing/terminal owner."""
        if run_key is None:
            return
        owner = self._engine_owners.get(run_key)
        if owner is None or owner.terminal:
            return
        resolved: asyncio.Future[BrowserEngineSelection] = asyncio.get_running_loop().create_future()
        resolved.set_result(selection)
        owner.task = resolved

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
        cdp_port: int | None = None,
        browser_profile_id: str | None = None,
        engine_run_key: str | None = None,
        engine_workflow_run_id: str | None = None,
    ) -> BrowserState:
        run_key = engine_run_key or canonical_run_key(
            workflow_run_id=workflow_run_id, task_id=task_id, script_id=script_id
        )
        engine_selection = await self.get_or_resolve_engine_selection(
            run_key=run_key,
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
        context = skyvern_context.current()

        async def _start(selection: BrowserEngineSelection) -> BrowserState:
            LOG.info(
                "Creating browser state",
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                browser_source=settings.BROWSER_TYPE,
                **selection.attribution(),
            )
            try:
                pw = await selection.start_driver()
            except Exception as start_error:
                # Mark a fallback-eligible driver-start failure so the boundary can degrade once; a
                # no-fallback selection (and CancelledError, a BaseException) propagates unchanged.
                if selection.boot_fallback_selection is None:
                    raise
                raise BrowserEngineBootstrapError(f"{selection.name} driver failed to start") from start_error
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
                    cdp_port=cdp_port,
                    browser_address_is_server_assigned=bool(context and context.browser_address_is_server_assigned),
                    browser_profile_id=browser_profile_id,
                    engine_selection=selection,
                )
            except BaseException:
                # start() launched the local Node driver; stop it (time-bounded) so a failed context
                # creation doesn't leak it, and never let a stop() error/timeout mask the original.
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
                engine_selection=selection,
            )

        # At most two attempts: a fallback-eligible (Rustwright) selection degrades EXACTLY ONCE to its
        # classical boot fallback before any usable context; the classical has none, so it then propagates.
        boot_fallback = engine_selection.boot_fallback_selection
        try:
            state = await _start(engine_selection)
        except BrowserEngineBootstrapError:
            if boot_fallback is None:
                raise
            LOG.warning(
                "Browser engine boot failed before a usable context; falling back once to its classical engine",
                failed_engine=engine_selection.name,
                fallback_engine=boot_fallback.name,
                task_id=task_id,
                workflow_run_id=workflow_run_id,
                exc_info=True,
            )
            self._repin_engine_selection(run_key, boot_fallback)
            return await _start(boot_fallback)
        if boot_fallback is not None:
            # Commit-strip: re-pin the same engine with its boot fallback removed so a later same-run
            # recreation reuses the effective engine and can no longer fall back.
            self._repin_engine_selection(run_key, replace(engine_selection, boot_fallback_selection=None))
        return state

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
            if browser_session_id and not browser_state.is_connected():
                LOG.warning(
                    "Cached persistent-session browser state for task is disconnected; reconnecting",
                    task_id=task.task_id,
                    workflow_run_id=task.workflow_run_id,
                    browser_session_id=browser_session_id,
                )
                for stale_key in (task.task_id, task.workflow_run_id):
                    if stale_key and self.pages.get(stale_key) is browser_state:
                        self.pages.pop(stale_key, None)
                browser_state = None
            else:
                return await _on_browser_state_acquired(browser_state, task.workflow_run_id)

        if browser_session_id:
            if not task.organization_id:
                raise MissingOrganizationForBrowserSession(browser_session_id)
            context = skyvern_context.current()
            expected_runnable_id = (
                task.task_id
                if task.workflow_run_id is None
                else (context.browser_session_runnable_id if context else None) or task.workflow_run_id
            )
            async with self.acquiring_session_runnable(expected_runnable_id):
                expected_runnable_generation_id: str | None
                if task.workflow_run_id is None:
                    expected_runnable_generation_id = await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
                        browser_session_id=browser_session_id,
                        runnable_type=PBS_TASK_RUNNABLE_TYPE,
                        runnable_id=task.task_id,
                        organization_id=task.organization_id,
                    )
                    if context is not None:
                        context.browser_session_runnable_id = expected_runnable_id
                        context.browser_session_runnable_generation_id = expected_runnable_generation_id
                else:
                    # The workflow service already acquired this lease. A task inside that workflow
                    # inherits the immutable workflow identity and never creates a competing task lease.
                    expected_runnable_generation_id = (
                        context.browser_session_runnable_generation_id if context else None
                    )
                download_run_id = (
                    resolve_run_download_id(skyvern_context.current(), fallback_run_id=expected_runnable_id)
                    or expected_runnable_id
                )
                LOG.info(
                    "Getting browser state for task from persistent sessions manager",
                    browser_session_id=browser_session_id,
                )
                get_state_kwargs = {
                    "organization_id": task.organization_id,
                    "expected_runnable_id": expected_runnable_id,
                    "download_run_id": download_run_id,
                }
                if expected_runnable_generation_id is not None:
                    get_state_kwargs["expected_runnable_generation_id"] = expected_runnable_generation_id
                browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    browser_session_id,
                    **get_state_kwargs,
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
                    await _rebind_pbs_download_dir(browser_state, download_run_id, browser_session_id)
                    self._store_session_lease(
                        expected_runnable_id,
                        _PersistentSessionLease(
                            session_id=browser_session_id,
                            organization_id=task.organization_id,
                            runnable_id=expected_runnable_id,
                            runnable_generation_id=expected_runnable_generation_id,
                            browser_state=browser_state,
                        ),
                    )
            if browser_state is not None:
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
        return await _on_browser_state_acquired(browser_state, task.workflow_run_id)

    async def get_or_create_for_workflow_run(
        self,
        workflow_run: WorkflowRun,
        url: str | None = None,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
        navigate: bool = True,
        browser_session_runnable_id: str | None = None,
        browser_session_runnable_generation_id: str | None = None,
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
            if browser_session_id and not browser_state.is_connected():
                LOG.warning(
                    "Cached persistent-session browser state for workflow run is disconnected; reconnecting",
                    workflow_run_id=workflow_run_id,
                    browser_session_id=browser_session_id,
                )
                if self.pages.get(workflow_run_id) is browser_state:
                    self.pages.pop(workflow_run_id, None)
                browser_state = None
            else:
                LOG.debug("Returning cached browser state for workflow run", workflow_run_id=workflow_run_id)
                return await _on_browser_state_acquired(browser_state, workflow_run_id)

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
                # A parent_workflow_run_id entry is shared by every child run of the same parent.
                # Independent children dispatched to one long-lived worker (e.g. a sequential
                # fan-out) can therefore find an earlier, already-completed sibling's torn-down
                # browser here. This early-return path skips get_or_create_page, so returning a
                # page-less state would fail the run's first browser block with a missing page.
                working_page = await browser_state.get_working_page()
                if working_page is None and await _inherited_browser_transport_alive(browser_state):
                    # The inherited browser is still live but its last valid tab was closed
                    # (e.g. a use_parent_browser_session child whose prior run closed the last
                    # page). Recreate a page in the SAME context so the child keeps the parent's
                    # cookies/session, instead of orphaning the live browser and starting fresh.
                    # Liveness is actively probed (not just is_connected()'s cached flags) so a
                    # transport that died while idle falls through to a fresh browser (SKY-13389).
                    LOG.info(
                        "Inherited parent browser is live but has no working page; recreating a page in the same context",
                        workflow_run_id=workflow_run_id,
                        parent_workflow_run_id=parent_workflow_run_id,
                    )
                    await browser_state.get_or_create_page(
                        proxy_location=workflow_run.proxy_location,
                        workflow_run_id=workflow_run_id,
                        workflow_permanent_id=workflow_run.workflow_permanent_id,
                        organization_id=workflow_run.organization_id,
                        extra_http_headers=workflow_run.extra_http_headers,
                        cdp_connect_headers=workflow_run.cdp_connect_headers,
                        browser_address=workflow_run.browser_address,
                        browser_profile_id=browser_profile_id,
                    )
                    working_page = await browser_state.get_working_page()
                if working_page is not None:
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
                    return await _on_browser_state_acquired(browser_state, workflow_run_id)
                # The inherited state is genuinely torn down (disconnected and page-less).
                # Drop the stale entry and fall through to create a fresh browser for this run.
                LOG.warning(
                    "Inherited parent browser state is torn down; creating a fresh browser",
                    workflow_run_id=workflow_run_id,
                    parent_workflow_run_id=parent_workflow_run_id,
                )
                for stale_key in (workflow_run_id, parent_workflow_run_id):
                    if stale_key and self.pages.get(stale_key) is browser_state:
                        self.pages.pop(stale_key, None)
                browser_state = None

        if browser_session_id:
            context = skyvern_context.current()
            # A synthetic run (minted per-action by run_sdk_action) never begins the session, so
            # presenting it as the expected owner can only ever fail the ownership guard (SKY-13518).
            owner_fallback_id = (
                None if context is not None and context.workflow_run_is_synthetic else workflow_run.workflow_run_id
            )
            expected_runnable_id = (
                browser_session_runnable_id
                or (context.browser_session_runnable_id if context else None)
                or owner_fallback_id
            )
            expected_runnable_generation_id = browser_session_runnable_generation_id or (
                context.browser_session_runnable_generation_id if context else None
            )
            download_run_id = (
                resolve_run_download_id(skyvern_context.current(), fallback_run_id=workflow_run.workflow_run_id)
                or workflow_run.workflow_run_id
            )
            LOG.info(
                "Getting browser state for workflow run from persistent sessions manager",
                browser_session_id=browser_session_id,
            )
            async with self.acquiring_session_runnable(expected_runnable_id):
                browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    browser_session_id,
                    **{
                        "organization_id": workflow_run.organization_id,
                        "expected_runnable_id": expected_runnable_id,
                        "download_run_id": download_run_id,
                        **(
                            {"expected_runnable_generation_id": expected_runnable_generation_id}
                            if expected_runnable_generation_id is not None
                            else {}
                        ),
                    },
                )
                if browser_state is not None:
                    LOG.info("Used to occupy browser session here", browser_session_id=browser_session_id)
                    # An SDK-minted synthetic run only reads a session owned by another runnable.
                    # It cannot rebind that runnable's download directory or acquire a cleanup lease.
                    if expected_runnable_id is not None:
                        await _rebind_pbs_download_dir(browser_state, download_run_id, browser_session_id)
                        self._store_session_lease(
                            workflow_run.workflow_run_id,
                            _PersistentSessionLease(
                                session_id=browser_session_id,
                                organization_id=workflow_run.organization_id,
                                runnable_id=expected_runnable_id,
                                runnable_generation_id=expected_runnable_generation_id,
                                browser_state=browser_state,
                            ),
                        )
            if browser_state is None:
                LOG.warning(
                    "Browser state not found in persistent sessions manager", browser_session_id=browser_session_id
                )
            else:
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
                                **{
                                    "organization_id": workflow_run.organization_id,
                                    "expected_runnable_id": expected_runnable_id,
                                    "download_run_id": download_run_id,
                                    **(
                                        {
                                            "expected_runnable_generation_id": expected_runnable_generation_id,
                                        }
                                        if expected_runnable_generation_id is not None
                                        else {}
                                    ),
                                },
                            )
                            if browser_state is None:
                                raise
                            if expected_runnable_id is not None:
                                self._store_session_lease(
                                    workflow_run.workflow_run_id,
                                    _PersistentSessionLease(
                                        session_id=browser_session_id,
                                        organization_id=workflow_run.organization_id,
                                        runnable_id=expected_runnable_id,
                                        runnable_generation_id=expected_runnable_generation_id,
                                        browser_state=browser_state,
                                    ),
                                )
                                await _rebind_pbs_download_dir(browser_state, download_run_id, browser_session_id)
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
                browser_profile_id=browser_profile_id or "none",
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
        return await _on_browser_state_acquired(browser_state, workflow_run_id)

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

        raise MissingBrowserState(
            task_id=task.task_id,
            workflow_run_id=task.workflow_run_id,
            detected_at=datetime.now(UTC),
            failure_reason="browser_state_registry_lookup_miss",
        )

    async def get_video_artifacts(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
        finalize: bool = True,
    ) -> list[VideoArtifact]:
        if len(browser_state.browser_artifacts.video_artifacts) == 0:
            # Empty is the expected state on browsers with no local Playwright recording
            # (vendor/CDP/persistent sessions) until finalize time, when vendor recordings
            # have been attached and a still-empty list means the recording is missing.
            log = LOG.warning if finalize else LOG.debug
            log(
                "Video data not found for task",
                task_id=task_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
            )
            return []

        for i, video_artifact in enumerate(browser_state.browser_artifacts.video_artifacts):
            path = video_artifact.video_path
            if path and os.path.exists(path=path):
                is_webm = path.lower().endswith(".webm")
                if finalize and is_webm:
                    async with prepare_recording_for_upload(path) as prepared:
                        with open(prepared.path, "rb") as f:
                            browser_state.browser_artifacts.video_artifacts[i].video_data = f.read()
                        browser_state.browser_artifacts.video_artifacts[
                            i
                        ].video_file_extension = prepared.file_extension
                else:
                    # Non-WebM sources are already container-valid; per-step WebM snapshots are still incomplete.
                    with open(path, "rb") as f:
                        browser_state.browser_artifacts.video_artifacts[i].video_data = f.read()
                    browser_state.browser_artifacts.video_artifacts[i].video_file_extension = (
                        os.path.splitext(path)[1].lstrip(".").lower() or "webm"
                    )
            else:
                LOG.debug(
                    "Video path not found",
                    task_id=task_id,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                    video_path=path,
                )

        return browser_state.browser_artifacts.video_artifacts

    def snapshot_recording_prefixes(
        self,
        browser_state: BrowserState,
        task_id: str = "",
    ) -> list[RecordingPrefixSnapshot] | None:
        """Plan a per-step recording sync without reading any bytes.

        Mirrors the per-step branch of ``get_video_artifacts(finalize=False)`` for the shipped Playwright
        per-page recording: returns ``[]`` when there is nothing to sync, a per-artifact prefix plan (a
        size snapshot of each ordinary growing WebM recording) for the fast streaming path, or ``None`` to
        fall back to the byte-based path whenever an artifact is non-WebM, not yet registered, or missing on
        disk.
        """
        video_artifacts = browser_state.browser_artifacts.video_artifacts
        if len(video_artifacts) == 0:
            return []

        snapshots: list[RecordingPrefixSnapshot] = []
        for video_artifact in video_artifacts:
            path = video_artifact.video_path
            if not video_artifact.video_artifact_id or not path or not os.path.exists(path=path):
                return None
            if not path.lower().endswith(".webm"):
                return None
            snapshots.append(
                RecordingPrefixSnapshot(
                    video_artifact_id=video_artifact.video_artifact_id,
                    path=path,
                    prefix_len=os.path.getsize(path),
                )
            )
        return snapshots

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
        if self._session_activity_renewer is not None:
            self._session_activity_renewer.cancel()
            self._session_activity_renewer = None
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
        session_lease = self._persistent_session_leases.get(task_id)
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
                released = await self._release_persistent_session(
                    browser_session_id,
                    organization_id,
                    session_lease,
                )
                if released:
                    self._discard_session_lease(task_id, session_lease)
                LOG.info("Released browser session", browser_session_id=browser_session_id)
            else:
                LOG.warning("Organization ID not specified, cannot release browser session", task_id=task_id)

        return browser_state_to_close

    def _shared_with_another_workflow_run(self, workflow_run_id: str, browser_state_to_close: BrowserState) -> bool:
        # NON-PBS ONLY. Python-object sharing of an ephemeral BrowserState is process-local, so an
        # alias may veto the terminal close only when it denotes ANOTHER workflow run that is
        # currently live in THIS process and legitimately shares this exact state (parent/child
        # use_parent_browser_session). Three qualifications, all required:
        #   1. another run's key (wr_ prefix, not our own) — same-run task/script aliases never share;
        #   2. object identity — it points at the exact state we are about to close;
        #   3. that run is live here — it still has a workflow-run context. A remote/finished/ghost
        #      parent whose key was forward-synced into this process, or a never-cleaned
        #      synthetic-run key, has no context and must not veto the close, or the browser
        #      would outlive every run in this process.
        # PBS lifetime is distributed (runnable_id + generation CAS across processes) and is governed
        # elsewhere: close is force-gated off (close_browser_on_completion=False) and release goes
        # through the expected-owner CAS. This local-liveness predicate must never decide PBS close.
        return any(
            page_id != workflow_run_id
            and page_id.startswith(_WORKFLOW_RUN_KEY_PREFIX)
            and browser_state is browser_state_to_close
            and app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context(page_id)
            for page_id, browser_state in self.pages.items()
        )

    async def cleanup_for_workflow_run(
        self,
        workflow_run_id: str,
        task_ids: list[str],
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
        child_workflow_run_ids: list[str] | None = None,
    ) -> BrowserCleanupResult:
        LOG.info("Cleaning up for workflow run", sampling=True)
        # No await before the tombstone: a concurrent stream attach either increments first and is
        # observed below, or sees CLOSING. The tombstone also holds the session lease immediately,
        # before deferred-close parameters exist, until complete_stream_teardown releases it.
        mark_stream_closing(workflow_run_id)
        browser_state_to_close = self.pages.get(workflow_run_id)
        session_lease = self._persistent_session_leases.get(workflow_run_id)
        recording_finalized = False
        finalization_attempted = False

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
            shared = self._shared_with_another_workflow_run(workflow_run_id, browser_state_to_close)
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
                shared_after_cleanup = self._shared_with_another_workflow_run(workflow_run_id, browser_state_to_close)
                deferred_close = close_browser_on_completion and not shared_after_cleanup
                release_driver = False if (shared_after_cleanup or browser_session_id) else None
                owner = (
                    self._matching_session_lease(session_lease, browser_session_id, organization_id)
                    if browser_session_id is not None and organization_id is not None
                    else None
                )
                if owner is None:
                    streams_active = set_deferred_close_params(
                        workflow_run_id,
                        deferred_close,
                        release_driver=release_driver,
                    )
                else:
                    streams_active = set_deferred_close_params(
                        workflow_run_id,
                        deferred_close,
                        release_driver=release_driver,
                        browser_session_id=owner.session_id,
                        organization_id=owner.organization_id,
                        expected_runnable_id=owner.runnable_id,
                        expected_runnable_generation_id=owner.runnable_generation_id,
                        expected_browser_state=owner.browser_state,
                    )
                if not streams_active:
                    effective_close = deferred_close
                    shared = shared_after_cleanup
                elif owner is not None:
                    # The tombstone now owns the immutable lease until its finalizer succeeds.
                    self._discard_session_lease(workflow_run_id, session_lease)
            if streams_active:
                LOG.info(
                    "Deferring browser close — active CDP streams",
                    workflow_run_id=workflow_run_id,
                )
                # Keep the publisher running while streams are attached. The
                # eventual ``close(True)`` fires the on-close callback that
                # stops it; ``close(False)`` is covered by the publisher's
                # own disconnect-driven self-termination.
            else:
                # Detach the publisher's CDP session before the Playwright context
                # closes; otherwise the stale session can race the teardown.
                await self._stop_frame_publisher(workflow_run_id=workflow_run_id)
                close_succeeded = await browser_state_to_close.close(
                    close_browser_on_completion=effective_close,
                    release_driver=False if (shared or browser_session_id) else None,
                )
                finalization_attempted = effective_close
                recording_finalized = effective_close and bool(close_succeeded)

        if not streams_active:
            self.pages.pop(workflow_run_id, None)
        for task_id in task_ids:
            task_browser_state = self.pages.pop(task_id, None)
            if task_browser_state is None or streams_active:
                continue
            if task_browser_state is browser_state_to_close and finalization_attempted:
                continue
            # Same liveness-qualified ownership predicate as the run-level close: a distinct
            # task-level state must not be held open by a ghost alias, and it must still yield to a
            # genuinely live cross-run sharer.
            shared = self._shared_with_another_workflow_run(task_id, task_browser_state)
            effective_close = close_browser_on_completion and not shared
            if shared:
                LOG.info(
                    "Browser state is shared with another workflow run, skipping browser close",
                    sampling=True,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                )
            try:
                if task_browser_state is browser_state_to_close:
                    finalization_attempted = effective_close
                close_succeeded = await task_browser_state.close(
                    close_browser_on_completion=effective_close,
                    release_driver=False if (shared or browser_session_id) else None,
                )
                if task_browser_state is browser_state_to_close and effective_close:
                    recording_finalized = bool(close_succeeded)
            except Exception:
                LOG.info(
                    "Failed to close the browser state from the task block, might because it's already closed.",
                    exc_info=True,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                )
        LOG.info("Workflow run is cleaned up", sampling=True)

        release_complete = True
        if browser_session_id and not streams_active:
            if organization_id:
                release_complete = await self._release_persistent_session(
                    browser_session_id,
                    organization_id,
                    session_lease,
                )
                if release_complete:
                    self._discard_session_lease(workflow_run_id, session_lease)
                LOG.info("Released browser session", browser_session_id=browser_session_id)
            else:
                LOG.warning(
                    "Organization ID not specified, cannot release browser session", workflow_run_id=workflow_run_id
                )

        if not streams_active and release_complete:
            complete_stream_teardown(workflow_run_id)

        return BrowserCleanupResult(
            browser_state=browser_state_to_close,
            recording_finalized=recording_finalized,
        )

    async def get_or_create_for_script(
        self,
        script_id: str | None = None,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState:
        context = skyvern_context.current()
        workflow_run_id = context.workflow_run_id if context else None
        browser_state = self.get_for_script(script_id=script_id)
        if browser_state:
            return await _on_browser_state_acquired(browser_state, workflow_run_id)

        if browser_session_id:
            # Fail closed: look the session up under its real organization_id (release's symmetric key).
            if not organization_id:
                raise MissingOrganizationForBrowserSession(browser_session_id)
            if not script_id:
                raise MissingBrowserStateForBrowserSession(browser_session_id)
            async with self.acquiring_session_runnable(script_id):
                raw_generation_id = await app.PERSISTENT_SESSIONS_MANAGER.begin_session(
                    browser_session_id=browser_session_id,
                    runnable_type="script",
                    runnable_id=script_id,
                    organization_id=organization_id,
                )
                expected_runnable_generation_id = raw_generation_id if isinstance(raw_generation_id, str) else None
                if context is not None:
                    context.browser_session_runnable_id = script_id
                    context.browser_session_runnable_generation_id = expected_runnable_generation_id
                download_run_id = (
                    resolve_run_download_id(skyvern_context.current(), fallback_run_id=script_id) or script_id
                )
                LOG.info(
                    "Getting browser state for script",
                    browser_session_id=browser_session_id,
                )
                browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    browser_session_id,
                    **{
                        "organization_id": organization_id,
                        "expected_runnable_id": script_id,
                        "download_run_id": download_run_id,
                        **(
                            {"expected_runnable_generation_id": expected_runnable_generation_id}
                            if expected_runnable_generation_id is not None
                            else {}
                        ),
                    },
                )
                if browser_state is None:
                    # Fail closed: a cold/evicted session has no reusable state. Silently creating a local
                    # browser below would produce an unregistered state that terminal cleanup misclassifies as
                    # a reusable persistent session (keyed off browser_session_id) and leaks instead of closes.
                    raise MissingBrowserStateForBrowserSession(browser_session_id)
                self._store_session_lease(
                    script_id,
                    _PersistentSessionLease(
                        session_id=browser_session_id,
                        organization_id=organization_id,
                        runnable_id=script_id,
                        runnable_generation_id=expected_runnable_generation_id,
                        browser_state=browser_state,
                    ),
                )
            page = await browser_state.get_working_page()
            if not page:
                LOG.warning("Browser state has no page to run the script", script_id=script_id)
        proxy_location = ProxyLocation.RESIDENTIAL
        if not browser_state:
            browser_state = await self._create_browser_state(
                proxy_location=proxy_location,
                script_id=script_id,
                organization_id=organization_id,
            )

        if script_id:
            self.pages[script_id] = browser_state
        await browser_state.get_or_create_page(
            proxy_location=proxy_location,
            script_id=script_id,
        )

        return await _on_browser_state_acquired(browser_state, workflow_run_id)

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
        session_lease = self._persistent_session_leases.get(script_id)
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
                    await self._release_persistent_session(
                        browser_session_id,
                        organization_id,
                        session_lease,
                    )
                    LOG.info("Released browser session", browser_session_id=browser_session_id)
                except Exception:
                    LOG.warning(
                        "Failed to release browser session during script cleanup",
                        script_id=script_id,
                        browser_session_id=browser_session_id,
                        exc_info=True,
                    )
                finally:
                    # This is the script's only cleanup call, so a retained lease is never retried —
                    # it just reads as "still running" to the reaper's lease gate and pins the
                    # session. Drop it either way and leave the occupied row to the reaper.
                    self._discard_session_lease(script_id, session_lease)
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
