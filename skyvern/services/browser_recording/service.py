import asyncio
import base64
import json
import pathlib
import re
import typing as t
import zlib
from urllib.parse import urlparse

import structlog

import skyvern.services.browser_recording.state_machines as sm
from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
    WorkflowDefinitionYamlBlocksItem_Wait,
)
from skyvern.client.types.workflow_definition_yaml_parameters_item import WorkflowDefinitionYamlParametersItem_Workflow
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.services.browser_recording.types import (
    Action,
    ActionBlockable,
    ActionInputText,
    ActionKind,
    ActionUrlChange,
    ActionWait,
    ExfiltratedCdpEvent,
    ExfiltratedConsoleEvent,
    ExfiltratedEvent,
    OutputBlock,
    RecordingDraftStep,
)


def summarize_exfiltrated_recording_events(events: list[ExfiltratedEvent]) -> dict[str, t.Any]:
    cdp_by_event_name: dict[str, int] = {}
    console_by_dom_type: dict[str, int] = {}
    console_by_exfil_event_name: dict[str, int] = {}
    cdp_total = 0
    console_total = 0

    for ev in events:
        if isinstance(ev, ExfiltratedCdpEvent):
            cdp_total += 1
            cdp_by_event_name[ev.event_name] = cdp_by_event_name.get(ev.event_name, 0) + 1
        elif isinstance(ev, ExfiltratedConsoleEvent):
            console_total += 1
            dom_type = ev.params.type
            console_by_dom_type[dom_type] = console_by_dom_type.get(dom_type, 0) + 1
            console_by_exfil_event_name[ev.event_name] = console_by_exfil_event_name.get(ev.event_name, 0) + 1

    return {
        "recording_exfil_total_events": len(events),
        "recording_exfil_cdp_event_count": cdp_total,
        "recording_exfil_console_event_count": console_total,
        "recording_exfil_cdp_event_name_counts": cdp_by_event_name,
        "recording_exfil_console_dom_type_counts": console_by_dom_type,
        "recording_exfil_console_exfil_event_name_counts": console_by_exfil_event_name,
    }


LOG = structlog.get_logger(__name__)

# avoid decompression bombs
MAX_BASE64_SIZE = 14 * 1024 * 1024  # ~10MB compressed + base64 overhead
DEFAULT_DRAFT_ACTION_TITLE = "Browser Action"


def _action_identity(action: Action) -> tuple[str, str, str, str]:
    """Stable identity fields used for duplicate-action suppression."""
    return (
        str(action.kind),
        action.url,
        action.target.sky_id or "",
        action.target.id or "",
    )


def _is_duplicate_action(candidate: Action, existing_actions: list[Action]) -> bool:
    """
    Suppress duplicate actions emitted from duplicate transport events.

    We only dedupe when the latest action has the exact same identity and
    timestamps, which keeps intentional repeated clicks intact.
    """
    if not existing_actions:
        return False

    previous = existing_actions[-1]
    return (
        _action_identity(previous) == _action_identity(candidate)
        and previous.timestamp_start == candidate.timestamp_start
        and previous.timestamp_end == candidate.timestamp_end
    )


def deterministic_goto_url_label(url: str) -> str:
    host = ""
    try:
        host = urlparse(url).netloc
    except ValueError:
        pass

    return normalize_recording_block_label(f"goto_{host}" if host else None, fallback="goto_url")


def deterministic_wait_seconds(duration_ms: int) -> int:
    return int(max(duration_ms / 1000.0, ActionWait.MIN_DURATION_THRESHOLD_MS / 1000.0))


def deterministic_input_text_parameter_key(action: ActionInputText) -> str:
    target = action.target
    for candidate in (target.id, *(target.texts or []), target.sky_id):
        if not candidate:
            continue
        key = normalize_recording_block_label(str(candidate), fallback="")
        if key:
            return key.lower()
    return "input_value"


def normalize_recording_block_label(label: str | None, *, fallback: str) -> str:
    candidate = (label or "").strip()
    candidate = re.sub(r"\W+", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate).strip("_")

    if not candidate:
        return fallback

    if not re.match(r"^[A-Za-z_]", candidate):
        candidate = f"{fallback}_{candidate}"

    return candidate


