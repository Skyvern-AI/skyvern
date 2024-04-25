from __future__ import annotations

import random

import structlog
from playwright.async_api import Browser, Playwright, async_playwright

from skyvern.exceptions import MissingBrowserState
from skyvern.forge.sdk.schemas.tasks import ProxyLocation, Task
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.webeye.browser_factory import BrowserContextFactory, BrowserState

# import random


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
        proxy_location: ProxyLocation | None = None, url: str | None = None, new_context_tree: bool = False
    ) -> BrowserState:
        pw = await async_playwright().start()
        browser_context, browser_artifacts = await BrowserContextFactory.create_browser_context(
            pw, proxy_location=proxy_location, url=url
        )
        return BrowserState(
            pw=pw,
            browser_context=browser_context,
            page=None,
            browser_artifacts=browser_artifacts,
            new_context_tree=new_context_tree,
        )

    async def get_or_create_for_task(self, task: Task) -> BrowserState:
        if task.task_id in self.pages:
            return self.pages[task.task_id]
        elif task.workflow_run_id in self.pages:
            LOG.info(
                "Browser state for task not found. Using browser state for workflow run",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
            )
            self.pages[task.task_id] = self.pages[task.workflow_run_id]
            return self.pages[task.task_id]

        # TODO: percentage (50%) to use new context tree
        new_ctx = random.choices([False, True], weights=[0.5, 0.5], k=1)[0]
        LOG.info("Creating browser state for task", task_id=task.task_id, new_ctx=new_ctx)
        browser_state = await self._create_browser_state(task.proxy_location, task.url, new_ctx)

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        await browser_state.get_or_create_page(task.url)

        self.pages[task.task_id] = browser_state
        if task.workflow_run_id:
            self.pages[task.workflow_run_id] = browser_state
        return browser_state

    async def get_or_create_for_workflow_run(self, workflow_run: WorkflowRun, url: str | None = None) -> BrowserState:
        if workflow_run.workflow_run_id in self.pages:
            return self.pages[workflow_run.workflow_run_id]
        LOG.info("Creating browser state for workflow run", workflow_run_id=workflow_run.workflow_run_id)
        browser_state = await self._create_browser_state(workflow_run.proxy_location, url=url)

        # The URL here is only used when creating a new page, and not when using an existing page.
        # This will make sure browser_state.page is not None.
        await browser_state.get_or_create_page(url)

        self.pages[workflow_run.workflow_run_id] = browser_state
        return browser_state

    def set_video_artifact_for_task(self, task: Task, artifact_id: str) -> None:
        if task.workflow_run_id and task.workflow_run_id in self.pages:
            if self.pages[task.workflow_run_id].browser_artifacts.video_artifact_id:
                LOG.warning(
                    "Video artifact is already set for workflow run. Overwriting",
                    workflow_run_id=task.workflow_run_id,
                    old_artifact_id=self.pages[task.workflow_run_id].browser_artifacts.video_artifact_id,
                    new_artifact_id=artifact_id,
                )
            self.pages[task.workflow_run_id].browser_artifacts.video_artifact_id = artifact_id
            return
        if task.task_id in self.pages:
            if self.pages[task.task_id].browser_artifacts.video_artifact_id:
                LOG.warning(
                    "Video artifact is already set for task. Overwriting",
                    task_id=task.task_id,
                    old_artifact_id=self.pages[task.task_id].browser_artifacts.video_artifact_id,
                    new_artifact_id=artifact_id,
                )
            self.pages[task.task_id].browser_artifacts.video_artifact_id = artifact_id
            return

        raise MissingBrowserState(task_id=task.task_id)

    async def get_video_data(
        self, browser_state: BrowserState, task_id: str = "", workflow_id: str = "", workflow_run_id: str = ""
    ) -> bytes:
        if browser_state:
            path = browser_state.browser_artifacts.video_path
            if path:
                try:
                    with open(path, "rb") as f:
                        return f.read()
                except FileNotFoundError:
                    pass
        LOG.warning(
            "Video data not found for task", task_id=task_id, workflow_id=workflow_id, workflow_run_id=workflow_run_id
        )
        return b""

    async def get_har_data(
        self, browser_state: BrowserState, task_id: str = "", workflow_id: str = "", workflow_run_id: str = ""
    ) -> bytes:
        if browser_state:
            path = browser_state.browser_artifacts.har_path
            if path:
                with open(path, "rb") as f:
                    return f.read()
        LOG.warning(
            "HAR data not found for task", task_id=task_id, workflow_id=workflow_id, workflow_run_id=workflow_run_id
        )
        return b""

    @classmethod
    async def connect_to_scraping_browser(cls, pw: Playwright) -> Browser:
        if not SettingsManager.get_settings().REMOTE_BROWSER_KEY:
            raise Exception("REMOTE_BROWSER_KEY is empty. Cannot connect to remote browser.")
        browser = await pw.chromium.connect_over_cdp(SettingsManager.get_settings().REMOTE_BROWSER_KEY)
        LOG.info("Connected to remote browser", browser_type=SettingsManager.get_settings().BROWSER_TYPE)
        return browser

    @classmethod
    async def close(cls) -> None:
        LOG.info("Closing BrowserManager")
        for browser_state in cls.pages.values():
            await browser_state.close()
        cls.pages = dict()
        LOG.info("BrowserManger is closed")

    async def cleanup_for_task(self, task_id: str, close_browser_on_completion: bool = True) -> BrowserState | None:
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

        return browser_state_to_close

    async def cleanup_for_workflow_run(
        self, workflow_run_id: str, task_ids: list[str], close_browser_on_completion: bool = True
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
            self.pages.pop(task_id, None)
        LOG.info("Workflow run is cleaned up")

        return browser_state_to_close
