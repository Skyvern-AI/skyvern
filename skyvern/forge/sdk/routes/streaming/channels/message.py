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
import typing as t

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosedError

from skyvern.forge.sdk.routes.streaming.channels.execution import execution_channel
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent, ExfiltrationChannel
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
from skyvern.forge.sdk.utils.aio import collect
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun

LOG = structlog.get_logger()

Loops = list[asyncio.Task]  # aka "queue-less actors"; or "programs"


class MessageKind(enum.StrEnum):
    ASK_FOR_CLIPBOARD_RESPONSE = "ask-for-clipboard-response"
    BEGIN_EXFILTRATION = "begin-exfiltration"
    BROWSER_TABS = "browser-tabs"
    CEDE_CONTROL = "cede-control"
    END_EXFILTRATION = "end-exfiltration"
    EXFILTRATED_EVENT = "exfiltrated-event"
    TAKE_CONTROL = "take-control"


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
    MessageKind.CEDE_CONTROL,
    MessageKind.END_EXFILTRATION,
    MessageKind.EXFILTRATED_EVENT,
    MessageKind.TAKE_CONTROL,
]


@dataclasses.dataclass
class Message:
    kind: MessageKinds


@dataclasses.dataclass
class MessageInBeginExfiltration(Message):
    kind: t.Literal[MessageKind.BEGIN_EXFILTRATION] = MessageKind.BEGIN_EXFILTRATION


@dataclasses.dataclass
class MessageInEndExfiltration(Message):
    kind: t.Literal[MessageKind.END_EXFILTRATION] = MessageKind.END_EXFILTRATION


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
class MessageOutExfiltratedEvent(Message):
    kind: t.Literal[MessageKind.EXFILTRATED_EVENT] = MessageKind.EXFILTRATED_EVENT
    event_name: str = "[not-specified]"

    # TODO(jdo): improve typing for params
    params: dict = dataclasses.field(default_factory=dict)
    source: ExfiltratedEventSource = ExfiltratedEventSource.NOT_SPECIFIED
    timestamp: float = dataclasses.field(default_factory=lambda: 0.0)  # seconds since epoch


@dataclasses.dataclass
class MessageOutTabInfo(Message):
    kind: t.Literal[MessageKind.BROWSER_TABS] = MessageKind.BROWSER_TABS
    tabs: list[TabInfo] = dataclasses.field(default_factory=list)


MessageIn = (
    MessageInAskForClipboardResponse
    | MessageInBeginExfiltration
    | MessageInCedeControl
    | MessageInEndExfiltration
    | MessageInTakeControl
)


MessageOut = MessageOutExfiltratedEvent | MessageOutTabInfo


ChannelMessage = MessageIn | MessageOut


def reify_channel_message(data: dict) -> ChannelMessage:
    kind = data.get("kind", None)

    match kind:
        case MessageKind.ASK_FOR_CLIPBOARD_RESPONSE:
            text = data.get("text") or ""
            return MessageInAskForClipboardResponse(text=text)
        case MessageKind.BEGIN_EXFILTRATION:
            return MessageInBeginExfiltration()
        case MessageKind.CEDE_CONTROL:
            return MessageInCedeControl()
        case MessageKind.END_EXFILTRATION:
            return MessageInEndExfiltration()
        case MessageKind.TAKE_CONTROL:
            return MessageInTakeControl()
        case _:
            raise ValueError(f"Unknown message kind: '{kind}'")


def message_to_dict(message: MessageOut) -> dict:
    """
    Convert message to dict with enums as their values.
    """

    def convert_value(obj: t.Any) -> t.Any:
        if isinstance(obj, enum.Enum):
            return obj.value
        return obj

    return dataclasses.asdict(message, dict_factory=lambda x: {k: convert_value(v) for k, v in x})


@dataclasses.dataclass
class MessageChannel:
    """
    A message channel for streaming JSON messages between our frontend and our API server.
    """

    client_id: str
    organization_id: str
    websocket: WebSocket
    # --
    out_queue: asyncio.Queue[MessageOut] = dataclasses.field(default_factory=asyncio.Queue)  # warn: unbounded
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
        LOG.info(f"{self.class_name} closing message stream.", reason=reason, code=code, **self.identity)

        self.browser_session = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_message_channel(self.client_id)

        return self

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state != WebSocketState.CONNECTED:
            return False

        return True

    async def drain(self) -> list[dict | MessageOut]:
        datums: list[dict | MessageOut] = []

        result = await asyncio.gather(
            self.receive_from_out_queue(),
            self.receive_from_user(),
        )

        # NOTE(jdo): mypy seems to be unable to infer this, whereas pylance has
        # no issue; added explicit type hints here to help mypy out.
        out_queue: list[MessageOut] = result[0]
        in_queue: list[dict] = result[1]

        for out_message in out_queue:
            datums.append(out_message)

        for in_message in in_queue:
            if isinstance(in_message, dict):
                datums.append(in_message)
            else:
                LOG.error(
                    f"{self.class_name} drain dropping user message: unexpected result type: {type(in_message)}",
                    message=in_message,
                    **self.identity,
                )

        if datums:
            LOG.debug(f"{self.class_name} Drained {len(datums)} messages from message channel.", **self.identity)

        return datums

    async def receive_from_user(self) -> list[dict]:
        datums: list[dict] = []

        while True:
            try:
                data = await asyncio.wait_for(self.websocket.receive_json(), timeout=0.001)
                datums.append(data)
            except asyncio.TimeoutError:
                break
            except RuntimeError as ex:
                if "not connected" in str(ex).lower():
                    break
            except WebSocketDisconnect:
                LOG.warning(f"{self.class_name} Disconnected while receiving message from channel", **self.identity)
                break
            except Exception:
                LOG.exception(f"{self.class_name} Failed to receive message from message channel", **self.identity)
                break

        return datums

    async def receive_from_out_queue(self) -> list[MessageOut]:
        datums: list[MessageOut] = []

        while True:
            try:
                data = await asyncio.wait_for(self.out_queue.get(), timeout=0.001)
                datums.append(data)
            except asyncio.TimeoutError:
                break
            except asyncio.QueueEmpty:
                break

        return datums

    def receive_from_out_queue_nowait(self) -> list[MessageOut]:
        datums: list[MessageOut] = []

        while True:
            try:
                data = self.out_queue.get_nowait()
                datums.append(data)
            except asyncio.QueueEmpty:
                break

        return datums

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

        try:
            await self.websocket.send_json(
                {
                    "kind": "ask-for-clipboard",
                }
            )
        except Exception:
            LOG.exception(f"{self.class_name} Failed to send ask-for-clipboard to message channel", **self.identity)

    async def send_copied_text(self, copied_text: str) -> None:
        LOG.info(f"{self.class_name} Sending copied text to message channel", **self.identity)

        try:
            await self.websocket.send_json(
                {
                    "kind": "copied-text",
                    "text": copied_text,
                }
            )
        except Exception:
            LOG.exception(f"{self.class_name} Failed to send copied text to message channel", **self.identity)