class Processor:
    """
    Process browser session recordings into workflow definition blocks.
    """

    def __init__(
        self,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> None:
        self.browser_session_id = browser_session_id
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    @property
    def identity(self) -> dict[str, str]:
        return dict(
            browser_session_id=self.browser_session_id,
            organization_id=self.organization_id,
            workflow_permanent_id=self.workflow_permanent_id,
        )

    def decompress(self, base64_payload: str) -> bytes | None:
        """
        Decode a base64 string, decompress it using gzip, and return it.
        """

        if len(base64_payload) > MAX_BASE64_SIZE:
            LOG.warning(f"{self.class_name}: base64 payload too large: {len(base64_payload)} bytes", **self.identity)
            return None

        try:
            # base64 decode -> gzip binary data
            #
            # NOTE(llm): The data sent from btoa() is technically a "non-standard"
            # Base64, but Python's standard decoder is usually robust enough to
            # handle it.
            compressed_data: bytes = base64.b64decode(base64_payload)
        except Exception as ex:
            LOG.warning(f"{self.class_name} failed to decode Base64 payload", exc_info=ex, **self.identity)
            return None

        try:
            # gzip decompression -> bytes
            #
            # NOTE(llm): We use zlib.decompress with wbits=16 + zlib.MAX_WBITS (31).
            # This tells zlib to automatically detect and handle Gzip headers,
            # which is essential since the browser used CompressionStream('gzip').
            # Using zlib is often faster than the higher-level gzip module for this
            # purpose.
            decompressed_bytes: bytes = zlib.decompress(compressed_data, wbits=16 + zlib.MAX_WBITS)
        except zlib.error as e:
            LOG.warning(f"{self.class_name} decompression error: {e}", **self.identity)
            # Log the error, maybe log the first few characters of the payload for debugging
            return None

        return decompressed_bytes

    def serialize(self, decompressed_bytes: bytes | None) -> list[dict[str, t.Any]]:
        """
        Convert decompressed bytes into a list of events (Python list/dictionary).
        """
        if not decompressed_bytes:
            LOG.warning(f"{self.class_name} No decompressed bytes to serialize", **self.identity)
            return []

        try:
            # bytes -> JSON string
            json_string: str = decompressed_bytes.decode("utf-8")
        except Exception as e:
            LOG.warning(f"{self.class_name} decode error: {e}", **self.identity)
            return []

        try:
            # JSON string -> list of dicts
            events_list: list[dict[str, t.Any]] = json.loads(json_string)
        except Exception as e:
            LOG.warning(f"{self.class_name} JSON parsing error: {e}", **self.identity)
            return []

        if not isinstance(events_list, list):
            LOG.warning(f"{self.class_name} Expected a list of events, got:", type(events_list), **self.identity)
            return []

        return events_list

    def reify(self, events_list: list[dict[str, t.Any]]) -> list[ExfiltratedEvent]:
        """
        Convert a list of event dictionaries into a list of `ExfiltratedEvent`s.
        """

        if not events_list:
            LOG.warning(f"{self.class_name} No events to reify", **self.identity)
            return []

        reified_events: list[ExfiltratedEvent] = []
        for event in events_list:
            if event.get("source") == "cdp":
                try:
                    reified_event = ExfiltratedCdpEvent(**event)
                except Exception as e:
                    LOG.warning(f"{self.class_name} Failed to reify CDP event: {e}", **self.identity)
                    continue
            elif event.get("source") == "console":
                try:
                    reified_event = ExfiltratedConsoleEvent(**event)
                except Exception as e:
                    LOG.warning(f"{self.class_name} Failed to reify console event: {e}", **self.identity)
                    continue
            else:
                LOG.error(f"{self.class_name} Unknown event source: {event.get('source')}", **self.identity)
                continue
            reified_events.append(reified_event)

        return reified_events

    def compressed_chunks_to_events(self, compressed_chunks: list[str]) -> list[ExfiltratedEvent]:
        """
        Convert a list of base64 encoded and compressed (gzip) event strings into
        a list of `ExfiltratedEvent`s.
        """
        all_events: list[ExfiltratedEvent] = []

        for compressed_chunk in compressed_chunks:
            decompressed = self.decompress(compressed_chunk)
            serialized = self.serialize(decompressed)
            reified = self.reify(serialized)
            all_events.extend(reified)

        return all_events

    def events_to_actions(
        self,
        events: list[ExfiltratedEvent],
        machines: list[sm.StateMachine] | None = None,
        initial_actions: list[Action] | None = None,
    ) -> list[Action]:
        """
        Convert a list of `ExfiltratedEvent`s into `Action`s.
        """
        actions: list[Action] = list(initial_actions or [])

        machines = machines or [
            sm.Click(),
            sm.Hover(),
            sm.InputText(),
            sm.UrlChange(),
            sm.Wait(),
        ]

        for event in events:
            for machine in machines:
                action = machine.tick(event, actions)

                if not action:
                    continue

                allow_action = True

                for m in machines:
                    if not m.on_action(action, actions):
                        allow_action = False
                        LOG.debug(
                            f"{self.class_name} action vetoed by state machine {m.__class__.__name__}",
                            action=action,
                            **self.identity,
                        )

                if allow_action:
                    if _is_duplicate_action(action, actions):
                        LOG.debug(
                            f"{self.class_name} duplicate action suppressed",
                            action=action,
                            **self.identity,
                        )
                        continue

                    actions.append(action)
                else:
                    # if an action was vetoed, we do not allow further processing
                    # of this event through subsequent state machines
                    break

        # NOTE: append-only — the live interpreter calls this each iteration and
        # tracks emitted actions by index, so collapsing here would shrink the list
        # and drop a later wait. Collapsing happens in the raw process() path only.
        return actions

    @staticmethod
    def _collapse_consecutive_waits(actions: list[Action]) -> list[Action]:
        collapsed: list[Action] = []

        for action in actions:
            previous = collapsed[-1] if collapsed else None
            if isinstance(action, ActionWait) and isinstance(previous, ActionWait):
                collapsed[-1] = ActionWait(
                    kind=ActionKind.WAIT.value,
                    target=previous.target,
                    timestamp_start=previous.timestamp_start,
                    timestamp_end=action.timestamp_end,
                    url=action.url,
                    duration_ms=previous.duration_ms + action.duration_ms,
                )
                continue
            collapsed.append(action)

        return collapsed

    def dedupe_block_labels(self, suspects: list[OutputBlock]) -> list[OutputBlock]:
        """
        Detect if any block labels are duplicated, and, if so, rename them for
        uniqueness.
        """

        blocks: list[OutputBlock] = []
        labels: set[str] = set()

        for block in suspects:
            if block.label not in labels:
                labels.add(block.label)
                blocks.append(block)
                continue
            else:
                original_label = block.label
                count = 0
                while True:
                    new_label = f"{original_label}_{count}"
                    if new_label not in labels:
                        cls = block.__class__
                        data = block.model_dump() | {"label": new_label}
                        new_block = cls(**data)
                        blocks.append(new_block)
                        labels.add(new_label)
                        break
                    count += 1

        return blocks

    async def actions_to_blocks(self, actions: list[Action]) -> list[OutputBlock]:
        """
        Convert a list of `Action` objects into workflow definition (YAML) blocks.
        """
        tasks: list[asyncio.Task] = []

        for action in actions:
            action_kind = action.kind.value

            match action.kind:
                case ActionKind.CLICK | ActionKind.HOVER | ActionKind.INPUT_TEXT:
                    task = asyncio.create_task(self.create_action_block(action))
                    tasks.append(task)
                case ActionKind.URL_CHANGE:
                    task = asyncio.create_task(self.create_url_block(action))
                    tasks.append(task)
                case ActionKind.WAIT:
                    task = asyncio.create_task(self.create_wait_block(action))
                    tasks.append(task)
                case _:
                    LOG.warning(
                        f"{self.class_name} Unknown action kind: {action_kind}",
                        action=action,
                        **self.identity,
                    )
                    continue

        blocks: list[OutputBlock] = await asyncio.gather(*tasks)

        blocks = self.dedupe_block_labels(blocks)

        return blocks

    def blocks_to_parameters(self, blocks: list[OutputBlock]) -> list[WorkflowDefinitionYamlParametersItem_Workflow]:
        """
        Convert a list of workflow definition (YAML) blocks into workflow definition (YAML) parameters.
        """
        parameter_names: set[str] = set()

        for block in blocks:
            if isinstance(block, WorkflowDefinitionYamlBlocksItem_Action):
                for param_name in block.parameter_keys or []:
                    parameter_names.add(param_name)

        parameters: list[WorkflowDefinitionYamlParametersItem_Workflow] = []

        for param_name in parameter_names:
            parameter = WorkflowDefinitionYamlParametersItem_Workflow(
                key=param_name,
                workflow_parameter_type="string",
                default_value="",
                description="",
            )
            parameters.append(parameter)

        return parameters

    def drafts_to_blocks(self, draft_steps: list[RecordingDraftStep]) -> list[OutputBlock]:
        """
        Convert user-editable live recording drafts into workflow definition blocks.
        """
        blocks: list[OutputBlock] = []

        for draft_step in draft_steps:
            match draft_step.block_type:
                case "action":
                    block = WorkflowDefinitionYamlBlocksItem_Action(
                        label=normalize_recording_block_label(draft_step.label, fallback="act"),
                        title=draft_step.title or DEFAULT_DRAFT_ACTION_TITLE,
                        navigation_goal=draft_step.navigation_goal or "",
                        error_code_mapping=None,
                        parameters=draft_step.parameters,
                        parameter_keys=draft_step.parameter_keys,
                    )
                case "goto_url":
                    url = (draft_step.url or "").strip()
                    if not url:
                        LOG.warning(
                            "skipping draft goto_url block with empty URL",
                            draft_step=draft_step.model_dump(mode="json"),
                            **self.identity,
                        )
                        continue
                    fallback_label = deterministic_goto_url_label(url)
                    title_candidate = (draft_step.title or "").strip()
                    label_candidate = (draft_step.label or "").strip()
                    if title_candidate:
                        goto_label = normalize_recording_block_label(
                            title_candidate,
                            fallback=fallback_label,
                        )
                    elif label_candidate:
                        goto_label = normalize_recording_block_label(
                            label_candidate,
                            fallback=fallback_label,
                        )
                    else:
                        goto_label = fallback_label
                    block = WorkflowDefinitionYamlBlocksItem_GotoUrl(
                        label=goto_label,
                        url=url,
                    )
                case "wait":
                    wait_sec = max(
                        int(draft_step.wait_sec or 0),
                        int(ActionWait.MIN_DURATION_THRESHOLD_MS / 1000.0),
                    )
                    block = WorkflowDefinitionYamlBlocksItem_Wait(
                        label=normalize_recording_block_label(draft_step.label, fallback="wait"),
                        wait_sec=wait_sec,
                    )
                case _:
                    LOG.warning(
                        "skipping unsupported draft block type",
                        draft_step=draft_step.model_dump(mode="json"),
                        **self.identity,
                    )
                    continue

            blocks.append(block)

        return self.dedupe_block_labels(blocks)

    async def create_action_block(self, action: ActionBlockable) -> WorkflowDefinitionYamlBlocksItem_Action:
        """
        Create a YAML action block from an `ActionBlockable`.
        """

        DEFAULT_BLOCK_TITLE = "Browser Action"

        if action.kind == ActionKind.INPUT_TEXT:
            prompt_name = "recording-action-block-prompt-input-text"
        else:
            prompt_name = "recording-action-block-prompt"

        metadata_prompt = prompt_engine.load_prompt(
            prompt_name,
            action=action,
        )

        metadata_response = await app.LLM_API_HANDLER(
            prompt=metadata_prompt,
            prompt_name=prompt_name,
            organization_id=self.organization_id,
        )

        block_label: str = metadata_response.get("block_label", None) or "act"
        title: str = metadata_response.get("title", None) or DEFAULT_BLOCK_TITLE
        navigation_goal: str = metadata_response.get("prompt", "")
        parameter_name: dict | None = metadata_response.get("parameter_name", None)

        block = WorkflowDefinitionYamlBlocksItem_Action(
            label=block_label,
            title=title,
            navigation_goal=navigation_goal,
            error_code_mapping=None,
            parameters=[parameter_name] if parameter_name else [],  # sic(jdo): the frontend requires this
            parameter_keys=[parameter_name.get("key")] if parameter_name else [],
        )

        return block

    async def create_url_block(self, action: ActionUrlChange) -> WorkflowDefinitionYamlBlocksItem_GotoUrl:
        """
        Create a YAML goto URL block from an `ActionUrlChange`.

        Fully deterministic: goto blocks carry no LLM-generated metadata, so
        skipping the LLM round-trip makes navigation drafts instant.
        """
        return WorkflowDefinitionYamlBlocksItem_GotoUrl(
            label=deterministic_goto_url_label(action.url),
            url=action.url,
        )

    async def create_wait_block(self, action: ActionWait) -> WorkflowDefinitionYamlBlocksItem_Wait:
        """
        Create a YAML wait block from an `ActionWait`.

        Fully deterministic: wait blocks carry no LLM-generated metadata, so
        skipping the LLM round-trip makes wait drafts instant.
        """
        wait_sec = deterministic_wait_seconds(action.duration_ms)

        return WorkflowDefinitionYamlBlocksItem_Wait(
            label=f"wait_{wait_sec}s",
            wait_sec=wait_sec,
        )

    async def process(
        self,
        compressed_chunks: list[str],
        draft_steps: list[RecordingDraftStep] | None = None,
    ) -> tuple[list[OutputBlock], list[WorkflowDefinitionYamlParametersItem_Workflow]]:
        """
        Process the compressed browser session recording into workflow definition blocks.
        """
        # `is not None` (not truthiness): an empty list means the user deleted every
        # live-interpreted step, which must not fall back to re-processing raw events.
        if draft_steps is not None:
            LOG.info(
                "record_browser.process_recording_drafts",
                recording_draft_step_count=len(draft_steps),
                **self.identity,
            )
            blocks = self.drafts_to_blocks(draft_steps)
            parameters = self.blocks_to_parameters(blocks)
            return blocks, parameters

        events = self.compressed_chunks_to_events(compressed_chunks)
        LOG.info(
            "record_browser.process_recording_payload",
            recording_compressed_chunk_count=len(compressed_chunks),
            **summarize_exfiltrated_recording_events(events),
            **self.identity,
        )
        actions = self._collapse_consecutive_waits(self.events_to_actions(events))
        blocks = await self.actions_to_blocks(actions)
        parameters = self.blocks_to_parameters(blocks)

        return blocks, parameters


class BrowserSessionRecordingService:
    async def process_recording(
        self,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        compressed_chunks: list[str],
        draft_steps: list[RecordingDraftStep] | None = None,
    ) -> tuple[list[OutputBlock], list[WorkflowDefinitionYamlParametersItem_Workflow]]:
        """
        Process compressed browser session recording events into workflow definition blocks.
        """
        processor = Processor(
            browser_session_id,
            organization_id,
            workflow_permanent_id,
        )

        return await processor.process(compressed_chunks, draft_steps=draft_steps)


async def smoke() -> None:
    with open(pathlib.Path("/path/to/uncompressed/events.json")) as f:
        raw_events: list[dict] = json.load(f)

    events: list[ExfiltratedEvent] = []

    for i, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            LOG.debug(f"~ skipping non-dict event: {raw_event}")
            continue
        if raw_event.get("source") == "cdp":
            try:
                event = ExfiltratedCdpEvent(**raw_event)
            except Exception:
                LOG.exception(f"{i} Failed to parse exfiltrated CDP event")
                LOG.debug(f"~ raw event: {json.dumps(raw_event, sort_keys=True, indent=2)}")
                continue
            events.append(event)
        elif raw_event.get("source") == "console":
            event = ExfiltratedConsoleEvent(**raw_event)
            events.append(event)

    LOG.debug(f"{len(events)} events.")

    my_local_org_id = "o_389844905020748346"
    processor = Processor("pbs_123", my_local_org_id, "wpid_123")
    actions = processor.events_to_actions(events)

    LOG.debug(f"{len(actions)} actions:")

    for action in actions:
        id = action.target.sky_id if action.target.sky_id else action.target.id
        text = ",".join(action.target.texts or [])
        LOG.debug(f"  {action.kind} [{id}] [{text}] @ {action.url}")

    blocks = await processor.actions_to_blocks(actions)

    LOG.debug(f"{len(blocks)} blocks:")

    for block in blocks:
        LOG.debug(f"  {block.label}")

        if isinstance(block, WorkflowDefinitionYamlBlocksItem_Action):
            LOG.debug(f"    title: {block.title}")
            LOG.debug(f"    nav goal: {block.navigation_goal}")

        if isinstance(block, WorkflowDefinitionYamlBlocksItem_GotoUrl):
            LOG.debug(f"    url: {block.url}")

        if isinstance(block, WorkflowDefinitionYamlBlocksItem_Wait):
            LOG.debug(f"    wait sec: {block.wait_sec}")


# if __name__ == "__main__":
#     from skyvern.forge.forge_app_initializer import start_forge_app

#     start_forge_app()

#     asyncio.run(smoke())
