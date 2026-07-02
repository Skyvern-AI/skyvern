"""
A channel for streaming whole messages between our frontend and our API server.
This channel can access a persistent browser instance through the execution channel.

What this channel looks like:

    [Skyvern App] <--> [API Server]

Channel data:

    JSON over WebSockets. Semantics are fire and forget. Req-resp is built on
    top of that using message types.
"""

import asyncio
import dataclasses
import enum
import math
import typing as t

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosedError

from skyvern.forge.sdk.routes.streaming.channels.execution import execution_for_message_channel
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent, ExfiltrationChannel
from skyvern.forge.sdk.routes.streaming.payload_limits import MAX_CLIPBOARD_PASTE_BYTES
from skyvern.forge.sdk.routes.streaming.registries import (
    add_message_channel,
    del_message_channel,
    get_vnc_channel,
)
from skyvern.forge.sdk.routes.streaming.verify import (
    loop_verify_browser_session,
    loop_verify_workflow_run,
    verify_browser_session,
    verify_workflow_run,
)
from skyvern.forge.sdk.schemas.persistent_browser_sessions import AddressablePersistentBrowserSession
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.services.browser_recording.session_registry import interpretation_registry
from skyvern.services.browser_recording.types import RecordingDraftStep, RecordingInterpretationUpdate

LOG = structlog.get_logger()

Loops = list[asyncio.Task]  # aka "queue-less actors"; or "programs"


class MessageKind(enum.StrEnum):
    ASK_FOR_CLIPBOARD_RESPONSE = "ask-for-clipboard-response"
    BEGIN_EXFILTRATION = "begin-exfiltration"
    BROWSER_TABS = "browser-tabs"
    BROWSER_URL = "browser-url"
    CEDE_CONTROL = "cede-control"
    CLEAR_ALL_DATA = "clear-all-data"
    CLEAR_COOKIES = "clear-cookies"
    CLEAR_HISTORY = "clear-history"
    CLIPBOARD_COPY = "clipboard-copy"
    CLIPBOARD_PASTE = "clipboard-paste"
    END_EXFILTRATION = "end-exfiltration"
    ERROR = "error"
    EXFILTRATED_EVENT = "exfiltrated-event"
    GET_BROWSER_URL = "get-browser-url"
    GO_BACK = "go-back"
    GO_FORWARD = "go-forward"
    NAVIGATE = "navigate"
    RELOAD = "reload"
    RECORDING_CAPTURE_PAUSE = "recording-capture-pause"
    RECORDING_CAPTURE_RESUME = "recording-capture-resume"
    RECORDING_REARM_CAPTURE = "recording-rearm-capture"
    RECORDING_INTERPRETATION_UPDATE = "recording-interpretation-update"
    SCREENSHOT = "screenshot"
    TAKE_CONTROL = "take-control"
    TAKE_SCREENSHOT = "take-screenshot"


class ExfiltratedEventSource(enum.StrEnum):
    CONSOLE = "console"
    CDP = "cdp"
    NOT_SPECIFIED = "[not-specified]"


@dataclasses.dataclass
class TabInfo:
    id: str
    title: str
    url: str
    # --
    active: bool = False
    favicon: str | None = None
    isReady: bool = True
    pageNumber: int | None = None


MessageKinds = t.Literal[
    MessageKind.ASK_FOR_CLIPBOARD_RESPONSE,
    MessageKind.BEGIN_EXFILTRATION,
    MessageKind.BROWSER_TABS,
    MessageKind.BROWSER_URL,
    MessageKind.CEDE_CONTROL,
    MessageKind.CLEAR_ALL_DATA,
    MessageKind.CLEAR_COOKIES,
    MessageKind.CLEAR_HISTORY,
    MessageKind.CLIPBOARD_COPY,
    MessageKind.CLIPBOARD_PASTE,
    MessageKind.END_EXFILTRATION,
    MessageKind.ERROR,
    MessageKind.EXFILTRATED_EVENT,
    MessageKind.GET_BROWSER_URL,
    MessageKind.GO_BACK,
    MessageKind.GO_FORWARD,
    MessageKind.NAVIGATE,
    MessageKind.RELOAD,
    MessageKind.RECORDING_CAPTURE_PAUSE,
    MessageKind.RECORDING_CAPTURE_RESUME,
    MessageKind.RECORDING_REARM_CAPTURE,
    MessageKind.RECORDING_INTERPRETATION_UPDATE,
    MessageKind.SCREENSHOT,
    MessageKind.TAKE_CONTROL,
    MessageKind.TAKE_SCREENSHOT,
]


