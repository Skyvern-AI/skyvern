from __future__ import annotations

import os

import structlog
from playwright.async_api import async_playwright

from skyvern.exceptions import MissingBrowserState
from skyvern.forge import app
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.webeye.browser_factory import BrowserContextFactory, BrowserState, VideoArtifact

LOG = structlog.get_logger()


class BrowserManager:
    instance = None
    pages: dict[str, BrowserState] = dict()

    def __new__(cls) -> BrowserManager:
        if cls.instance is None:
            cls.instance = super().__new__(cls)
        return cls.instance

    @staticmethod
    async def _create_browser_state(
        proxy_location: ProxyLocation | None = None,
        url: str | None = None,
        task_id: str | None = None,
        workflow_run_id: str | None = None,
        organization_id: str | None = None,
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
            organization_id=organization_id,
        )
        return BrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            browser_cleanup=browser_cleanup,
        )

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
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(browser_session_id)
            if browser_state is None:
                LOG.warning(
                    "Browser state not found in persistent sessions manager",
                    browser_session_id=browser_session_id,
                )
                raise MissingBrowserState(task_id=task.task_id)
            else:
                if task.organization_id:
                    await app.PERSISTENT_SESSIONS_MANAGER.occupy_browser_session(
                        browser_session_id,
                        organization_id=task.organization_id,
                        runnable_type="task",
                        runnable_id=task.task_id,
                    )
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
                organization_id=task.organization_id,
            )

        self.pages[task.task_id] = browser_state
        if task.workflow_run_id:
            self.pages[task.workflow_run_id] = browser_state

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        await browser_state.get_or_create_page(
            url=task.url, proxy_location=task.proxy_location, task_id=task.task_id, organization_id=task.organization_id
        )
        return browser_state

    async def get_or_create_for_workflow_run(
        self,
        workflow_run: WorkflowRun,
        url: str | None = None,
        browser_session_id: str | None = None,
    ) -> BrowserState:
        parent_workflow_run_id = workflow_run.parent_workflow_run_id
        workflow_run_id = workflow_run.workflow_run_id
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
            # TODO: what if there's a parent workflow run?
            LOG.info(
                "Getting browser state for workflow run from persistent sessions manager",
                browser_session_id=browser_session_id,
            )
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(browser_session_id)
            if browser_state is None:
                LOG.warning(
                    "Browser state not found in persistent sessions manager", browser_session_id=browser_session_id
                )
                raise MissingBrowserState(workflow_run_id=workflow_run.workflow_run_id)
            else:
                await app.PERSISTENT_SESSIONS_MANAGER.occupy_browser_session(
                    browser_session_id,
                    runnable_type="workflow_run",
                    runnable_id=workflow_run.workflow_run_id,
                    organization_id=workflow_run.organization_id,
                )
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
                organization_id=workflow_run.organization_id,
            )

        self.pages[workflow_run_id] = browser_state
        if parent_workflow_run_id:
            self.pages[parent_workflow_run_id] = browser_state

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        await browser_state.get_or_create_page(
            url=url,
            proxy_location=workflow_run.proxy_location,
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=workflow_run.organization_id,
        )
        return browser_state

    def get_for_workflow_run(
        self, workflow_run_id: str, parent_workflow_run_id: str | None = None
    ) -> BrowserState | None:
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

    @classmethod
    async def close(cls) -> None:
        LOG.info("Closing BrowserManager")
        for browser_state in cls.pages.values():
            await browser_state.close()
        cls.pages = dict()
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
    ) -> BrowserState | None:
        LOG.info("Cleaning up for workflow run")
        browser_state_to_close = self.pages.pop(workflow_run_id, None)
        if browser_state_to_close:
            # Stop tracing before closing the browser if tracing is enabled
            if browser_state_to_close.browser_context and browser_state_to_close.browser_artifacts.traces_dir:
                trace_path = f"{browser_state_to_close.browser_artifacts.traces_dir}/{workflow_run_id}.zip"
                await browser_state_to_close.browser_context.tracing.stop(path=trace_path)
                LOG.info("Stopped tracing", trace_path=trace_path)

            await browser_state_to_close.close(close_browser_on_completion=close_browser_on_completion)
        for task_id in task_ids:
            task_browser_state = self.pages.pop(task_id, None)
            if task_browser_state is None:
                continue
            try:
                await task_browser_state.close()
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
