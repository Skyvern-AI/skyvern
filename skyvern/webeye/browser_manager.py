from __future__ import annotations

from typing import Protocol

import structlog

from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.webeye.browser_artifacts import VideoArtifact
from skyvern.webeye.browser_state import BrowserState

LOG = structlog.get_logger()


class BrowserManager(Protocol):
    pages: dict[str, BrowserState]

    async def get_or_create_for_task(self, task: Task, browser_session_id: str | None = None) -> BrowserState: ...

    async def get_or_create_for_workflow_run(
        self,
        workflow_run: WorkflowRun,
        url: str | None = None,
        browser_session_id: str | None = None,
        browser_profile_id: str | None = None,
    ) -> BrowserState: ...

    async def cleanup_for_task(
        self,
        task_id: str,
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState | None: ...

    async def cleanup_for_workflow_run(
        self,
        workflow_run_id: str,
        task_ids: list[str],
        close_browser_on_completion: bool = True,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserState | None: ...

    async def get_or_create_for_script(
        self,
        script_id: str | None = None,
        browser_session_id: str | None = None,
    ) -> BrowserState: ...

    def get_for_task(self, task_id: str, workflow_run_id: str | None = None) -> BrowserState | None: ...

    def get_for_workflow_run(
        self,
        workflow_run_id: str,
        parent_workflow_run_id: str | None = None,
    ) -> BrowserState | None: ...

    def get_for_script(self, script_id: str | None = None) -> BrowserState | None: ...

    def set_video_artifact_for_task(self, task: Task, artifacts: list[VideoArtifact]) -> None: ...

    async def get_video_artifacts(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> list[VideoArtifact]: ...

    async def get_har_data(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> bytes: ...

    async def get_browser_console_log(
        self,
        browser_state: BrowserState,
        task_id: str = "",
        workflow_id: str = "",
        workflow_run_id: str = "",
    ) -> bytes: ...