@dataclasses.dataclass
class Message:
    kind: MessageKinds


@dataclasses.dataclass
class MessageInBeginExfiltration(Message):
    kind: t.Literal[MessageKind.BEGIN_EXFILTRATION] = MessageKind.BEGIN_EXFILTRATION
    workflow_permanent_id: str | None = None
    live_interpretation_enabled: bool = False
    # Client capability: true only when the frontend understands delta interpretation
    # updates. Defaults false so older clients keep receiving full snapshots.
    supports_interpretation_deltas: bool = False
    # Per-recording id from the client. Distinguishes a reconnect (same id, reuse
    # session) from a new recording (new id, fresh session). None on older clients.
    recording_attempt_id: str | None = None


@dataclasses.dataclass
class MessageInEndExfiltration(Message):
    kind: t.Literal[MessageKind.END_EXFILTRATION] = MessageKind.END_EXFILTRATION


@dataclasses.dataclass
class MessageInRecordingCapturePause(Message):
    kind: t.Literal[MessageKind.RECORDING_CAPTURE_PAUSE] = MessageKind.RECORDING_CAPTURE_PAUSE


@dataclasses.dataclass
class MessageInRecordingCaptureResume(Message):
    kind: t.Literal[MessageKind.RECORDING_CAPTURE_RESUME] = MessageKind.RECORDING_CAPTURE_RESUME


@dataclasses.dataclass
class MessageInRecordingRearmCapture(Message):
    kind: t.Literal[MessageKind.RECORDING_REARM_CAPTURE] = MessageKind.RECORDING_REARM_CAPTURE


@dataclasses.dataclass
class MessageInTakeControl(Message):
    kind: t.Literal[MessageKind.TAKE_CONTROL] = MessageKind.TAKE_CONTROL


@dataclasses.dataclass
class MessageInCedeControl(Message):
    kind: t.Literal[MessageKind.CEDE_CONTROL] = MessageKind.CEDE_CONTROL


@dataclasses.dataclass
class MessageInAskForClipboardResponse(Message):
    kind: t.Literal[MessageKind.ASK_FOR_CLIPBOARD_RESPONSE] = MessageKind.ASK_FOR_CLIPBOARD_RESPONSE
    text: str = ""


@dataclasses.dataclass
class MessageInClipboardCopy(Message):
    kind: t.Literal[MessageKind.CLIPBOARD_COPY] = MessageKind.CLIPBOARD_COPY


@dataclasses.dataclass
class MessageInClipboardPaste(Message):
    kind: t.Literal[MessageKind.CLIPBOARD_PASTE] = MessageKind.CLIPBOARD_PASTE
    text: str = ""


@dataclasses.dataclass
class MessageInGetBrowserUrl(Message):
    kind: t.Literal[MessageKind.GET_BROWSER_URL] = MessageKind.GET_BROWSER_URL


@dataclasses.dataclass
class MessageInNavigate(Message):
    kind: t.Literal[MessageKind.NAVIGATE] = MessageKind.NAVIGATE
    url: str = ""


@dataclasses.dataclass
class MessageInReload(Message):
    kind: t.Literal[MessageKind.RELOAD] = MessageKind.RELOAD
    hard: bool = False


@dataclasses.dataclass
class MessageInGoBack(Message):
    kind: t.Literal[MessageKind.GO_BACK] = MessageKind.GO_BACK


