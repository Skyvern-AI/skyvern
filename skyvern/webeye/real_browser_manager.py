from __future__ import annotations

import asyncio
import os

import structlog
from playwright.async_api import async_playwright

from skyvern.constants import is_loop_iteration_key, loop_iteration_key
from skyvern.exceptions import MissingBrowserState
from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.schemas.runs import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_artifacts import VideoArtifact
from skyvern.webeye.browser_factory import BrowserContextFactory
from skyvern.webeye.browser_manager import BrowserManager
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.real_browser_state import RealBrowserState

LOG = structlog.get_logger()


class RealBrowserManager(BrowserManager):
    def __init__(self) -> None:
        self.pages: dict[str, BrowserState] = {}
        # Lazily initialized inside an async context to avoid binding the lock
        # to the wrong event loop when RealBrowserManager is instantiated
        # during module import or test fixtures that haven't started a loop.
        self._loop_iteration_lock_instance: asyncio.Lock | None = None

    @property
    def _loop_iteration_lock(self) -> asyncio.Lock:
        if self._loop_iteration_lock_instance is None:
            self._loop_iteration_lock_instance = asyncio.Lock()
        return self._loop_iteration_lock_instance

    @staticmethod
    async def _create_browser_state(
        proxy_location: ProxyLocationInput = None,
        url: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        workflow_permanent_id: str | None = None,
        script_id: str | None = None,
        organization_id: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        browser_address: str | None = None,
        browser_profile_id: str | None = None,
    ) -> BrowserState:
        pw = await async_playwright().start()
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
            browser_address=browser_address,
            browser_profile_id=browser_profile_id,
        )
        return RealBrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            browser_cleanup=browser_cleanup,
        )

    def evict_page(self, page_id: str) -> None:
        self.pages.pop(page_id, None)

    def get_for_task(self, task_id: str, workflow_run_id: str | None = None) -> BrowserState | None:
        if task_id in self.pages:
            return self.pages[task_id]

        if workflow_run_id and workflow_run_id in self.pages:
            LOG.info(
                "Browser state for task not found. Using browser state for workflow run",
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

        if browser_state is None:
            LOG.info("Creating browser state for task", task_id=task.task_id)
            browser_state = await self._create_browser_state(
                proxy_location=task.proxy_location,
                url=task.url,
                task_id=task.task_id,
                workflow_permanent_id=task.workflow_permanent_id,
                organization_id=task.organization_id,
                extra_http_headers=task.extra_http_headers,
                browser_address=task.browser_address,
            )

            if browser_session_id:
                await app.PERSISTENT_SESSIONS_MANAGER.set_browser_state(
                    browser_session_id,
                    browser_state,
                )

        self.pages[task.task_id] = browser_state
        if task.workflow_run_id:
            self.pages[task.workflow_run_id] = browser_state

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        await browser_state.get_or_create_page(
            url=task.url,
            proxy_location=task.proxy_location,
            task_id=task.task_id,
            workflow_permanent_id=task.workflow_permanent_id,
            organization_id=task.organization_id,
            extra_http_headers=task.extra_http_headers,
            browser_address=task.browser_address,
        )
        return browser_state

    async def get_or_create_for_workflow_run(
        self,
        workflow_run: WorkflowRun,
        url: str | None = None,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
    ) -> BrowserState:
        parent_workflow_run_id = workflow_run.parent_workflow_run_id
        workflow_run_id = workflow_run.workflow_run_id
        if browser_profile_id is None:
            browser_profile_id = workflow_run.browser_profile_id

        # When running inside a parallel loop iteration, SkyvernContext holds an
        # iteration-specific browser_session_id (e.g. "wr_xxx__iter_0").  The
        # iteration browser was pre-created by get_or_create_for_loop_iteration()
        # and stored under that key.  Return it directly so child blocks (task,
        # action, etc.) use the isolated browser instead of racing to create one
        # under the bare workflow_run_id.
        ctx = skyvern_context.current()
        if ctx and ctx.browser_session_id and is_loop_iteration_key(ctx.browser_session_id):
            iteration_browser = self.pages.get(ctx.browser_session_id)
            if iteration_browser:
                # Navigate to the task URL if page is still on about:blank
                if url:
                    page = await iteration_browser.get_working_page()
                    if page and page.url == "about:blank":
                        await iteration_browser.navigate_to_url(page=page, url=url)
                LOG.debug(
                    "Returning iteration-specific browser state from parallel loop context",
                    workflow_run_id=workflow_run_id,
                    iteration_key=ctx.browser_session_id,
                )
                return iteration_browser

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
                page = await browser_state.get_working_page()
                if page:
                    if url:
                        await browser_state.navigate_to_url(page=page, url=url)
                else:
                    LOG.warning("Browser state has no page", workflow_run_id=workflow_run.workflow_run_id)

        if browser_state is None:
            LOG.info(
                "Creating browser state for workflow run",
                workflow_run_id=workflow_run.workflow_run_id,
            )
            browser_state = await self._create_browser_state(
                proxy_location=workflow_run.proxy_location,
                url=url,
                workflow_run_id=workflow_run.workflow_run_id,
                workflow_permanent_id=workflow_run.workflow_permanent_id,
                organization_id=workflow_run.organization_id,
                extra_http_headers=workflow_run.extra_http_headers,
                browser_address=workflow_run.browser_address,
                browser_profile_id=browser_profile_id,
            )

            if browser_session_id:
                await app.PERSISTENT_SESSIONS_MANAGER.set_browser_state(
                    browser_session_id,
                    browser_state,
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
        await browser_state.get_or_create_page(
            url=url,
            proxy_location=workflow_run.proxy_location,
            workflow_run_id=workflow_run.workflow_run_id,
            workflow_permanent_id=workflow_run.workflow_permanent_id,
            organization_id=workflow_run.organization_id,
            extra_http_headers=workflow_run.extra_http_headers,
            browser_address=workflow_run.browser_address,
            browser_profile_id=browser_profile_id,
        )
        return browser_state

    def get_for_workflow_run(
        self, workflow_run_id: str, parent_workflow_run_id: str | None = None
    ) -> BrowserState | None:
        # Check for parallel loop iteration browser via SkyvernContext first.
        # This mirrors the async get_or_create_for_workflow_run() so callers
        # like task block's non-first-task path get the correct iteration browser.
        ctx = skyvern_context.current()
        if ctx and ctx.browser_session_id and is_loop_iteration_key(ctx.browser_session_id):
            iteration_browser = self.pages.get(ctx.browser_session_id)
            if iteration_browser:
                return iteration_browser

        # Priority: parent first, then own entry.
        # Callers that need to avoid parent inheritance must omit parent_workflow_run_id.
        # See get_or_create_for_workflow_run() for the two-phase lookup pattern.
        if parent_workflow_run_id and parent_workflow_run_id in self.pages:
            return self.pages[parent_workflow_run_id]

        if workflow_run_id in self.pages:
            return self.pages[workflow_run_id]

        return None

    def set_video_artifact_for_task(self, task: Task, artifacts: list[VideoArtifact]) -> None:
        # Check parallel loop iteration browser first
        ctx = skyvern_context.current()
        if ctx and ctx.browser_session_id and is_loop_iteration_key(ctx.browser_session_id):
            iter_browser = self.pages.get(ctx.browser_session_id)
            if iter_browser:
                iter_browser.browser_artifacts.video_artifacts = artifacts
                return
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
        for browser_state in self.pages.values():
            await browser_state.close()
        self.pages = dict()
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
        browser_state_to_close = self.pages.pop(task_id, None)
        if browser_state_to_close:
            # Stop tracing before closing the browser if tracing is enabled
            if browser_state_to_close.browser_context and browser_state_to_close.browser_artifacts.traces_dir:
                trace_path = f"{browser_state_to_close.browser_artifacts.traces_dir}/{task_id}.zip"
                await browser_state_to_close.browser_context.tracing.stop(path=trace_path)
                LOG.info("Stopped tracing", trace_path=trace_path)
            await browser_state_to_close.close(close_browser_on_completion=close_browser_on_completion)
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
        LOG.info("Cleaning up for workflow run")
        browser_state_to_close = self.pages.get(workflow_run_id)

        # Pop child workflow_run entries first — these are orphaned because child
        # workflows skip clean_up_workflow. Must happen before the shared check
        # so the task loop can correctly detect when the browser is no longer shared.
        if child_workflow_run_ids:
            for child_id in child_workflow_run_ids:
                self.pages.pop(child_id, None)

        from skyvern.forge.sdk.routes.streaming.registries import set_deferred_close_params, stream_ref_active

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
                # Defer close until the last stream disconnects
                LOG.info(
                    "Deferring browser close — active CDP streams",
                    workflow_run_id=workflow_run_id,
                )
                set_deferred_close_params(workflow_run_id, close_browser_on_completion)
            else:
                await browser_state_to_close.close(close_browser_on_completion=effective_close)

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
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                )
            try:
                await task_browser_state.close(close_browser_on_completion=effective_close)
            except Exception:
                LOG.info(
                    "Failed to close the browser state from the task block, might because it's already closed.",
                    exc_info=True,
                    task_id=task_id,
                    workflow_run_id=workflow_run_id,
                )
        LOG.info("Workflow run is cleaned up")

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
    ) -> BrowserState:
        browser_state = self.get_for_script(script_id=script_id)
        if browser_state:
            return browser_state

        if browser_session_id:
            LOG.info(
                "Getting browser state for script",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                browser_session_id, organization_id=script_id
            )
            if browser_state is None:
                LOG.warning(
                    "Browser state not found in persistent sessions manager",
                    browser_session_id=browser_session_id,
                )
            else:
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

    def get_for_script(self, script_id: str | None = None) -> BrowserState | None:
        if script_id and script_id in self.pages:
            return self.pages[script_id]
        return None

    async def get_or_create_for_loop_iteration(
        self,
        workflow_run_id: str,
        loop_idx: int,
        organization_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> BrowserState:
        """Get or create an isolated BrowserContext for a parallel loop iteration.

        Each iteration gets its own browser context so cookies, auth state, and
        storage are fully isolated between concurrent iterations.

        Uses _loop_iteration_lock to prevent concurrent create_task coroutines
        from racing through the check-then-act on self.pages.
        """
        key = loop_iteration_key(workflow_run_id, loop_idx)

        async with self._loop_iteration_lock:
            if key in self.pages:
                return self.pages[key]

            # Persistent sessions cannot be aliased under per-iteration keys —
            # multiple iterations would race on the same live page. The caller
            # (execute_loop_helper) is expected to force sequential execution
            # when a persistent session is in use; this branch only runs if
            # that contract is bypassed, in which case we still create a fresh
            # isolated context to preserve correctness over the persistence.
            if browser_session_id:
                LOG.warning(
                    "Persistent browser session not used for parallel loop iteration — "
                    "creating isolated context to avoid cross-iteration races",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    browser_session_id=browser_session_id,
                )

            LOG.info(
                "Creating isolated browser state for loop iteration",
                workflow_run_id=workflow_run_id,
                loop_idx=loop_idx,
                key=key,
            )
            browser_state = await self._create_browser_state(
                workflow_run_id=workflow_run_id,
                organization_id=organization_id,
            )
            self.pages[key] = browser_state

        # Page creation can happen outside the lock — the key is already
        # reserved in self.pages so no other coroutine will race on it.
        await browser_state.get_or_create_page(
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return browser_state

    async def cleanup_loop_iterations(
        self,
        workflow_run_id: str,
        loop_indices: list[int],
        organization_id: str | None = None,
    ) -> None:
        """Close and remove browser states for the given parallel loop iterations.

        Uses _loop_iteration_lock so cleanup cannot race with create or with
        a concurrent cleanup call from another batch.
        """
        # Collect entries to close under the lock, then close outside it
        # to avoid holding the lock during potentially slow browser teardown.
        to_close: list[tuple[int, BrowserState]] = []
        async with self._loop_iteration_lock:
            for loop_idx in loop_indices:
                key = loop_iteration_key(workflow_run_id, loop_idx)
                browser_state = self.pages.pop(key, None)
                if browser_state is None:
                    continue
                # Only close if no other entry still references the same object
                shared = any(bs is browser_state for bs in self.pages.values())
                if not shared:
                    to_close.append((loop_idx, browser_state))

        for loop_idx, browser_state in to_close:
            try:
                await browser_state.close()
            except Exception:
                LOG.warning(
                    "Failed to close loop iteration browser state",
                    workflow_run_id=workflow_run_id,
                    loop_idx=loop_idx,
                    exc_info=True,
                )
