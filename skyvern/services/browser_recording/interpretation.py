import asyncio
import dataclasses
import enum
import typing as t

import structlog

import skyvern.services.browser_recording.state_machines as sm
from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
    WorkflowDefinitionYamlBlocksItem_Wait,
)
from skyvern.forge.sdk.routes.streaming.channels import exfiltration as streaming_exfiltration
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEventSource
from skyvern.services.browser_recording.service import Processor
from skyvern.services.browser_recording.types import (
    Action,
    ActionKind,
    ExfiltratedCdpEvent,
    ExfiltratedConsoleEvent,
    ExfiltratedEvent,
    OutputBlock,
    RecordingDraftStep,
    RecordingDraftStepEditableField,
    RecordingDraftStepStatus,
    RecordingInterpretationUpdate,
)

LOG = structlog.get_logger(__name__)

INTERPRETATION_DEBOUNCE_SECONDS = 1.5
SIGNIFICANT_CONSOLE_EVENT_TYPES = {
    "blur",
    "change",
    "click",
    "focus",
    "input",
    "keydown",
    "keypress",
}

OnRecordingInterpretationUpdate = t.Callable[[RecordingInterpretationUpdate], None]


def _source_value(source: ExfiltratedEventSource | str) -> str:
    if isinstance(source, enum.Enum):
        return str(source.value)
    return str(source)


def streaming_events_to_recording_events(
    events: list[streaming_exfiltration.ExfiltratedEvent],
) -> list[ExfiltratedEvent]:
    reified_events: list[ExfiltratedEvent] = []

    for event in events:
        source = _source_value(event.source)
        payload = dataclasses.asdict(event)
        payload["source"] = source

        try:
            if source == "cdp":
                reified_events.append(ExfiltratedCdpEvent(**payload))
            elif source == "console":
                reified_events.append(ExfiltratedConsoleEvent(**payload))
            else:
                LOG.debug("Skipping recording interpretation event with unsupported source", source=source)
        except Exception:
            LOG.debug(
                "Skipping recording interpretation event that could not be reified",
                source=source,
                event_name=event.event_name,
                exc_info=True,
            )

    return reified_events


def event_should_trigger_interpretation(event: ExfiltratedEvent) -> bool:
    if isinstance(event, ExfiltratedCdpEvent):
        return event.event_name.startswith("nav:")

    return event.params.type in SIGNIFICANT_CONSOLE_EVENT_TYPES


def _extra_field(block: OutputBlock, field_name: str, fallback: t.Any) -> t.Any:
    value = getattr(block, field_name, None)
    if value is not None:
        return value

    model_extra = getattr(block, "model_extra", None)
    if isinstance(model_extra, dict) and field_name in model_extra:
        return model_extra[field_name]

    return fallback


def _draft_step_from_block(
    *,
    browser_session_id: str,
    action_index: int,
    action: Action,
    block: OutputBlock,
) -> RecordingDraftStep | None:
    step_id = f"{browser_session_id}-recording-step-{action_index}"

    if isinstance(block, WorkflowDefinitionYamlBlocksItem_Action):
        return RecordingDraftStep(
            step_id=step_id,
            action_kind=action.kind,
            block_type="action",
            label=block.label,
            title=block.title,
            navigation_goal=block.navigation_goal,
            url=action.url,
            status=RecordingDraftStepStatus.READY,
            editable_fields=[
                RecordingDraftStepEditableField.LABEL,
                RecordingDraftStepEditableField.TITLE,
                RecordingDraftStepEditableField.NAVIGATION_GOAL,
            ],
            parameters=_extra_field(block, "parameters", []),
            parameter_keys=block.parameter_keys or [],
        )

    if isinstance(block, WorkflowDefinitionYamlBlocksItem_GotoUrl):
        return RecordingDraftStep(
            step_id=step_id,
            action_kind=ActionKind.URL_CHANGE,
            block_type="goto_url",
            label=block.label,
            url=block.url,
            status=RecordingDraftStepStatus.READY,
            editable_fields=[
                RecordingDraftStepEditableField.LABEL,
                RecordingDraftStepEditableField.URL,
            ],
        )

    if isinstance(block, WorkflowDefinitionYamlBlocksItem_Wait):
        return RecordingDraftStep(
            step_id=step_id,
            action_kind=ActionKind.WAIT,
            block_type="wait",
            label=block.label,
            wait_sec=block.wait_sec,
            status=RecordingDraftStepStatus.READY,
            editable_fields=[
                RecordingDraftStepEditableField.LABEL,
                RecordingDraftStepEditableField.WAIT_SEC,
            ],
        )

    return None


