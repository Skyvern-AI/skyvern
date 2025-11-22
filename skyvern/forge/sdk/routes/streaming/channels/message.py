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
import typing as t

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosedError

from skyvern.forge.sdk.routes.streaming.channels.execution import execution_channel
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


MessageKinds = t.Literal[
    "ask-for-clipboard-response",
    "cede-control",
    "take-control",
]


@dataclasses.dataclass
class Message:
    kind: MessageKinds


@dataclasses.dataclass
class MessageInTakeControl(Message):
    kind: t.Literal["take-control"] = "take-control"


@dataclasses.dataclass
class MessageInCedeControl(Message):
    kind: t.Literal["cede-control"] = "cede-control"


@dataclasses.dataclass
class MessageInAskForClipboardResponse(Message):
    kind: t.Literal["ask-for-clipboard-response"] = "ask-for-clipboard-response"
    text: str = ""


ChannelMessage = t.Union[
    MessageInAskForClipboardResponse,
    MessageInCedeControl,
    MessageInTakeControl,
]


def reify_channel_message(data: dict) -> ChannelMessage:
    kind = data.get("kind", None)

    match kind:
        case "ask-for-clipboard-response":
            text = data.get("text") or ""
            return MessageInAskForClipboardResponse(text=text)
        case "cede-control":
            return MessageInCedeControl()
        case "take-control":
            return MessageInTakeControl()
        case _:
            raise ValueError(f"Unknown message kind: '{kind}'")


@dataclasses.dataclass
class MessageChannel:
    """
    A message channel for streaming JSON messages between our frontend and our API server.
    """

    client_id: str
    organization_id: str
    websocket: WebSocket
    # --
    out_queue: asyncio.Queue = dataclasses.field(default_factory=asyncio.Queue)  # warn: unbounded
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

    async def drain(self) -> list[dict]:
        datums: list[dict] = []

        tasks = [
            asyncio.create_task(self.receive_from_out_queue()),
            asyncio.create_task(self.receive_from_user()),
        ]

        results = await asyncio.gather(*tasks)

        for result in results:
            datums.extend(result)

        if datums:
            LOG.info(f"{self.class_name} Drained {len(datums)} messages from message channel.", **self.identity)

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
            except Exception:
                LOG.exception(f"{self.class_name} Failed to receive message from message channel", **self.identity)
                break

        return datums

    async def receive_from_out_queue(self) -> list[dict]:
        datums: list[dict] = []

        while True:
            try:
                data = await asyncio.wait_for(self.out_queue.get(), timeout=0.001)
                datums.append(data)
            except asyncio.TimeoutError:
                break
            except asyncio.QueueEmpty:
                break

        return datums

    def receive_from_out_queue_nowait(self) -> list[dict]:
        datums: list[dict] = []

        while True:
            try:
                data = self.out_queue.get_nowait()
                datums.append(data)
            except asyncio.QueueEmpty:
                break

        return datums

    async def send(self, *, messages: list[dict]) -> t.Self:
        for message in messages:
            await self.out_queue.put(message)

        return self

    def send_nowait(self, *, messages: list[dict]) -> t.Self:
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

    async def handle_data(data: dict) -> None:
        nonlocal class_name

        try:
            message = reify_channel_message(data)
        except ValueError:
            LOG.error(f"MessageChannel: cannot reify channel message from data: {data}", **message_channel.identity)
            return

        message_kind = message.kind

        match message_kind:
            case "ask-for-clipboard-response":
                if not isinstance(message, MessageInAskForClipboardResponse):
                    LOG.error(
                        f"{class_name} invalid message type for ask-for-clipboard-response.",
                        message=message,
                        **message_channel.identity,
                    )
                    return

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

            case "cede-control":
                vnc_channel = get_vnc_channel(message_channel.client_id)

                if not vnc_channel:
                    LOG.error(
                        f"{class_name} no vnc channel client found for message channel.",
                        message=message,
                        **message_channel.identity,
                    )
                    return
                vnc_channel.interactor = "agent"

            case "take-control":
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
                LOG.error(f"{class_name} unknown message kind: '{message_kind}'", **message_channel.identity)
                return

    async def frontend_to_backend() -> None:
        nonlocal class_name

        LOG.info(f"{class_name} starting frontend-to-backend loop.", **message_channel.identity)

        while message_channel.is_open:
            try:
                datums = await message_channel.drain()

                for data in datums:
                    if not isinstance(data, dict):
                        LOG.error(
                            f"{class_name} cannot create message: expected dict, got {type(data)}",
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

    LOG.info("Getting message channel for browser session.", browser_session_id=browser_session_id)

    browser_session = await verify_browser_session(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        LOG.info(
            "Message channel: no initial browser session found.",
            browser_session_id=browser_session_id,
            organization_id=organization_id,
        )
        return None

    message_channel = MessageChannel(
        client_id=client_id,
        organization_id=organization_id,
        browser_session=browser_session,
        websocket=websocket,
    )

    LOG.info("Got message channel for browser session.", message_channel=message_channel)

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
        LOG.info(
            "Message channel: no initial browser session found for workflow run.",
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
        )
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