@dataclasses.dataclass
class MessageInGoForward(Message):
    kind: t.Literal[MessageKind.GO_FORWARD] = MessageKind.GO_FORWARD


@dataclasses.dataclass
class MessageInTakeScreenshot(Message):
    kind: t.Literal[MessageKind.TAKE_SCREENSHOT] = MessageKind.TAKE_SCREENSHOT


@dataclasses.dataclass
class MessageInClearCookies(Message):
    kind: t.Literal[MessageKind.CLEAR_COOKIES] = MessageKind.CLEAR_COOKIES


@dataclasses.dataclass
class MessageInClearHistory(Message):
    kind: t.Literal[MessageKind.CLEAR_HISTORY] = MessageKind.CLEAR_HISTORY


@dataclasses.dataclass
class MessageInClearAllData(Message):
    kind: t.Literal[MessageKind.CLEAR_ALL_DATA] = MessageKind.CLEAR_ALL_DATA


@dataclasses.dataclass
class MessageOutBrowserUrl(Message):
    kind: t.Literal[MessageKind.BROWSER_URL] = MessageKind.BROWSER_URL
    url: str = ""


@dataclasses.dataclass
class MessageOutScreenshot(Message):
    kind: t.Literal[MessageKind.SCREENSHOT] = MessageKind.SCREENSHOT
    data: str = ""  # base64-encoded PNG payload
    mime_type: str = "image/png"


@dataclasses.dataclass
class MessageOutError(Message):
    """Surfaces a backend handler failure to the frontend so it can toast."""

    kind: t.Literal[MessageKind.ERROR] = MessageKind.ERROR
    failed_kind: str = ""
    message: str = ""


@dataclasses.dataclass
class MessageOutExfiltratedEvent(Message):
    kind: t.Literal[MessageKind.EXFILTRATED_EVENT] = MessageKind.EXFILTRATED_EVENT
    event_name: str = "[not-specified]"

    # TODO(jdo): improve typing for params
    params: dict = dataclasses.field(default_factory=dict)
    source: ExfiltratedEventSource = ExfiltratedEventSource.NOT_SPECIFIED
    timestamp: float = dataclasses.field(default_factory=lambda: 0.0)  # seconds since epoch


@dataclasses.dataclass
class MessageOutRecordingInterpretationUpdate(Message):
    kind: t.Literal[MessageKind.RECORDING_INTERPRETATION_UPDATE] = MessageKind.RECORDING_INTERPRETATION_UPDATE
    interpretation_session_id: str = ""
    session_revision: int = 0
    steps: list[RecordingDraftStep] = dataclasses.field(default_factory=list)
    changed_steps: list[RecordingDraftStep] = dataclasses.field(default_factory=list)
    is_snapshot: bool = True
    pending: bool = False
    finalized: bool = False


@dataclasses.dataclass
class MessageOutTabInfo(Message):
    kind: t.Literal[MessageKind.BROWSER_TABS] = MessageKind.BROWSER_TABS
    tabs: list[TabInfo] = dataclasses.field(default_factory=list)


MessageIn = (
    MessageInAskForClipboardResponse
    | MessageInBeginExfiltration
    | MessageInCedeControl
    | MessageInClearAllData
    | MessageInClearCookies
    | MessageInClearHistory
    | MessageInClipboardCopy
    | MessageInClipboardPaste
    | MessageInEndExfiltration
    | MessageInGetBrowserUrl
    | MessageInGoBack
    | MessageInGoForward
    | MessageInNavigate
    | MessageInRecordingCapturePause
    | MessageInRecordingCaptureResume
    | MessageInRecordingRearmCapture
    | MessageInReload
    | MessageInTakeControl
    | MessageInTakeScreenshot
)


MessageOut = (
    MessageOutBrowserUrl
    | MessageOutError
    | MessageOutExfiltratedEvent
    | MessageOutRecordingInterpretationUpdate
    | MessageOutScreenshot
    | MessageOutTabInfo
)


