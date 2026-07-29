import asyncio
import dataclasses
import enum
import time
import typing as t
import uuid

import structlog

import skyvern.services.browser_recording.state_machines as sm
from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
    WorkflowDefinitionYamlBlocksItem_Wait,
)
from skyvern.config import settings
from skyvern.forge.sdk.routes.streaming.channels import exfiltration as streaming_exfiltration
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEventSource
from skyvern.services.browser_recording.service import (
    Processor,
    deterministic_input_text_parameter_key,
    normalize_recording_block_label,
)
from skyvern.services.browser_recording.types import (
    Action,
    ActionBlockable,
    ActionInputText,
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

INTERPRETATION_DEBOUNCE_SECONDS = 0.15
INTERPRETATION_MAX_WAIT_SECONDS = 0.8
# events_to_actions is pure CPU and runs on the event loop; log when a single pass
# is slow enough to risk starving the raw-event/WebSocket path so we can decide
# whether to offload it to a thread.
EVENTS_TO_ACTIONS_SLOW_MS = 50.0
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
            timestamp_start=action.timestamp_start,
            timestamp_end=action.timestamp_end,
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
            timestamp_start=action.timestamp_start,
            timestamp_end=action.timestamp_end,
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
            timestamp_start=action.timestamp_start,
            timestamp_end=action.timestamp_end,
        )

    return None


def _action_display_text(action: Action) -> str:
    for text in action.target.texts or []:
        cleaned = " ".join(text.split())
        if cleaned:
            return cleaned[:80]

    return (action.target.tag_name or "element").lower()


_PLACEHOLDER_VERBS: dict[ActionKind, str] = {
    ActionKind.CLICK: "Click",
    ActionKind.HOVER: "Hover over",
    ActionKind.INPUT_TEXT: "Fill",
}


def _placeholder_step_from_action(
    *,
    browser_session_id: str,
    action_index: int,
    action: ActionBlockable,
) -> RecordingDraftStep:
    """
    A draft step built purely from exfiltrated event data, shown to the user
    immediately while LLM enrichment runs in the background.
    """
    step_id = f"{browser_session_id}-recording-step-{action_index}"
    text = _action_display_text(action)
    verb = _PLACEHOLDER_VERBS[action.kind]
    title = f"{verb} '{text}'"

    parameters: list[dict[str, t.Any]] = []
    parameter_keys: list[str] = []
    if isinstance(action, ActionInputText):
        parameter_key = deterministic_input_text_parameter_key(action)
        navigation_goal = f"{verb} '{text}' with {{{{ {parameter_key} }}}}."
        parameters = [{"key": parameter_key}]
        parameter_keys = [parameter_key]
    else:
        navigation_goal = f"{verb} '{text}'."

    return RecordingDraftStep(
        step_id=step_id,
        action_kind=action.kind,
        block_type="action",
        label=normalize_recording_block_label(f"{action.kind.value}_{text}", fallback=action.kind.value),
        title=title,
        navigation_goal=navigation_goal,
        status=RecordingDraftStepStatus.INTERPRETING,
        editable_fields=[
            RecordingDraftStepEditableField.LABEL,
            RecordingDraftStepEditableField.TITLE,
            RecordingDraftStepEditableField.NAVIGATION_GOAL,
        ],
        parameters=parameters,
        parameter_keys=parameter_keys,
        timestamp_start=action.timestamp_start,
        timestamp_end=action.timestamp_end,
    )


