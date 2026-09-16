import base64
import functools
import json
import re
import typing as t
import zlib
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import structlog

import skyvern.services.browser_recording.state_machines as sm
from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_Code,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
    WorkflowDefinitionYamlBlocksItem_Wait,
)
from skyvern.client.types.workflow_definition_yaml_parameters_item import WorkflowDefinitionYamlParametersItem
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.services.browser_recording.code_first import actions_to_code_first_blocks
from skyvern.services.browser_recording.evidence import RecordingEvidencePacket, build_recording_evidence
from skyvern.services.browser_recording.redact import is_secret_field, redact_console_event, texts_are_labels
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
    RecordingDraftStep,
)


def _durable_recording_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return ""
    return urlunparse((parsed.scheme, host, "", "", "", ""))


_DURABLE_TARGET_TAGS = frozenset(
    {
        "a",
        "button",
        "div",
        "input",
        "label",
        "li",
        "option",
        "path",
        "select",
        "span",
        "svg",
        "textarea",
    }
)
_DURABLE_TARGET_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "gridcell",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
    }
)
_DURABLE_TARGET_INPUT_TYPES = frozenset(
    {
        "button",
        "checkbox",
        "color",
        "date",
        "datetime-local",
        "email",
        "file",
        "hidden",
        "image",
        "month",
        "number",
        "password",
        "radio",
        "range",
        "reset",
        "search",
        "submit",
        "tel",
        "text",
        "time",
        "url",
        "week",
    }
)
_DURABLE_TARGET_AUTOCOMPLETE_TOKENS = frozenset(
    {
        "additional-name",
        "address-level1",
        "address-level2",
        "address-level3",
        "address-level4",
        "address-line1",
        "address-line2",
        "address-line3",
        "bday",
        "bday-day",
        "bday-month",
        "bday-year",
        "billing",
        "cc-additional-name",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
        "cc-family-name",
        "cc-given-name",
        "cc-name",
        "cc-number",
        "cc-type",
        "country",
        "country-name",
        "current-password",
        "email",
        "family-name",
        "given-name",
        "honorific-prefix",
        "honorific-suffix",
        "impp",
        "language",
        "name",
        "new-password",
        "nickname",
        "off",
        "on",
        "one-time-code",
        "organization",
        "organization-title",
        "photo",
        "postal-code",
        "sex",
        "shipping",
        "street-address",
        "tel",
        "tel-area-code",
        "tel-country-code",
        "tel-extension",
        "tel-local",
        "tel-local-prefix",
        "tel-local-suffix",
        "tel-national",
        "transaction-amount",
        "transaction-currency",
        "url",
        "username",
    }
)


def _allowlisted_target_value(value: str | None, allowed: frozenset[str]) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized if normalized in allowed else None


def _allowlisted_autocomplete(value: str | None) -> str | None:
    tokens = (value or "").strip().lower().split()
    if not tokens or any(token not in _DURABLE_TARGET_AUTOCOMPLETE_TOKENS for token in tokens):
        return None
    return " ".join(tokens)


def build_durable_recording_evidence(actions: list[Action]) -> list[dict[str, t.Any]]:
    evidence: list[dict[str, t.Any]] = []
    for action in sorted(actions, key=lambda item: (item.timestamp_start, item.timestamp_end)):
        target = {
            key: value
            for key, value in {
                "tag_name": _allowlisted_target_value(action.target.tag_name, _DURABLE_TARGET_TAGS),
                "role": _allowlisted_target_value(action.target.role, _DURABLE_TARGET_ROLES),
                "input_type": _allowlisted_target_value(action.target.input_type, _DURABLE_TARGET_INPUT_TYPES),
                "autocomplete": _allowlisted_autocomplete(action.target.autocomplete),
            }.items()
            if value is not None
        }
        evidence.append(
            {
                "kind": action.kind.value,
                "timestamp_start": action.timestamp_start,
                "timestamp_end": action.timestamp_end,
                "url": _durable_recording_url(action.url),
                "target": target,
            }
        )
    return evidence