def reify_channel_message(data: dict) -> MessageIn:
    kind = data.get("kind", None)

    match kind:
        case MessageKind.ASK_FOR_CLIPBOARD_RESPONSE:
            text = data.get("text") or ""
            return MessageInAskForClipboardResponse(text=text)
        case MessageKind.BEGIN_EXFILTRATION:
            workflow_permanent_id = data.get("workflow_permanent_id")
            recording_attempt_id = data.get("recording_attempt_id")
            return MessageInBeginExfiltration(
                workflow_permanent_id=workflow_permanent_id if isinstance(workflow_permanent_id, str) else None,
                live_interpretation_enabled=bool(data.get("live_interpretation_enabled") or False),
                supports_interpretation_deltas=bool(data.get("supports_interpretation_deltas") or False),
                recording_attempt_id=recording_attempt_id if isinstance(recording_attempt_id, str) else None,
            )
        case MessageKind.CEDE_CONTROL:
            return MessageInCedeControl()
        case MessageKind.CLEAR_ALL_DATA:
            return MessageInClearAllData()
        case MessageKind.CLEAR_COOKIES:
            return MessageInClearCookies()
        case MessageKind.CLEAR_HISTORY:
            return MessageInClearHistory()
        case MessageKind.CLIPBOARD_COPY:
            return MessageInClipboardCopy()
        case MessageKind.CLIPBOARD_PASTE:
            text = data.get("text") or ""
            return MessageInClipboardPaste(text=text)
        case MessageKind.END_EXFILTRATION:
            return MessageInEndExfiltration()
        case MessageKind.RECORDING_CAPTURE_PAUSE:
            return MessageInRecordingCapturePause()
        case MessageKind.RECORDING_CAPTURE_RESUME:
            return MessageInRecordingCaptureResume()
        case MessageKind.RECORDING_REARM_CAPTURE:
            return MessageInRecordingRearmCapture()
        case MessageKind.GET_BROWSER_URL:
            return MessageInGetBrowserUrl()
        case MessageKind.GO_BACK:
            return MessageInGoBack()
        case MessageKind.GO_FORWARD:
            return MessageInGoForward()
        case MessageKind.NAVIGATE:
            url = data.get("url") or ""
            return MessageInNavigate(url=url)
        case MessageKind.RELOAD:
            hard = bool(data.get("hard") or False)
            return MessageInReload(hard=hard)
        case MessageKind.TAKE_CONTROL:
            return MessageInTakeControl()
        case MessageKind.TAKE_SCREENSHOT:
            return MessageInTakeScreenshot()
        case _:
            raise ValueError(f"Unknown message kind: '{kind}'")


def message_to_dict(message: MessageOut) -> dict:
    """
    Convert message to dict with enums as their values.
    """

    def convert_value(obj: t.Any) -> t.Any:
        if isinstance(obj, enum.Enum):
            return obj.value
        if isinstance(obj, BaseModel):
            return _replace_non_finite(obj.model_dump(mode="json"))
        if isinstance(obj, list):
            return [convert_value(item) for item in obj]
        if isinstance(obj, dict):
            return {key: convert_value(value) for key, value in obj.items()}
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        return obj

    return {key: convert_value(value) for key, value in dataclasses.asdict(message).items()}