class RecordingInterpretationSession:
    def __init__(
        self,
        *,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        on_update: OnRecordingInterpretationUpdate,
        debounce_seconds: float = INTERPRETATION_DEBOUNCE_SECONDS,
        max_wait_seconds: float = INTERPRETATION_MAX_WAIT_SECONDS,
        deltas_enabled: bool = False,
        recording_attempt_id: str | None = None,
    ) -> None:
        self.browser_session_id = browser_session_id
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id
        self.on_update = on_update
        # Identifies the client-side recording attempt. The registry continues an
        # unfinished session across reconnects even when this differs (the client
        # lost its state, e.g. page reload — SKY-12429) and adopts the new id.
        self.recording_attempt_id = recording_attempt_id
        self.set_deltas_enabled(deltas_enabled)
        self.interpretation_session_id = str(uuid.uuid4())
        self.debounce_seconds = debounce_seconds
        self.max_wait_seconds = max_wait_seconds
        self.events: list[ExfiltratedEvent] = []
        self.steps: list[RecordingDraftStep] = []
        self.emitted_action_count = 0
        self.session_revision = 0
        self.pending = False
        self.finalized = False
        self._debounce_task: asyncio.Task[None] | None = None
        self._pending_since: float | None = None
        self._enrichment_tasks: set[asyncio.Task[None]] = set()
        self._enrichment_semaphore = asyncio.Semaphore(settings.RECORDING_ENRICHMENT_MAX_CONCURRENCY)
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
        self._capture_paused = False

    def reset_wait_capture(self) -> None:
        for machine in self._action_machines:
            if isinstance(machine, sm.Wait):
                machine.reset()

    def set_deltas_enabled(self, enabled: bool) -> None:
        # Deltas require both the client capability and the server kill switch.
        self._deltas_enabled = enabled and settings.RECORDING_INTERPRETATION_DELTAS_ENABLED

    def pause_capture(self) -> None:
        self._capture_paused = True
        self.reset_wait_capture()

    def resume_capture(self) -> None:
        self._capture_paused = False
        self.reset_wait_capture()
        # Enrichment deltas that landed while paused were dropped client-side, so
        # send an authoritative snapshot to resync on resume.
        if self.session_revision > 0:
            self._emit()

    def ingest_events(self, events: list[streaming_exfiltration.ExfiltratedEvent]) -> None:
        if self._capture_paused:
            return
        reified_events = streaming_events_to_recording_events(events)
        if not reified_events:
            return

        self.events.extend(reified_events)

        # Async materialization (e.g. console json_value round-trips) can append
        # events out of true order under load. Re-sort only the not-yet-interpreted
        # tail by capture order; never touch the processed prefix, whose emitted
        # actions are tracked by index. sorted() is stable, so legacy events without
        # a capture_seq (-1) keep their arrival order.
        tail_start = self._processed_event_count
        self.events[tail_start:] = sorted(self.events[tail_start:], key=lambda event: event.capture_seq)

        if not any(event_should_trigger_interpretation(event) for event in reified_events):
            return

        if self._pending_since is None:
            self._pending_since = time.monotonic()

        self.pending = True
        self.finalized = False
        # Pending ping: no step changed yet, just signal interpretation is in flight.
        self._emit(changed_steps=[])
        self._schedule_interpretation()

    def cancel(self) -> None:
        self._cancel_debounce()
        for task in list(self._enrichment_tasks):
            task.cancel()
        self._enrichment_tasks.clear()

    async def flush(self) -> list[RecordingDraftStep]:
        self._cancel_debounce()
        await self._interpret(finalized=True)
        return self.steps

    def _cancel_debounce(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None

    def _schedule_interpretation(self) -> None:
        self._cancel_debounce()

        delay = self.debounce_seconds
        if self._pending_since is not None:
            deadline = self._pending_since + self.max_wait_seconds
            delay = min(delay, max(0.0, deadline - time.monotonic()))

        self._debounce_task = asyncio.create_task(self._debounced_interpret(delay))

    async def _debounced_interpret(self, delay: float) -> None:
        # No finally-cleanup of self._debounce_task here: by the time a
        # cancelled task unwinds, the reference may already point at a newer
        # task, and clearing it would orphan that one.
        try:
            await asyncio.sleep(delay)
            await self._interpret(finalized=False)
        except asyncio.CancelledError:
            return

    async def _interpret(self, *, finalized: bool) -> None:
        async with self._interpret_lock:
            self._pending_since = None
            processor = Processor(
                self.browser_session_id,
                self.organization_id,
                self.workflow_permanent_id,
            )

            if self._processed_event_count < len(self.events):
                new_events = self.events[self._processed_event_count :]
                started_at = time.perf_counter()
                self._all_actions = processor.events_to_actions(
                    new_events,
                    machines=self._action_machines,
                    initial_actions=self._all_actions,
                )
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                if elapsed_ms >= EVENTS_TO_ACTIONS_SLOW_MS:
                    LOG.warning(
                        "Slow events_to_actions pass blocked the event loop",
                        browser_session_id=self.browser_session_id,
                        elapsed_ms=round(elapsed_ms, 1),
                        new_event_count=len(new_events),
                        total_action_count=len(self._all_actions),
                    )
                self._processed_event_count = len(self.events)

            new_actions = self._all_actions[self.emitted_action_count :]

            new_steps: list[RecordingDraftStep] = []
            for offset, action in enumerate(new_actions):
                action_index = self.emitted_action_count + offset
                draft_step = await self._step_from_action(processor, action_index, action)
                if draft_step:
                    draft_step.label = self._unique_step_label(draft_step.label, draft_step)
                    self.steps.append(draft_step)
                    new_steps.append(draft_step)

            self.emitted_action_count += len(new_actions)
            self.pending = False
            self.finalized = finalized and not self._enrichment_tasks
            self._emit(changed_steps=new_steps)

        if not finalized:
            return

        # Hold the finalized signal until in-flight LLM enrichment lands, so commit
        # paths never capture placeholder metadata. Emit a final snapshot so the
        # client's full step list is authoritative regardless of any dropped delta.
        await self._drain_enrichment()
        self.finalized = True
        self._emit()

    async def _step_from_action(
        self,
        processor: Processor,
        action_index: int,
        action: Action,
    ) -> RecordingDraftStep | None:
        """
        Build a draft step for the action immediately. Deterministic kinds
        (goto_url, wait) come back final; agent-action kinds come back as
        placeholders with LLM enrichment scheduled in the background.
        """
        if action.kind in (ActionKind.CLICK, ActionKind.HOVER, ActionKind.INPUT_TEXT):
            blockable = t.cast(ActionBlockable, action)
            step = _placeholder_step_from_action(
                browser_session_id=self.browser_session_id,
                action_index=action_index,
                action=blockable,
            )
            self._schedule_enrichment(processor, action_index, step, blockable)
            return step

        if action.kind in (ActionKind.URL_CHANGE, ActionKind.WAIT):
            block: OutputBlock
            if action.kind == ActionKind.URL_CHANGE:
                block = await processor.create_url_block(action)
            else:
                block = await processor.create_wait_block(action)

            return _draft_step_from_block(
                browser_session_id=self.browser_session_id,
                action_index=action_index,
                action=action,
                block=block,
            )

        LOG.warning(
            "Unknown action kind in live interpretation",
            action_kind=action.kind,
            browser_session_id=self.browser_session_id,
        )
        return None

    def _schedule_enrichment(
        self,
        processor: Processor,
        action_index: int,
        step: RecordingDraftStep,
        action: ActionBlockable,
    ) -> None:
        task = asyncio.create_task(self._enrich_step(processor, action_index, step, action))
        self._enrichment_tasks.add(task)
        task.add_done_callback(self._enrichment_tasks.discard)

    async def _enrich_step(
        self,
        processor: Processor,
        action_index: int,
        step: RecordingDraftStep,
        action: ActionBlockable,
    ) -> None:
        enriched: RecordingDraftStep | None = None

        try:
            async with self._enrichment_semaphore:
                block = await processor.create_action_block(action)
            enriched = _draft_step_from_block(
                browser_session_id=self.browser_session_id,
                action_index=action_index,
                action=action,
                block=block,
            )
        except Exception:
            LOG.exception(
                "Failed to enrich recording draft step; keeping deterministic placeholder",
                browser_session_id=self.browser_session_id,
                organization_id=self.organization_id,
                step_id=step.step_id,
            )

        if enriched:
            step.label = self._unique_step_label(enriched.label, step)
            step.title = enriched.title
            step.navigation_goal = enriched.navigation_goal
            step.parameters = enriched.parameters
            step.parameter_keys = enriched.parameter_keys

        step.status = RecordingDraftStepStatus.READY
        self._emit(changed_steps=[step])

    async def _drain_enrichment(self) -> None:
        while self._enrichment_tasks:
            await asyncio.gather(*list(self._enrichment_tasks), return_exceptions=True)

    def _unique_step_label(self, label: str, step: RecordingDraftStep) -> str:
        existing = {s.label for s in self.steps if s.step_id != step.step_id}
        if label not in existing:
            return label

        suffix = 2
        while f"{label}_{suffix}" in existing:
            suffix += 1
        return f"{label}_{suffix}"

    def emit_snapshot(self) -> None:
        if self.session_revision == 0:
            return

        update = RecordingInterpretationUpdate(
            interpretation_session_id=self.interpretation_session_id,
            session_revision=self.session_revision,
            steps=self.steps,
            pending=self.pending,
            finalized=self.finalized,
        )

        try:
            self.on_update(update)
        except Exception:
            LOG.exception(
                "Failed to emit recording interpretation snapshot",
                browser_session_id=self.browser_session_id,
                organization_id=self.organization_id,
            )

    def _emit(self, *, changed_steps: list[RecordingDraftStep] | None = None) -> None:
        # Snapshot when changed_steps is None (full steps, is_snapshot=True); delta
        # otherwise (only the changed steps), keeping each update O(1) instead of
        # re-sending the whole growing list. Both advance session_revision so the
        # client's staleness guard orders them.
        self.session_revision += 1
        # Send a delta only when the client understands them and a changed set was
        # given; otherwise fall back to a full snapshot (legacy-compatible).
        is_delta = self._deltas_enabled and changed_steps is not None
        update = RecordingInterpretationUpdate(
            interpretation_session_id=self.interpretation_session_id,
            session_revision=self.session_revision,
            steps=[] if is_delta else self.steps,
            changed_steps=changed_steps if is_delta else [],
            is_snapshot=not is_delta,
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