def build_durable_recording_metadata(
    *,
    draft_steps: list[RecordingDraftStep] | None,
    blocks: list[WorkflowDefinitionYamlBlocksItem_Code],
    parameters: list[WorkflowDefinitionYamlParametersItem],
    interpretation_session_id: str | None,
) -> dict[str, t.Any]:
    return {
        "code_first": True,
        "interpretation_session_id": interpretation_session_id,
        "draft_steps": [
            {
                "step_id": step.step_id,
                "action_kind": step.action_kind.value,
                "block_type": step.block_type,
                "status": step.status.value,
                "editable_fields": [field.value for field in step.editable_fields],
                "timestamp_start": step.timestamp_start,
                "timestamp_end": step.timestamp_end,
                "credential_kind": step.credential_kind,
            }
            for step in draft_steps or []
        ],
        "generated_blocks": [{"block_type": block.block_type} for block in blocks],
        "generated_parameters": [{"parameter_type": parameter.parameter_type} for parameter in parameters],
    }


LOG = structlog.get_logger(__name__)

# avoid decompression bombs
MAX_BASE64_SIZE = 14 * 1024 * 1024  # ~10MB compressed + base64 overhead
# Cap decompressed output per chunk. The compressed input is already bounded to ~10MB, so this
# allows a generous ~10x expansion for legitimate recordings while rejecting bombs that would
# otherwise inflate to gigabytes and exhaust process memory on a shared host.
MAX_DECOMPRESSED_SIZE = 100 * 1024 * 1024  # 100MB


def _gunzip_bounded(compressed_data: bytes, max_output_size: int) -> bytes | None:
    """Gzip-decompress `compressed_data`, returning None once the output would exceed
    `max_output_size`.

    The bound is enforced incrementally via zlib's ``max_length`` so a decompression bomb
    aborts mid-stream and never materializes its full output in memory. Raises ``zlib.error``
    on malformed input, matching a raw ``zlib.decompress`` call.
    """
    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    output = bytearray()
    pending = compressed_data

    while pending:
        # +1 so an output that exactly fills the budget stays distinguishable from an overflow.
        output.extend(decompressor.decompress(pending, max_output_size - len(output) + 1))
        if len(output) > max_output_size:
            return None
        pending = decompressor.unconsumed_tail

    # unconsumed_tail is empty, so all input was consumed within the budget; flush only drains
    # zlib's small internal buffer and cannot reintroduce an unbounded amount of output.
    output.extend(decompressor.flush())
    if len(output) > max_output_size:
        return None

    return bytes(output)


@functools.lru_cache(maxsize=None)
def _resolve_enrichment_handler(key: str) -> LLMAPIHandler | None:
    """Resolve the dedicated enrichment handler for `key`, or None if it isn't registered.

    Memoized because the key is static config and the registry is populated at startup;
    exceptions propagate uncached so a transient resolution failure isn't pinned.
    """
    if LLMConfigRegistry.is_registered(key):
        return LLMAPIHandlerFactory.get_llm_api_handler(key)
    return None


def _recording_enrichment_llm_handler() -> LLMAPIHandler:
    """Dedicated (fast) LLM for draft enrichment; falls back to the default handler on any resolution failure."""
    key = settings.RECORDING_ENRICHMENT_LLM_KEY
    if key:
        try:
            handler = _resolve_enrichment_handler(key)
        except Exception:
            LOG.warning(
                "record_browser.enrichment_llm_fallback",
                enrichment_llm_key=key,
                exc_info=True,
            )
        else:
            if handler is not None:
                return handler
    return app.LLM_API_HANDLER


# Re-captures of one interaction land within this browser-clock (ms) window; genuine repeats fall outside it.
DUPLICATE_ACTION_WINDOW_MS = 250
DUPLICATE_ACTION_SCAN_DEPTH = 8


def _action_identity(action: Action) -> tuple[str, str, str, str, str]:
    """Stable identity fields used for duplicate-action suppression."""
    return (
        str(action.kind),
        action.url,
        action.target.sky_id or "",
        action.target.id or "",
        # A <select> fires change once per type-ahead keystroke. Without the value those read as
        # one action, and suppression keeps the first — an option the user only passed through.
        action.input_value if isinstance(action, ActionInputText) else "",
    )