def _replace_non_finite(obj: t.Any) -> t.Any:
    """NaN/Infinity are not valid JSON; JSON.parse rejects them and drops the whole frame."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, list):
        return [_replace_non_finite(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _replace_non_finite(value) for key, value in obj.items()}
    return obj


@dataclasses.dataclass
class MessageChannel:
    """
    A message channel for streaming JSON messages between our frontend and our API server.
    """

    client_id: str
    organization_id: str
    websocket: WebSocket
    # --
    # Every outbound frame goes through here so backend_to_frontend is the single
    # websocket writer. Carries MessageOut (serialized by the pump) or a pre-built
    # dict for out-of-band frames (clipboard) that aren't modeled as MessageOut.
    out_queue: asyncio.Queue[MessageOut | dict] = dataclasses.field(default_factory=asyncio.Queue)  # warn: unbounded
    browser_session: AddressablePersistentBrowserSession | None = None
    workflow_run: WorkflowRun | None = None

    def __post_init__(self) -> None:
        add_message_channel(self)

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    @property
    def identity(self) -> dict[str, str]:
        base = {"organization_id": self.organization_id}

        if self.browser_session:
            return base | {"browser_session_id": self.browser_session.persistent_browser_session_id}

        if self.workflow_run:
            return base | {"workflow_run_id": self.workflow_run.workflow_run_id}

        return base

    async def close(self, code: int = 1000, reason: str | None = None) -> "MessageChannel":
        LOG.debug(f"{self.class_name} closing message stream.", reason=reason, code=code, **self.identity)

        self.browser_session = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_message_channel(self.client_id, expected=self)

        return self

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state != WebSocketState.CONNECTED:
            return False

        return True

    # async def send(self, *, messages: list[dict]) -> t.Self:
    async def send(self, *, messages: list[MessageOut]) -> t.Self:
        for message in messages:
            await self.out_queue.put(message)

        return self

    def send_nowait(self, *, messages: list[MessageOut]) -> t.Self:
        for message in messages:
            self.out_queue.put_nowait(message)

        return self

    async def ask_for_clipboard(self) -> None:
        LOG.info(f"{self.class_name} Sending ask-for-clipboard to message channel", **self.identity)
        # Enqueue rather than write directly: backend_to_frontend is the sole
        # websocket writer, so this can't race the pump mid-frame (this method is
        # called from the VNC channel task, a different task than the pump).
        self.out_queue.put_nowait({"kind": "ask-for-clipboard"})

    async def send_copied_text(self, copied_text: str) -> None:
        LOG.info(f"{self.class_name} Sending copied text to message channel", **self.identity)
        self.out_queue.put_nowait({"kind": "copied-text", "text": copied_text})


async def loop_stream_messages(message_channel: MessageChannel) -> None:
    """
    Stream messages and their results back and forth.

    Loops until the websocket is closed.
    """

    class_name = message_channel.class_name
    exfiltration_channel: ExfiltrationChannel | None = None
    live_interpretation_browser_session_id: str | None = None

    async def send(message: MessageOut) -> None:
        # Single-writer: enqueue only; backend_to_frontend is the sole task that
        # writes to the websocket, so responses and out-of-band messages (event
        # echoes, interpretation updates) cannot interleave mid-frame.
        message_channel.send_nowait(messages=[message])

    async def handle_data(data: object) -> None:
        nonlocal class_name
        nonlocal exfiltration_channel
        nonlocal live_interpretation_browser_session_id
        message: MessageIn

        # receive_json returns whatever the client's JSON parses to — a top-level
        # array/string/number is possible, so the guard is load-bearing.
        if isinstance(data, dict):
            try:
                message = reify_channel_message(data)
            except ValueError:
                LOG.error(f"MessageChannel: cannot reify channel message from data: {data}", **message_channel.identity)
                return
        else:
            LOG.error(
                f"{class_name} cannot handle data: expected dict, got {type(data)}",
                **message_channel.identity,
            )
            return

        async def send_error(failed_kind: MessageKind, reason: str) -> None:
            await send(MessageOutError(failed_kind=str(failed_kind), message=reason))

        async def send_current_browser_url(execute: t.Any) -> None:
            url = await execute.get_current_url()
            await send(MessageOutBrowserUrl(url=url))

        match message.kind:
            case MessageKind.ASK_FOR_CLIPBOARD_RESPONSE:
                text = message.text

                paste_byte_len = len(text.encode("utf-8"))
                if paste_byte_len > MAX_CLIPBOARD_PASTE_BYTES:
                    LOG.warning(
                        f"{class_name} ask-for-clipboard-response paste exceeds size cap; rejecting.",
                        size=paste_byte_len,
                        max_size=MAX_CLIPBOARD_PASTE_BYTES,
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Clipboard payload too large.")
                    return

                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.paste_text(text)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to paste clipboard response into browser.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to paste into browser.")

            case MessageKind.BEGIN_EXFILTRATION:
                vnc_channel = get_vnc_channel(message_channel.client_id)

                if not vnc_channel:
                    LOG.error(
                        f"{class_name} no vnc channel client found for message channel - cannot exfiltrate.",
                        message=message,
                        **message_channel.identity,
                    )
                    return

                browser_session_id = (
                    message_channel.browser_session.persistent_browser_session_id
                    if message_channel.browser_session
                    else None
                )

                def on_interpretation_update(update: RecordingInterpretationUpdate) -> None:
                    message_channel.send_nowait(
                        messages=[
                            MessageOutRecordingInterpretationUpdate(
                                interpretation_session_id=update.interpretation_session_id,
                                session_revision=update.session_revision,
                                steps=update.steps,
                                changed_steps=update.changed_steps,
                                is_snapshot=update.is_snapshot,
                                pending=update.pending,
                                finalized=update.finalized,
                            )
                        ]
                    )

                def ensure_live_interpretation() -> None:
                    nonlocal live_interpretation_browser_session_id

                    if not (
                        browser_session_id and message.workflow_permanent_id and message.live_interpretation_enabled
                    ):
                        return

                    interpretation_registry.start_session(
                        browser_session_id=browser_session_id,
                        organization_id=message_channel.organization_id,
                        workflow_permanent_id=message.workflow_permanent_id,
                        on_update=on_interpretation_update,
                        deltas_enabled=message.supports_interpretation_deltas,
                        recording_attempt_id=message.recording_attempt_id,
                    )
                    live_interpretation_browser_session_id = browser_session_id

                if exfiltration_channel is not None:
                    ensure_live_interpretation()
                    await exfiltration_channel.rearm_all_pages()
                    return

                ensure_live_interpretation()

                def on_event(events: list[ExfiltratedEvent]) -> None:
                    for event in events:
                        message_out_exfiltrated_event = MessageOutExfiltratedEvent(
                            kind=t.cast(t.Literal[MessageKind.EXFILTRATED_EVENT], event.kind),
                            event_name=event.event_name,
                            params=event.params,
                            source=t.cast(ExfiltratedEventSource, event.source or ExfiltratedEventSource.NOT_SPECIFIED),
                            timestamp=event.timestamp,
                        )

                        message_channel.send_nowait(messages=[message_out_exfiltrated_event])

                    if live_interpretation_browser_session_id:
                        interpretation_registry.ingest_events(live_interpretation_browser_session_id, events)

                exfiltration_channel = await ExfiltrationChannel(
                    on_event=on_event,
                    vnc_channel=vnc_channel,
                ).start()

            case MessageKind.CEDE_CONTROL:
                vnc_channel = get_vnc_channel(message_channel.client_id)

                if not vnc_channel:
                    LOG.error(
                        f"{class_name} no vnc channel client found for message channel.",
                        message=message,
                        **message_channel.identity,
                    )
                    return
                vnc_channel.interactor = "agent"

            case MessageKind.CLEAR_ALL_DATA:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.clear_storage()
                        await execute.clear_cookies()
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to clear all data.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to clear all browsing data.")

            case MessageKind.CLEAR_COOKIES:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.clear_cookies()
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to clear cookies.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to clear cookies.")

            case MessageKind.CLEAR_HISTORY:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.clear_history()
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to clear history.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to clear browsing history.")

            case MessageKind.CLIPBOARD_COPY:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        copied_text = await execute.get_selected_text()
                        await message_channel.send_copied_text(copied_text)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to copy text from browser.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to copy selected text.")

            case MessageKind.CLIPBOARD_PASTE:
                text = message.text

                paste_byte_len = len(text.encode("utf-8"))
                if paste_byte_len > MAX_CLIPBOARD_PASTE_BYTES:
                    LOG.warning(
                        f"{class_name} clipboard-paste payload exceeds size cap; rejecting.",
                        size=paste_byte_len,
                        max_size=MAX_CLIPBOARD_PASTE_BYTES,
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Clipboard payload too large.")
                    return

                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.paste_text(text)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to paste text into browser.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to paste into browser.")

            case MessageKind.END_EXFILTRATION:
                if exfiltration_channel is None:
                    return

                await exfiltration_channel.stop()

                exfiltration_channel = None
                if live_interpretation_browser_session_id:
                    await interpretation_registry.stop_session(live_interpretation_browser_session_id)
                    live_interpretation_browser_session_id = None

            case MessageKind.RECORDING_CAPTURE_PAUSE:
                if exfiltration_channel is not None:
                    exfiltration_channel.pause_capture()
                if live_interpretation_browser_session_id:
                    interpretation_registry.pause_capture(live_interpretation_browser_session_id)

            case MessageKind.RECORDING_CAPTURE_RESUME:
                if exfiltration_channel is not None:
                    exfiltration_channel.resume_capture()
                if live_interpretation_browser_session_id:
                    interpretation_registry.resume_capture(live_interpretation_browser_session_id)

            case MessageKind.RECORDING_REARM_CAPTURE:
                if exfiltration_channel is not None:
                    await exfiltration_channel.rearm_all_pages()

            case MessageKind.GET_BROWSER_URL:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        url = await execute.get_current_url()
                        url_message = MessageOutBrowserUrl(url=url)
                        await send(url_message)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to get browser URL.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to read browser URL.")

            case MessageKind.GO_BACK:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.go_back()
                        await send_current_browser_url(execute)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to go back.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Couldn't go back.")

            case MessageKind.GO_FORWARD:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.go_forward()
                        await send_current_browser_url(execute)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to go forward.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Couldn't go forward.")

            case MessageKind.NAVIGATE:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.navigate(message.url)
                        # Push the new URL back so the URL input reflects it immediately,
                        # rather than waiting for the next poll.
                        await send_current_browser_url(execute)
                except ValueError as exc:
                    LOG.warning(
                        f"{class_name} rejected navigate.",
                        url=message.url,
                        error=str(exc),
                        **message_channel.identity,
                    )
                    await send_error(message.kind, str(exc))
                except Exception:
                    # The target URL is user-controlled; log it server-side
                    # but don't reflect it back in the toast.
                    LOG.exception(
                        f"{class_name} failed to navigate.",
                        url=message.url,
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Navigation failed.")

            case MessageKind.RELOAD:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        await execute.reload(hard=message.hard)
                        await send_current_browser_url(execute)
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to reload.",
                        hard=message.hard,
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to reload.")

            case MessageKind.TAKE_CONTROL:
                LOG.info(f"{class_name} processing take-control message.", **message_channel.identity)
                vnc_channel = get_vnc_channel(message_channel.client_id)

                if not vnc_channel:
                    LOG.error(
                        f"{class_name} no vnc channel client found for message channel.",
                        message=message,
                        **message_channel.identity,
                    )
                    return
                vnc_channel.interactor = "user"

            case MessageKind.TAKE_SCREENSHOT:
                try:
                    async with execution_for_message_channel(message_channel) as execute:
                        screenshot_b64 = await execute.take_screenshot()
                        await send(MessageOutScreenshot(data=screenshot_b64))
                except Exception:
                    LOG.exception(
                        f"{class_name} failed to take screenshot.",
                        **message_channel.identity,
                    )
                    await send_error(message.kind, "Failed to capture screenshot.")

            case _:
                t.assert_never(message.kind)

    async def frontend_to_backend() -> None:
        nonlocal class_name

        LOG.debug(f"{class_name} starting frontend-to-backend loop.", **message_channel.identity)

        while message_channel.is_open:
            try:
                data = await message_channel.websocket.receive_json()
                await handle_data(data)
            except WebSocketDisconnect:
                LOG.debug(f"{class_name} frontend disconnected.", **message_channel.identity)
                raise
            except ConnectionClosedError:
                LOG.debug(f"{class_name} frontend closed channel.", **message_channel.identity)
                raise
            except RuntimeError as ex:
                # Starlette raises a bare RuntimeError when receive() races a close.
                if "not connected" in str(ex).lower():
                    LOG.debug(f"{class_name} frontend no longer connected.", **message_channel.identity)
                    return
                LOG.exception(f"{class_name} An unexpected exception occurred.", **message_channel.identity)
                raise
            except Exception:
                LOG.exception(f"{class_name} An unexpected exception occurred.", **message_channel.identity)
                raise

    async def backend_to_frontend() -> None:
        LOG.debug(f"{class_name} starting backend-to-frontend loop.", **message_channel.identity)

        # Sole websocket writer: wakes the moment a message is enqueued instead of
        # polling, and keeps sending while frontend_to_backend awaits a slow
        # command handler (no head-of-line blocking for interpretation updates).
        while message_channel.is_open:
            message = await message_channel.out_queue.get()

            if message_channel.websocket.client_state != WebSocketState.CONNECTED:
                return

            # Clipboard frames are enqueued as pre-built dicts; everything else is
            # a MessageOut that needs serializing (NaN-sanitized) first.
            data = message if isinstance(message, dict) else message_to_dict(message)

            try:
                await message_channel.websocket.send_json(data)
            except WebSocketDisconnect:
                return
            except Exception:
                # The try only wraps the socket write, so a failure here means the
                # connection is unhealthy (e.g. Starlette's RuntimeError after a
                # close frame) — end the session instead of log-spinning per message.
                LOG.exception("MessageChannel: failed to send data.")
                return

    loops = [
        asyncio.create_task(frontend_to_backend()),
        asyncio.create_task(backend_to_frontend()),
    ]

    try:
        # FIRST_COMPLETED (not collect's FIRST_EXCEPTION): a clean return from
        # either side must also tear down its sibling, which otherwise blocks
        # forever on receive_json()/out_queue.get().
        done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() is not None:
                raise t.cast(Exception, task.exception())
    except Exception:
        LOG.exception(f"{class_name} An exception occurred in loop message channel stream.", **message_channel.identity)
    finally:
        # Cancel both loops on every exit path — including when loop_stream_messages
        # is itself cancelled mid-wait (collect() cancels it when the sibling verify
        # loop raises). asyncio.wait does NOT cancel the tasks it awaits, so without
        # this backend_to_frontend would be stranded on out_queue.get() forever.
        for task in loops:
            task.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        LOG.debug(f"{class_name} Closing the message channel stream.", **message_channel.identity)
        if exfiltration_channel is not None:
            await exfiltration_channel.stop()
        # Live interpretation is torn down only on explicit end-exfiltration so a
        # disconnect→reconnect can resume the same in-flight recording session.
        await message_channel.close(reason="loop-channel-closed")


async def get_message_channel_for_browser_session(
    client_id: str,
    browser_session_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[MessageChannel, Loops] | None:
    """
    Return a message channel for a browser session, with a list of loops to run concurrently.
    """

    browser_session = await verify_browser_session(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        return None

    message_channel = MessageChannel(
        client_id=client_id,
        organization_id=organization_id,
        browser_session=browser_session,
        websocket=websocket,
    )

    loops = [
        asyncio.create_task(loop_verify_browser_session(message_channel)),
        asyncio.create_task(loop_stream_messages(message_channel)),
    ]

    return message_channel, loops


async def get_message_channel_for_workflow_run(
    client_id: str,
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[MessageChannel, Loops] | None:
    """
    Return a message channel for a workflow run, with a list of loops to run concurrently.
    """

    LOG.debug("Getting message channel for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.debug(
            "Message channel: no initial workflow run found.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
        return None

    if not browser_session:
        return None

    message_channel = MessageChannel(
        client_id,
        organization_id,
        browser_session=browser_session,
        websocket=websocket,
        workflow_run=workflow_run,
    )

    LOG.debug("Got message channel for workflow run.", message_channel=message_channel)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(message_channel)),
        asyncio.create_task(loop_stream_messages(message_channel)),
    ]

    return message_channel, loops