async def loop_stream_messages(message_channel: MessageChannel) -> None:
    """
    Stream messages and their results back and forth.

    Loops until the websocket is closed.
    """

    class_name = message_channel.class_name
    exfiltration_channel: ExfiltrationChannel | None = None

    async def send(message: MessageOut) -> None:
        if message_channel.websocket.client_state != WebSocketState.CONNECTED:
            return

        data = message_to_dict(message)

        try:
            await message_channel.websocket.send_json(data)
        except WebSocketDisconnect:
            pass
        except Exception:
            LOG.exception("MessageChannel: failed to send data.")

    async def handle_data(data: dict | MessageOut) -> None:
        nonlocal class_name
        nonlocal exfiltration_channel
        message: ChannelMessage

        if isinstance(data, MessageOut):
            message = data
        elif isinstance(data, dict):
            try:
                message = reify_channel_message(data)
            except ValueError:
                LOG.error(f"MessageChannel: cannot reify channel message from data: {data}", **message_channel.identity)
                return
        else:
            LOG.error(
                f"{class_name} cannot handle data: expected dict or MessageOut, got {type(data)}",
                **message_channel.identity,
            )
            return

        match message.kind:
            case MessageKind.ASK_FOR_CLIPBOARD_RESPONSE:
                vnc_channel = get_vnc_channel(message_channel.client_id)

                if not vnc_channel:
                    LOG.error(
                        f"{class_name} no vnc channel found for message channel.",
                        message=message,
                        **message_channel.identity,
                    )
                    return

                text = message.text

                async with execution_channel(vnc_channel) as execute:
                    await execute.paste_text(text)

            case MessageKind.BEGIN_EXFILTRATION:
                if exfiltration_channel is not None:
                    LOG.error(
                        "MessageChannel: cannot begin exfiltration: already active.", message_channel=message_channel
                    )
                    return

                vnc_channel = get_vnc_channel(message_channel.client_id)

                if not vnc_channel:
                    LOG.error(
                        f"{class_name} no vnc channel client found for message channel - cannot exfiltrate.",
                        message=message,
                        **message_channel.identity,
                    )
                    return

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

                exfiltration_channel = await ExfiltrationChannel(
                    on_event=on_event,
                    vnc_channel=vnc_channel,
                ).start()

            case MessageKind.BROWSER_TABS:
                await send(message)

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

            case MessageKind.END_EXFILTRATION:
                if exfiltration_channel is None:
                    return

                await exfiltration_channel.stop()

                exfiltration_channel = None

            case MessageKind.EXFILTRATED_EVENT:
                await send(message)

            # case MessageKind.GET_TAB_INFO:
            #     """
            #     TODO(jdo): implement - this is an on-demand request for tab info, which is
            #     required when connecting to an existing browser session.
            #     """

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

            case _:
                t.assert_never(message.kind)

    async def frontend_to_backend() -> None:
        nonlocal class_name

        LOG.info(f"{class_name} starting frontend-to-backend loop.", **message_channel.identity)

        while message_channel.is_open:
            try:
                datums = await message_channel.drain()

                for data in datums:
                    if not isinstance(data, (dict, MessageOut)):
                        LOG.error(
                            f"{class_name} cannot handle message: expected dict or MessageOut, got {type(data)}",
                            **message_channel.identity,
                        )
                        continue

                    await handle_data(data)

            except WebSocketDisconnect:
                LOG.info(f"{class_name} frontend disconnected.", **message_channel.identity)
                raise
            except ConnectionClosedError:
                LOG.info(f"{class_name} frontend closed channel.", **message_channel.identity)
                raise
            except Exception:
                LOG.exception(f"{class_name} An unexpected exception occurred.", **message_channel.identity)
                raise

    loops = [
        asyncio.create_task(frontend_to_backend()),
    ]

    try:
        await collect(loops)
    except Exception:
        LOG.exception(f"{class_name} An exception occurred in loop message channel stream.", **message_channel.identity)
    finally:
        LOG.info(f"{class_name} Closing the message channel stream.", **message_channel.identity)
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

    LOG.info("Getting message channel for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info(
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

    LOG.info("Got message channel for workflow run.", message_channel=message_channel)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(message_channel)),
        asyncio.create_task(loop_stream_messages(message_channel)),
    ]

    return message_channel, loops
