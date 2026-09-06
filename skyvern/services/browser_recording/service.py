import asyncio
import base64
import functools
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
    WorkflowDefinitionYamlBlocksItem_Login,
    WorkflowDefinitionYamlBlocksItem_Wait,
)
from skyvern.client.types.workflow_definition_yaml_parameters_item import (
    WorkflowDefinitionYamlParametersItem,
    WorkflowDefinitionYamlParametersItem_Credential,
    WorkflowDefinitionYamlParametersItem_Workflow,
)
from skyvern.config import settings
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler import LLMAPIHandler
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.services.browser_recording.code_first import actions_to_code_first_blocks
from skyvern.services.browser_recording.redact import is_secret_field, redact_console_event, texts_are_labels
from skyvern.services.browser_recording.types import (
    Action,
    ActionBlockable,
    ActionInputText,
    ActionKind,
    ActionUrlChange,
    ActionWait,
    CredentialKind,
    ExfiltratedCdpEvent,
    ExfiltratedConsoleEvent,
    ExfiltratedEvent,
    OutputBlock,
    ProcessedBlock,
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
# Cap decompressed output per chunk. The compressed input is already bounded to ~10MB, so this
# allows a generous ~10x expansion for legitimate recordings while rejecting bombs that would
# otherwise inflate to gigabytes and exhaust process memory on a shared host.
MAX_DECOMPRESSED_SIZE = 100 * 1024 * 1024  # 100MB
DEFAULT_DRAFT_ACTION_TITLE = "Browser Action"
DEFAULT_LOGIN_BLOCK_TITLE = "Log in"


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


LOGIN_CREDENTIAL_KINDS: frozenset[CredentialKind] = frozenset({"password", "totp", "magic_link"})

# A credential parameter resolves to a dict of fields, so binding one to a recorded fill means
# naming the field that fill typed. Only `secret` is unambiguous: SecretCredential holds a single
# value. A credit card spreads over card_number / card_cvv / card_exp_month / card_exp_year /
# card_holder_name, and the recorder classifies the field only as "credit_card" - it does not
# capture which one, so there is nothing to point the fill at. Card fills stay unbound.
CREDENTIAL_FIELD_BY_KIND: dict[CredentialKind, str] = {"secret": "secret_value"}


def _credential_id(step: RecordingDraftStep) -> str:
    return (step.credential_id or "").strip()


def _is_login_credential_step(step: RecordingDraftStep) -> bool:
    return bool(_credential_id(step)) and step.credential_kind in LOGIN_CREDENTIAL_KINDS


def _is_secret_fill(step: RecordingDraftStep) -> bool:
    return bool(_credential_id(step)) and step.credential_kind in CREDENTIAL_FIELD_BY_KIND


def _is_typed_form_field(step: RecordingDraftStep) -> bool:
    return step.block_type == "action" and step.action_kind == ActionKind.INPUT_TEXT


def _is_submit_click(step: RecordingDraftStep) -> bool:
    return step.block_type == "action" and step.action_kind == ActionKind.CLICK


def bound_credential_ids(draft_steps: list[RecordingDraftStep]) -> set[str]:
    """Credentials the generated blocks reference.

    Emitted blocks carry each credential under its id, as a parameter-key *token*: the recorder
    cannot see the target workflow, so it cannot pick a key that is free there. The editor
    allocates the real key when it applies the blocks and substitutes it for the token.
    """
    return {_credential_id(step) for step in draft_steps if _is_login_credential_step(step) or _is_secret_fill(step)}


def bind_credential_to_action_goal(navigation_goal: str, placeholder_keys: list[str], field_reference: str) -> str:
    """Repoint the fill instruction's `{{ placeholder }}` at the credential field.

    The recorder wrote the placeholder itself (`Type 'API token' with {{ api_token }}.`) as a
    workflow parameter with an empty default; left alone it renders empty and the credential
    goes unused.
    """
    goal = navigation_goal
    for key in placeholder_keys:
        goal = re.sub(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", "{{ " + field_reference + " }}", goal)
    return goal


def _is_login_fill_of(draft_steps: list[RecordingDraftStep], at: int, credential_id: str) -> bool:
    return (
        at < len(draft_steps)
        and _is_login_credential_step(draft_steps[at])
        and _credential_id(draft_steps[at]) == credential_id
    )


class LoginRun(t.NamedTuple):
    """The steps one login block replaces, plus the credential step it is built from."""

    anchor: int
    indices: list[int]


def login_block_runs(draft_steps: list[RecordingDraftStep]) -> dict[int, LoginRun]:
    """Run-start index -> the run one login block replaces.

    A login block fills and submits the form itself from the credential, so it replaces the
    steps that typed that form and nothing more: the credential fill, the one field typed just
    before it (the identifier), further fills of the same credential with at most the submit
    click between them (password, then a TOTP code), and one trailing submit click.

    Every bound is tight because each loosening has a demonstrated way to eat recorded steps.
    Reaching over same-URL steps swallows the session on a modal or single-page-app login;
    absorbing every preceding field eats a signup form's name and email; grouping every use of
    one credential erases the work between a login and a later re-auth. Clicks and text entry
    are what ordinary work looks like, so only adjacency separates a second field of the same
    form from that work.

    The cost is under-collapsing: a login split over two pages (identifier, then password on a
    new URL) leaves its first page a separate action block that still executes, typing an
    empty parameter into the identifier before the login block runs. That is visible and
    editable on the canvas; a step absorbed by a wrong guess is neither.
    """
    runs: dict[int, LoginRun] = {}
    claimed: set[int] = set()
    total = len(draft_steps)
    index = 0

    while index < total:
        step = draft_steps[index]
        if index in claimed or not _is_login_credential_step(step):
            index += 1
            continue

        credential_id = _credential_id(step)
        start = end = index

        if start - 1 >= 0 and start - 1 not in claimed and _is_typed_form_field(draft_steps[start - 1]):
            start -= 1

        while True:
            if _is_login_fill_of(draft_steps, end + 1, credential_id):
                end += 1
            elif (
                end + 1 < total
                and _is_submit_click(draft_steps[end + 1])
                and _is_login_fill_of(draft_steps, end + 2, credential_id)
            ):
                end += 2
            else:
                break

        if end + 1 < total and _is_submit_click(draft_steps[end + 1]):
            end += 1

        indices = list(range(start, end + 1))
        runs[start] = LoginRun(anchor=index, indices=indices)
        claimed.update(indices)
        index = end + 1

    return runs


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
            sm.Select(),
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

    def blocks_to_parameters(
        self,
        blocks: list[OutputBlock],
        credential_ids: set[str] | None = None,
    ) -> list[WorkflowDefinitionYamlParametersItem]:
        """
        Convert a list of workflow definition (YAML) blocks into workflow definition (YAML) parameters.

        Keys in `credential_ids` are credential tokens (see `bound_credential_ids`); they become
        credential parameters, not empty string workflow parameters.
        """
        credential_ids = credential_ids or set()
        parameter_names: set[str] = set()

        for block in blocks:
            if isinstance(block, WorkflowDefinitionYamlBlocksItem_Action):
                for param_name in block.parameter_keys or []:
                    if param_name not in credential_ids:
                        parameter_names.add(param_name)

        parameters: list[WorkflowDefinitionYamlParametersItem] = []

        for param_name in parameter_names:
            parameter = WorkflowDefinitionYamlParametersItem_Workflow(
                key=param_name,
                workflow_parameter_type="string",
                default_value="",
                description="",
            )
            parameters.append(parameter)

        for credential_id in sorted(credential_ids):
            parameters.append(
                WorkflowDefinitionYamlParametersItem_Credential(
                    key=credential_id,
                    credential_id=credential_id,
                    description="",
                )
            )

        return parameters

    def login_run_to_block(
        self,
        draft_steps: list[RecordingDraftStep],
        run: LoginRun,
    ) -> WorkflowDefinitionYamlBlocksItem_Login:
        """
        Collapse a recorded login form interaction into a single login block.
        """
        credential_step = draft_steps[run.anchor]
        tokens = list(dict.fromkeys(_credential_id(draft_steps[index]) for index in run.indices))
        tokens = [token for token in tokens if token]

        return WorkflowDefinitionYamlBlocksItem_Login(
            label=normalize_recording_block_label(credential_step.label, fallback="login"),
            title=credential_step.title or DEFAULT_LOGIN_BLOCK_TITLE,
            url=(credential_step.url or "").strip() or None,
            parameter_keys=tokens,
            # Editor's convertToNode reads block.parameters.map(p => p.key); mirror the action block.
            parameters=[{"key": token} for token in tokens],
        )

    def drafts_to_blocks(self, draft_steps: list[RecordingDraftStep]) -> list[OutputBlock]:
        """
        Convert user-editable live recording drafts into workflow definition blocks.
        """
        blocks: list[OutputBlock] = []
        login_runs = login_block_runs(draft_steps)
        absorbed_indices = {index for run in login_runs.values() for index in run.indices}

        for index, draft_step in enumerate(draft_steps):
            if index in login_runs:
                blocks.append(self.login_run_to_block(draft_steps, login_runs[index]))
                continue

            if index in absorbed_indices:
                continue

            match draft_step.block_type:
                case "action":
                    # A secret fill's placeholder parameter is replaced by the credential outright:
                    # the credential field supplies the value, so the empty string parameter and the
                    # instruction that referenced it both go.
                    navigation_goal = draft_step.navigation_goal or ""
                    parameters = list(draft_step.parameters)
                    parameter_keys = list(draft_step.parameter_keys)

                    if _is_secret_fill(draft_step) and draft_step.credential_kind:
                        token = _credential_id(draft_step)
                        field = CREDENTIAL_FIELD_BY_KIND[draft_step.credential_kind]
                        navigation_goal = bind_credential_to_action_goal(
                            navigation_goal, parameter_keys, f"{token}.{field}"
                        )
                        parameters = [{"key": token}]
                        parameter_keys = [token]

                    block = WorkflowDefinitionYamlBlocksItem_Action(
                        label=normalize_recording_block_label(draft_step.label, fallback="act"),
                        title=draft_step.title or DEFAULT_DRAFT_ACTION_TITLE,
                        navigation_goal=navigation_goal,
                        error_code_mapping=None,
                        parameters=parameters,
                        parameter_keys=parameter_keys,
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

        metadata_response = await _recording_enrichment_llm_handler()(
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
        code_first: bool = False,
    ) -> tuple[list[ProcessedBlock], list[WorkflowDefinitionYamlParametersItem]]:
        """
        Process the compressed browser session recording into workflow definition blocks.
        """
        if code_first:
            # Code-first always re-derives selector-bearing actions from raw events;
            # draft steps carry no locators and act only as an edit overlay.
            events = self.compressed_chunks_to_events(compressed_chunks)
            actions = self.events_to_actions(events)
            code_first_result = actions_to_code_first_blocks(actions, draft_steps)
            if code_first_result is not None:
                code_blocks, code_parameters = code_first_result
                LOG.info(
                    "record_browser.process_recording_code_first",
                    recording_code_block_count=len(code_blocks),
                    recording_code_parameter_count=len(code_parameters),
                    recording_action_count=len(actions),
                    **self.identity,
                )
                return list(code_blocks), code_parameters
            LOG.warning(
                "record_browser.code_first_fallback_to_legacy",
                recording_action_count=len(actions),
                **self.identity,
            )

        # `is not None` (not truthiness): an empty list means the user deleted every
        # live-interpreted step, which must not fall back to re-processing raw events.
        if draft_steps is not None:
            LOG.info(
                "record_browser.process_recording_drafts",
                recording_draft_step_count=len(draft_steps),
                **self.identity,
            )
            blocks = self.drafts_to_blocks(draft_steps)
            parameters = self.blocks_to_parameters(blocks, bound_credential_ids(draft_steps))
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
        code_first: bool = False,
    ) -> tuple[list[ProcessedBlock], list[WorkflowDefinitionYamlParametersItem]]:
        """
        Process compressed browser session recording events into workflow definition blocks.
        """
        processor = Processor(
            browser_session_id,
            organization_id,
            workflow_permanent_id,
        )

        return await processor.process(compressed_chunks, draft_steps=draft_steps, code_first=code_first)


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