class RecordingInterpretationSession:
    def __init__(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        on_update: OnRecordingInterpretationUpdate,
        debounce_seconds: float = INTERPRETATION_DEBOUNCE_SECONDS,
    ) -> None:
        self.browser_session_id = browser_session_id
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id
        self.on_update = on_update
        self.debounce_seconds = debounce_seconds
        self.events: list[ExfiltratedEvent] = []
        self.steps: list[RecordingDraftStep] = []
        self.emitted_action_count = 0
        self.session_revision = 0
        self.pending = False
        self.finalized = False
        self._debounce_task: asyncio.Task[None] | None = None
        self._interpret_lock = asyncio.Lock()
        self._action_machines: list[sm.StateMachine] = [
            sm.Click(),
            sm.Hover(),
            sm.InputText(),
            sm.UrlChange(),
            sm.Wait(),
        ]
        self._all_actions: list[Action] = []
        self._processed_event_count = 0

    def ingest_events(self, events: list[streaming_exfiltration.ExfiltratedEvent]) -> None:
        reified_events = streaming_events_to_recording_events(events)
        if not reified_events:
            return

        self.events.extend(reified_events)

        if not any(event_should_trigger_interpretation(event) for event in reified_events):
            return

        self.pending = True
        self.finalized = False
        self._emit_update()
        self._schedule_interpretation()

    def cancel(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None

    async def flush(self) -> list[RecordingDraftStep]:
        self.cancel()
        await self._interpret(finalized=True)
        return self.steps

    def _schedule_interpretation(self) -> None:
        self.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_interpret())

    async def _debounced_interpret(self) -> None:
        try:
            await asyncio.sleep(self.debounce_seconds)
            await self._interpret(finalized=False)
        except asyncio.CancelledError:
            return
        finally:
            self._debounce_task = None

    async def _interpret(self, *, finalized: bool) -> None:
        async with self._interpret_lock:
            processor = Processor(
                self.browser_session_id,
                self.organization_id,
                self.workflow_permanent_id,
            )

            if self._processed_event_count < len(self.events):
                new_events = self.events[self._processed_event_count :]
                self._all_actions = processor.events_to_actions(
                    new_events,
                    machines=self._action_machines,
                    initial_actions=self._all_actions,
                )
                self._processed_event_count = len(self.events)

            actions = self._all_actions
            new_actions = actions[self.emitted_action_count :]

            if not new_actions:
                self.pending = False
                self.finalized = finalized
                self._emit_update()
                return

            try:
                blocks = await processor.actions_to_blocks(new_actions)
            except Exception:
                self.pending = False
                self.finalized = finalized
                self._emit_update()
                LOG.exception(
                    "Failed to interpret live browser recording actions",
                    browser_session_id=self.browser_session_id,
                    organization_id=self.organization_id,
                    workflow_permanent_id=self.workflow_permanent_id,
                )
                return

            if len(blocks) < len(new_actions):
                LOG.warning(
                    "Live interpretation produced fewer blocks than actions",
                    browser_session_id=self.browser_session_id,
                    action_count=len(new_actions),
                    block_count=len(blocks),
                )

            for offset, (action, block) in enumerate(zip(new_actions, blocks, strict=False)):
                action_index = self.emitted_action_count + offset
                draft_step = _draft_step_from_block(
                    browser_session_id=self.browser_session_id,
                    action_index=action_index,
                    action=action,
                    block=block,
                )
                if draft_step:
                    self.steps.append(draft_step)

            self.emitted_action_count += len(blocks)
            self.pending = False
            self.finalized = finalized
            self._emit_update()

    def _emit_update(self) -> None:
        self.session_revision += 1
        update = RecordingInterpretationUpdate(
            session_revision=self.session_revision,
            steps=self.steps,
            pending=self.pending,
            finalized=self.finalized,
        )

        try:
            self.on_update(update)
        except Exception:
            LOG.exception(
                "Failed to emit recording interpretation update",
                browser_session_id=self.browser_session_id,
                organization_id=self.organization_id,
            )