def _is_duplicate_action(candidate: Action, existing_actions: list[Action]) -> bool:
    """
    Suppress duplicate actions from duplicate transport events: a recent action (within
    DUPLICATE_ACTION_WINDOW_MS, by identity) marks the candidate a duplicate. The tail scan and
    window catch non-adjacent, ms-jittered re-captures while keeping intentional repeats intact.
    """
    if not existing_actions:
        return False

    for previous in reversed(existing_actions[-DUPLICATE_ACTION_SCAN_DEPTH:]):
        if _action_identity(previous) != _action_identity(candidate):
            continue
        if abs(candidate.timestamp_start - previous.timestamp_start) <= DUPLICATE_ACTION_WINDOW_MS:
            return True

    return False


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
    # texts is only labelling for a void <input>; a <select> or <textarea> can carry option
    # labels or its own content there. The accessible name is the field label in those cases.
    if texts_are_labels(target.tag_name):
        candidates = (target.id, *(target.texts or []), target.sky_id)
    else:
        candidates = (target.id, target.accessible_name, target.sky_id)
    for candidate in candidates:
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
        recording_attempt_id: str | None = None,
        interpretation_session_id: str | None = None,
    ) -> None:
        self.browser_session_id = browser_session_id
        self.organization_id = organization_id
        self.workflow_permanent_id = workflow_permanent_id
        self.recording_attempt_id = recording_attempt_id
        self.interpretation_session_id = interpretation_session_id

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    @property
    def identity(self) -> dict[str, str]:
        identity = {
            "browser_session_id": self.browser_session_id,
            "organization_id": self.organization_id,
            "workflow_permanent_id": self.workflow_permanent_id,
        }
        if self.recording_attempt_id is not None:
            identity["recording_attempt_id"] = self.recording_attempt_id
        if self.interpretation_session_id is not None:
            identity["interpretation_session_id"] = self.interpretation_session_id
        return identity

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
            # gzip decompression -> bytes, bounded to MAX_DECOMPRESSED_SIZE.
            #
            # NOTE(llm): wbits=16 + zlib.MAX_WBITS (31) tells zlib to detect and handle Gzip
            # headers, which is essential since the browser used CompressionStream('gzip').
            decompressed_bytes = _gunzip_bounded(compressed_data, MAX_DECOMPRESSED_SIZE)
        except zlib.error as e:
            LOG.warning(f"{self.class_name} decompression error: {e}", **self.identity)
            # Log the error, maybe log the first few characters of the payload for debugging
            return None

        if decompressed_bytes is None:
            LOG.warning(
                f"{self.class_name}: decompressed payload exceeded {MAX_DECOMPRESSED_SIZE} bytes; "
                "rejecting suspected decompression bomb",
                **self.identity,
            )
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
            reified_events.append(redact_console_event(reified_event))

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
            sm.FileUpload(),
            sm.Select(),
            # After InputText: an Enter that submits a field must record as the fill
            # followed by the keypress, not replace it.
            sm.PressKey(),
            sm.UrlChange(),
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
        # and drop a later wait.
        return actions

    async def create_action_block(self, action: ActionBlockable) -> WorkflowDefinitionYamlBlocksItem_Action:
        """
        Create a YAML action block from an `ActionBlockable`.
        """

        DEFAULT_BLOCK_TITLE = "Browser Action"

        if action.kind == ActionKind.INPUT_TEXT:
            prompt_name = "recording-action-block-prompt-input-text"
            if (
                isinstance(action, ActionInputText)
                and is_secret_field(
                    action.target.input_type,
                    action.target.autocomplete,
                    field_id=action.target.id,
                    accessible_name=action.target.accessible_name,
                    texts=action.target.texts,
                    tag_name=action.target.tag_name,
                )
                and action.input_value
            ):
                action = action.model_copy(update={"input_value": ""})
        else:
            prompt_name = "recording-action-block-prompt"

        metadata_prompt = prompt_engine.load_prompt(
            prompt_name,
            action=action,
        )

        llm_kwargs: dict[str, t.Any] = {
            "prompt": metadata_prompt,
            "prompt_name": prompt_name,
            "organization_id": self.organization_id,
        }
        if self.recording_attempt_id is not None:
            llm_kwargs["recording_attempt_id"] = self.recording_attempt_id
        if self.interpretation_session_id is not None:
            llm_kwargs["interpretation_session_id"] = self.interpretation_session_id

        metadata_response = await _recording_enrichment_llm_handler()(
            **llm_kwargs,
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
        recorded_actions: list[Action] | None = None,
        supports_credential_tokens: bool = False,
    ) -> tuple[
        list[WorkflowDefinitionYamlBlocksItem_Code],
        list[WorkflowDefinitionYamlParametersItem],
        RecordingEvidencePacket,
    ]:
        """
        Process the compressed browser session recording into workflow definition blocks.
        """
        blocks, parameters, _, _, evidence = await self.process_with_evidence(
            compressed_chunks,
            draft_steps=draft_steps,
            recorded_actions=recorded_actions,
            supports_credential_tokens=supports_credential_tokens,
        )
        return blocks, parameters, evidence

    async def process_with_evidence(
        self,
        compressed_chunks: list[str],
        draft_steps: list[RecordingDraftStep] | None = None,
        recorded_actions: list[Action] | None = None,
        supports_credential_tokens: bool = False,
    ) -> tuple[
        list[WorkflowDefinitionYamlBlocksItem_Code],
        list[WorkflowDefinitionYamlParametersItem],
        list[dict[str, t.Any]],
        dict[str, t.Any],
        RecordingEvidencePacket,
    ]:
        if recorded_actions is None:
            events = self.compressed_chunks_to_events(compressed_chunks)
            actions = self.events_to_actions(events)
        else:
            actions = recorded_actions
        refinement_evidence = build_recording_evidence(
            actions,
            draft_steps,
            browser_session_id=self.browser_session_id,
            workflow_permanent_id=self.workflow_permanent_id,
            recording_attempt_id=self.recording_attempt_id or f"rra_{uuid4().hex}",
        )
        result = actions_to_code_first_blocks(actions, draft_steps, bind_credentials=supports_credential_tokens)
        if result is None:
            blocks: list[WorkflowDefinitionYamlBlocksItem_Code] = []
            parameters: list[WorkflowDefinitionYamlParametersItem] = []
            LOG.warning(
                "record_browser.process_recording_no_code_blocks",
                recording_action_count=len(actions),
                **self.identity,
            )
        else:
            blocks, parameters = result
            LOG.info(
                "record_browser.process_recording_code_first",
                recording_code_block_count=len(blocks),
                recording_code_parameter_count=len(parameters),
                recording_action_count=len(actions),
                **self.identity,
            )

        return (
            blocks,
            parameters,
            build_durable_recording_evidence(actions),
            build_durable_recording_metadata(
                draft_steps=draft_steps,
                blocks=blocks,
                parameters=parameters,
                interpretation_session_id=self.interpretation_session_id,
            ),
            refinement_evidence,
        )


