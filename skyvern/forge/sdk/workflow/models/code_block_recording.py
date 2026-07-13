"""Hangs CodeBlock page actions off a container task so they render on the run page."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.code_block_recorder import RecordingPage, recorded_action_from_payload
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import Action

if TYPE_CHECKING:
    from playwright.async_api import Page

    from skyvern.forge.sdk.models import Step
    from skyvern.forge.sdk.schemas.tasks import Task
    from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
    from skyvern.forge.sdk.workflow.models.block import CodeBlock

LOG = structlog.get_logger()


class CodeBlockActionRecording:
    """Records the page operations of one CodeBlock execution and persists them against a container task."""

    def __init__(
        self,
        *,
        code_block: CodeBlock,
        page: Page,
        workflow_run_id: str,
        workflow_run_block_id: str,
        organization_id: str | None,
        workflow_run_context: WorkflowRunContext,
    ) -> None:
        self._code_block = code_block
        self._page = page
        self._workflow_run_id = workflow_run_id
        self._workflow_run_block_id = workflow_run_block_id
        self._organization_id = organization_id
        self._workflow_run_context = workflow_run_context
        self._task: Task | None = None
        self._step: Step | None = None
        self._workflow_run_block: WorkflowRunBlock | None = None
        self._screenshot_tasks: list[asyncio.Task[None]] = []
        self._recording_enabled = False
        self._finalized = False
        self.recording_page = RecordingPage(page, on_action=self._screenshot_sink)

    @property
    def task(self) -> Task | None:
        return self._task

    def recorded_actions(self) -> list[Action]:
        return self.recording_page.recorded_actions()

    def last_recorded_exception(self) -> BaseException | None:
        return self.recording_page.last_recorded_exception()

    async def create_task_and_step(self) -> None:
        """Create the task/step that can anchor recorded actions for prompt-bearing blocks."""
        if not self._code_block.prompt:
            return
        try:
            self._task, self._step = await app.agent.create_task_and_step_from_code_block(
                code_block=self._code_block,
                organization_id=self._organization_id,
                workflow_run_id=self._workflow_run_id,
                task_url=self._page.url,
            )
        except Exception:
            LOG.warning(
                "Failed to create code block recording task",
                workflow_run_block_id=self._workflow_run_block_id,
                exc_info=True,
            )

    async def link_block(self) -> None:
        """Point the run block at the container task so the run-page timeline join resolves its actions."""
        if self._task is None:
            return
        try:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=self._workflow_run_block_id,
                task_id=self._task.task_id,
                organization_id=self._organization_id,
            )
            self._recording_enabled = True
        except Exception:
            LOG.warning(
                "Failed to link code block recording task to workflow run block",
                workflow_run_block_id=self._workflow_run_block_id,
                exc_info=True,
            )
            await self.finalize(success=False)

    async def _screenshot_sink(self, action: Action) -> None:
        if not self._recording_enabled:
            return
        # page.screenshot() shares the CDP channel with the user's page calls, so it must run synchronously
        # in the user-await chain (a backgrounded capture races the next action and clips a mid-nav frame);
        # only the page-free S3 upload is deferred off the critical path.
        try:
            if self._workflow_run_block is None:
                self._workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                    workflow_run_block_id=self._workflow_run_block_id, organization_id=self._organization_id
                )
            run_block = self._workflow_run_block
            screenshot = await self._page.screenshot(timeout=settings.BROWSER_SCREENSHOT_TIMEOUT_MS)
        except Exception:
            LOG.warning(
                "Code block screenshot capture failed",
                workflow_run_block_id=self._workflow_run_block_id,
                exc_info=True,
            )
            return

        async def _upload() -> None:
            try:
                action.screenshot_artifact_id = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                    workflow_run_block=run_block,
                    artifact_type=ArtifactType.SCREENSHOT_ACTION,
                    data=screenshot,
                )
            except Exception:
                LOG.warning(
                    "Code block screenshot upload failed",
                    workflow_run_block_id=self._workflow_run_block_id,
                    exc_info=True,
                )

        self._screenshot_tasks.append(asyncio.create_task(_upload()))

    async def _drain_screenshots(self) -> None:
        if self._screenshot_tasks:
            await asyncio.gather(*self._screenshot_tasks, return_exceptions=True)

    async def persist(self, recorded: list[Action]) -> None:
        # Best-effort like the screenshot sink: recording must never change block outcome. Drain first so
        # each action's screenshot_artifact_id is set on the in-memory row before it is dumped.
        await self._drain_screenshots()
        if not recorded or not self._recording_enabled or self._task is None or self._step is None:
            return
        try:
            masked = self._workflow_run_context.mask_secrets_in_data([a.model_dump(mode="json") for a in recorded])
            for raw in masked:
                action = recorded_action_from_payload(raw)
                action.task_id = self._task.task_id
                action.step_id = self._step.step_id
                action.step_order = self._step.order
                action.organization_id = self._organization_id
                await app.DATABASE.workflow_params.create_action(action)
        except Exception:
            LOG.warning(
                "Failed to persist recorded code block actions",
                workflow_run_block_id=self._workflow_run_block_id,
                exc_info=True,
            )

    async def finalize(self, success: bool) -> None:
        # Finalize both task and step on every exit path (incl. CancelledError via a finally); idempotent.
        if self._task is None or self._finalized:
            return
        self._finalized = True
        try:
            await app.DATABASE.tasks.update_task(
                task_id=self._task.task_id,
                organization_id=self._organization_id,
                status=TaskStatus.completed if success else TaskStatus.failed,
            )
            if self._step is not None:
                await app.DATABASE.tasks.update_step(
                    task_id=self._task.task_id,
                    step_id=self._step.step_id,
                    status=StepStatus.completed if success else StepStatus.failed,
                    output=AgentStepOutput(action_results=[]) if success else None,
                    is_last=True,
                    organization_id=self._organization_id,
                )
        except Exception:
            LOG.warning(
                "Failed to finalize code block task status",
                workflow_run_block_id=self._workflow_run_block_id,
                exc_info=True,
            )
