"""Hangs CodeBlock page actions off a container task so they render on the run page."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.models import StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.code_block_recorder import (
    RECORDED_FAILURE_RESPONSE_MAX_CHARS,
    PendingAction,
    PlaywrightInputDefaults,
    RecordingPage,
    recorded_action_from_payload,
)
from skyvern.forge.sdk.workflow.models.credential_release import CredentialReleaseGuard
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.browser_diagnostics import schedule_control_endpoint_diagnostics

if TYPE_CHECKING:
    from playwright.async_api import Page

    from skyvern.forge.sdk.models import Step
    from skyvern.forge.sdk.schemas.tasks import Task
    from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
    from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
    from skyvern.forge.sdk.workflow.models.block import CodeBlock

LOG = structlog.get_logger()

# Bounds on control-endpoint probing: enough to catch a wedge that develops after an
# early benign failure, few enough that a sustained wedge cannot pile attaches onto the
# browser under suspicion.
MAX_CONTROL_ENDPOINT_PROBES = 3
CONTROL_ENDPOINT_PROBE_INTERVAL_SECONDS = 30.0

_RECORDER_OWNED_ACTION_FIELDS = (
    "action_id",
    "source_action_id",
    "action_type",
    "status",
    "action_order",
    "screenshot_artifact_id",
    "started_at",
    "finished_at",
    "created_at",
    "modified_at",
)


def _null_redacted_scalars(original: Any, masked: Any) -> Any:
    if isinstance(original, bool | int | float) and isinstance(masked, str):
        return None
    if isinstance(original, dict) and isinstance(masked, dict):
        return {
            key: _null_redacted_scalars(original[key], value) if key in original else value
            for key, value in masked.items()
        }
    if isinstance(original, list) and isinstance(masked, list):
        return [
            _null_redacted_scalars(original[index], value) if index < len(original) else value
            for index, value in enumerate(masked)
        ]
    return masked


def _page_closed(page: Page) -> bool | None:
    # Separates "page was already gone" from "page was alive but hung" — a screenshot TimeoutError
    # alone cannot tell them apart. Must not raise: the caller is an except handler whose remaining
    # work still has to persist the action row.
    try:
        return page.is_closed()
    except Exception:
        return None


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
        redaction_parameters: dict[str, object] | None = None,
        credential_release_guard: CredentialReleaseGuard | None = None,
        strategy_aware_typing: bool = False,
        playwright_input_defaults: PlaywrightInputDefaults | None = None,
    ) -> None:
        self._code_block = code_block
        self._page = page
        self._workflow_run_id = workflow_run_id
        self._workflow_run_block_id = workflow_run_block_id
        self._organization_id = organization_id
        self._workflow_run_context = workflow_run_context
        self._redaction_parameters = redaction_parameters or {}
        self._task: Task | None = None
        self._step: Step | None = None
        self._workflow_run_block: WorkflowRunBlock | None = None
        self._screenshot_tasks: list[asyncio.Task[None]] = []
        self._control_endpoint_probes = 0
        self._last_control_endpoint_probe: float | None = None
        self._recorded_action_metadata: dict[int, dict[str, object]] = {}
        self._recording_enabled = False
        self._finalized = False
        self.recording_page = RecordingPage(
            page,
            on_action=self._recorded_action_sink,
            on_pending_action=self._page_call_still_pending,
            credential_release_guard=credential_release_guard,
            strategy_aware_typing=strategy_aware_typing,
            playwright_input_defaults=playwright_input_defaults,
        )

    def set_redaction_parameters(self, parameters: dict[str, object]) -> None:
        self._redaction_parameters = parameters

    @property
    def task(self) -> Task | None:
        return self._task

    def recorded_actions(self) -> list[Action]:
        return self.recording_page.recorded_actions()

    def last_recorded_exception(self) -> BaseException | None:
        return self.recording_page.last_recorded_exception()

    def _page_call_still_pending(self, pending_action: PendingAction) -> None:
        LOG.warning(
            "codeblock.page_call_still_pending",
            workflow_run_id=self._workflow_run_id,
            workflow_run_block_id=self._workflow_run_block_id,
            block_label=self._code_block.label,
            action_type=pending_action.action_type.value if pending_action.action_type is not None else None,
            action_order=pending_action.action_order,
            code_line=pending_action.code_line,
            call_name=pending_action.call_name,
            threshold_seconds=pending_action.threshold_seconds,
        )
        # The other way the browser invariant fails: a page call that never returns. The
        # recorded-action sink runs in the caller's finally, so it never fires here, and
        # this is the only signal on that path. Shares the probe budget with the sink.
        self._probe_control_endpoint("Code block browser control endpoint probed while a page call was pending")

    def _probe_control_endpoint(self, message: str) -> None:
        """Probe under a cap and a minimum interval, shared across both trigger paths.

        A hard one-shot is spent by whichever failure comes first, and the pending-call
        watchdog fires for any call outstanding at PENDING_CALL_DELAY_SECONDS -- so one
        benign slow navigation would consume the budget and leave a later wedge unprobed.
        """
        if self._control_endpoint_probes >= MAX_CONTROL_ENDPOINT_PROBES:
            return
        now = time.monotonic()
        if self._last_control_endpoint_probe is not None:
            if now - self._last_control_endpoint_probe < CONTROL_ENDPOINT_PROBE_INTERVAL_SECONDS:
                return
        self._control_endpoint_probes += 1
        self._last_control_endpoint_probe = now
        schedule_control_endpoint_diagnostics(
            self._page,
            message,
            workflow_run_block_id=self._workflow_run_block_id,
            probe_sequence=self._control_endpoint_probes,
        )

    def _remember_action_metadata(self, action: Action) -> dict[str, object]:
        metadata = self._recorded_action_metadata.get(id(action))
        if metadata is None:
            payload = action.model_dump(mode="json", warnings=False)
            metadata = {field: payload[field] for field in _RECORDER_OWNED_ACTION_FIELDS}
            model_fields = type(action).model_fields
            for field in payload.keys() - action.model_fields_set:
                model_field = model_fields.get(field)
                if model_field is not None and model_field.default_factory is None and not model_field.is_required():
                    metadata[field] = model_field.get_default(call_default_factory=False)
            self._recorded_action_metadata[id(action)] = metadata
        return metadata

    async def create_task_and_step(self) -> None:
        """Create the container task/step that anchors recorded actions (promptless blocks included)."""
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
            )
            await self.finalize(success=False)

    async def _recorded_action_sink(self, action: Action) -> None:
        # Fires as each action completes: capture its screenshot, then stream the row so consumers
        # (run timeline, copilot narration) see it mid-block instead of only at block end.
        if not self._recording_enabled:
            return
        metadata = self._remember_action_metadata(action)
        # page.screenshot() shares the CDP channel with the user's page calls, so it must run synchronously
        # in the user-await chain (a backgrounded capture races the next action and clips a mid-nav frame);
        # only the page-free S3 upload is deferred off the critical path.
        capture_started = False
        try:
            if self._workflow_run_block is None:
                self._workflow_run_block = await app.DATABASE.observer.get_workflow_run_block(
                    workflow_run_block_id=self._workflow_run_block_id, organization_id=self._organization_id
                )
            run_block = self._workflow_run_block
            capture_started = True
            screenshot = await self._page.screenshot(timeout=settings.CODE_BLOCK_RECORDING_SCREENSHOT_TIMEOUT_MS)
        except Exception:
            LOG.warning(
                "Code block screenshot capture failed",
                workflow_run_block_id=self._workflow_run_block_id,
                page_closed=_page_closed(self._page),
            )
            # Only a failed capture says anything about the browser; a database error above
            # would otherwise emit probe fields describing a browser that is perfectly fine.
            if capture_started:
                self._probe_control_endpoint("Code block browser control endpoint probed after screenshot failure")
        else:

            async def _upload() -> None:
                try:
                    artifact_id = await app.ARTIFACT_MANAGER.create_workflow_run_block_artifact(
                        workflow_run_block=run_block,
                        artifact_type=ArtifactType.SCREENSHOT_ACTION,
                        data=screenshot,
                    )
                    action.screenshot_artifact_id = artifact_id
                    metadata["screenshot_artifact_id"] = artifact_id
                except Exception:
                    LOG.warning(
                        "Code block screenshot upload failed",
                        workflow_run_block_id=self._workflow_run_block_id,
                    )

            self._screenshot_tasks.append(asyncio.create_task(_upload()))

        # Streamed row carries no screenshot yet (upload deferred); the end-of-block batch upserts it in.
        await self._persist_action(action, metadata)

    async def _persist_action(self, action: Action, metadata: dict[str, object]) -> None:
        # Best-effort like the screenshot sink: recording must never change block outcome. Both the streamed
        # write and the end-of-block batch route through here and upsert on action_id, so they converge on
        # one row instead of double-inserting.
        if not self._recording_enabled or self._task is None or self._step is None:
            return
        try:
            payload = action.model_dump(mode="json", warnings=False)
            masked = self._workflow_run_context.mask_secrets_in_data([payload])[0]
            masked = app.AGENT_FUNCTION.redact_codeblock_parameter_values(masked, self._redaction_parameters)
            # A matching bool/number becomes a string placeholder, which typed Action fields reject.
            # Null it recursively so subclass defaults cannot silently restore user-controlled values.
            masked = _null_redacted_scalars(payload, masked) if isinstance(masked, dict) else {}
            response = masked.get("response")
            if isinstance(response, str):
                masked["response"] = response[:RECORDED_FAILURE_RESPONSE_MAX_CHARS]
            # IDs, timestamps, and implicit model defaults come from a snapshot taken before user
            # code can mutate the action. Explicit page/action fields remain masked.
            masked.update(metadata)
            persisted = recorded_action_from_payload(masked)
            persisted.task_id = self._task.task_id
            persisted.step_id = self._step.step_id
            persisted.step_order = self._step.order
            persisted.organization_id = self._organization_id
            await app.DATABASE.workflow_params.upsert_recorded_action(persisted)
        except Exception:
            LOG.warning(
                "Failed to persist recorded code block action",
                workflow_run_block_id=self._workflow_run_block_id,
                action_order=metadata.get("action_order"),
            )

    async def _drain_screenshots(self) -> None:
        if self._screenshot_tasks:
            await asyncio.gather(*self._screenshot_tasks, return_exceptions=True)

    async def persist(self, recorded: list[Action]) -> None:
        # Drain first so each action's screenshot_artifact_id is set on the in-memory row before its final
        # upsert backfills the screenshot the streamed write couldn't include.
        await self._drain_screenshots()
        if not recorded or not self._recording_enabled or self._task is None or self._step is None:
            return
        for action in recorded:
            await self._persist_action(action, self._remember_action_metadata(action))

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
            )
        if not success or self._step is None:
            return
        # Billing counts the action rows persist() wrote, so this must stay after persist on every
        # success path; best-effort like the rest of recording — never change the block outcome.
        try:
            await app.AGENT_FUNCTION.post_code_block_execution(self._task, self._step)
        except Exception:
            LOG.warning(
                "Code block billing hook failed",
                workflow_run_block_id=self._workflow_run_block_id,
                task_id=self._task.task_id,
            )
        await self._record_final_url()

    async def _record_final_url(self) -> None:
        """Persist the page this block ended on; the failure path records the same column."""
        if not self._organization_id:
            return
        try:
            page_url = self._page.url
            final_url = self._workflow_run_context.mask_secrets_in_data(page_url)
        except Exception:
            LOG.warning(
                "Failed to read the end URL of a code block",
                workflow_run_block_id=self._workflow_run_block_id,
            )
            return
        if not isinstance(final_url, str) or not final_url:
            return
        if final_url != page_url:
            # Masking altering the URL means a login/MFA step left a credential or OTP in the query
            # string. The masked form is not a page anything can be resumed against, so record none.
            return
        try:
            await app.DATABASE.observer.update_workflow_run_block(
                workflow_run_block_id=self._workflow_run_block_id,
                organization_id=self._organization_id,
                final_url=final_url,
            )
        except Exception:
            LOG.warning(
                "Failed to persist the end URL of a code block",
                workflow_run_block_id=self._workflow_run_block_id,
            )