class BrowserSessionRecordingService:
    async def process_recording(
        self,
        browser_session_id: str,
        organization_id: str,
        workflow_permanent_id: str,
        compressed_chunks: list[str],
        draft_steps: list[RecordingDraftStep] | None = None,
        recorded_actions: list[Action] | None = None,
        supports_credential_tokens: bool = False,
        recording_attempt_id: str | None = None,
        interpretation_session_id: str | None = None,
    ) -> tuple[
        list[WorkflowDefinitionYamlBlocksItem_Code],
        list[WorkflowDefinitionYamlParametersItem],
        str | None,
        RecordingEvidencePacket,
    ]:
        """
        Process compressed browser session recording events into workflow definition blocks.
        """
        processor = Processor(
            browser_session_id,
            organization_id,
            workflow_permanent_id,
            recording_attempt_id=recording_attempt_id,
            interpretation_session_id=interpretation_session_id,
        )

        blocks, parameters, evidence, metadata, refinement_evidence = await processor.process_with_evidence(
            compressed_chunks,
            draft_steps=draft_steps,
            recorded_actions=recorded_actions,
            supports_credential_tokens=supports_credential_tokens,
        )
        if not blocks:
            return blocks, parameters, None, refinement_evidence

        recording = await app.DATABASE.browser_recordings.create_recording(
            organization_id=organization_id,
            recording_attempt_id=recording_attempt_id,
            browser_session_id=browser_session_id,
            workflow_permanent_id=workflow_permanent_id,
            evidence=evidence,
            metadata=metadata,
        )
        return blocks, parameters, recording.recording_id, refinement_evidence
