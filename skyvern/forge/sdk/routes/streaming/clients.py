"""
Streaming types.
"""

import asyncio
import dataclasses
import typing as t
from enum import IntEnum

import structlog
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from skyvern.forge.sdk.schemas.persistent_browser_sessions import AddressablePersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun

LOG = structlog.get_logger()


Interactor = t.Literal["agent", "user"]
Loops = list[asyncio.Task]  # aka "queue-less actors"; or "programs"


# Messages


# a global registry for WS message clients
message_channels: dict[str, "MessageChannel"] = {}


def add_message_client(message_channel: "MessageChannel") -> None:
    message_channels[message_channel.client_id] = message_channel


def get_message_client(client_id: str) -> t.Union["MessageChannel", None]:
    return message_channels.get(client_id, None)


def del_message_client(client_id: str) -> None:
    try:
        del message_channels[client_id]
    except KeyError:
        pass


@dataclasses.dataclass
class MessageChannel:
    client_id: str
    organization_id: str
    websocket: WebSocket

    # --

    browser_session: AddressablePersistentBrowserSession | None = None
    workflow_run: WorkflowRun | None = None

    def __post_init__(self) -> None:
        add_message_client(self)

    async def close(self, code: int = 1000, reason: str | None = None) -> "MessageChannel":
        LOG.info("Closing message stream.", reason=reason, code=code)

        self.browser_session = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_message_client(self.client_id)

        return self

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state not in (WebSocketState.CONNECTED, WebSocketState.CONNECTING):
            return False

        if not self.workflow_run and not self.browser_session:
            return False

        if not get_message_client(self.client_id):
            return False

        return True

    async def ask_for_clipboard(self, streaming: "Streaming") -> None:
        try:
            await self.websocket.send_json(
                {
                    "kind": "ask-for-clipboard",
                }
            )
            LOG.info(
                "Sent ask-for-clipboard to message channel",
                organization_id=streaming.organization_id,
            )
        except Exception:
            LOG.exception(
                "Failed to send ask-for-clipboard to message channel",
                organization_id=streaming.organization_id,
            )

    async def send_copied_text(self, copied_text: str, streaming: "Streaming") -> None:
        try:
            await self.websocket.send_json(
                {
                    "kind": "copied-text",
                    "text": copied_text,
                }
            )
            LOG.info(
                "Sent copied text to message channel",
                organization_id=streaming.organization_id,
            )
        except Exception:
            LOG.exception(
                "Failed to send copied text to message channel",
                organization_id=streaming.organization_id,
            )


MessageKinds = t.Literal["take-control", "cede-control", "ask-for-clipboard-response"]


@dataclasses.dataclass
class Message:
    kind: MessageKinds


@dataclasses.dataclass
class MessageTakeControl(Message):
    kind: t.Literal["take-control"] = "take-control"


@dataclasses.dataclass
class MessageCedeControl(Message):
    kind: t.Literal["cede-control"] = "cede-control"


@dataclasses.dataclass
class MessageInAskForClipboardResponse(Message):
    kind: t.Literal["ask-for-clipboard-response"] = "ask-for-clipboard-response"
    text: str = ""


ChannelMessage = t.Union[MessageTakeControl, MessageCedeControl, MessageInAskForClipboardResponse]


def reify_channel_message(data: dict) -> ChannelMessage:
    kind = data.get("kind", None)

    match kind:
        case "take-control":
            return MessageTakeControl()
        case "cede-control":
            return MessageCedeControl()
        case "ask-for-clipboard-response":
            text = data.get("text") or ""
            return MessageInAskForClipboardResponse(text=text)
        case _:
            raise ValueError(f"Unknown message kind: '{kind}'")


# Streaming


# a global registry for WS streaming VNC clients
streaming_clients: dict[str, "Streaming"] = {}


def add_streaming_client(streaming: "Streaming") -> None:
    streaming_clients[streaming.client_id] = streaming


def get_streaming_client(client_id: str) -> t.Union["Streaming", None]:
    return streaming_clients.get(client_id, None)


def del_streaming_client(client_id: str) -> None:
    try:
        del streaming_clients[client_id]
    except KeyError:
        pass


class MessageType(IntEnum):
    Keyboard = 4
    Mouse = 5


class Keys:
    """
    VNC RFB keycodes. There's likely a pithier repr (indexes 6-7). This is ok for now.

    ref: https://www.notion.so/References-21c426c42cd480fb9258ecc9eb8f09b4
    ref: https://github.com/novnc/noVNC/blob/master/docs/rfbproto-3.8.pdf
    """

    class Down:
        Ctrl = b"\x04\x01\x00\x00\x00\x00\xff\xe3"
        Cmd = b"\x04\x01\x00\x00\x00\x00\xff\xe9"
        Alt = b"\x04\x01\x00\x00\x00\x00\xff~"  # option
        CKey = b"\x04\x01\x00\x00\x00\x00\x00c"
        OKey = b"\x04\x01\x00\x00\x00\x00\x00o"
        VKey = b"\x04\x01\x00\x00\x00\x00\x00v"

    class Up:
        Ctrl = b"\x04\x00\x00\x00\x00\x00\xff\xe3"
        Cmd = b"\x04\x00\x00\x00\x00\x00\xff\xe9"
        Alt = b"\x04\x00\x00\x00\x00\x00\xff\x7e"  # option


def is_rmb(data: bytes) -> bool:
    return data[0:2] == b"\x05\x04"


class Mouse:
    class Up:
        Right = is_rmb


@dataclasses.dataclass
class KeyState:
    ctrl_is_down: bool = False
    alt_is_down: bool = False
    cmd_is_down: bool = False

    def is_forbidden(self, data: bytes) -> bool:
        """
        :return: True if the key is forbidden, else False
        """
        return self.is_ctrl_o(data)

    def is_ctrl_o(self, data: bytes) -> bool:
        """
        Do not allow the opening of files.
        """
        return self.ctrl_is_down and data == Keys.Down.OKey

    def is_copy(self, data: bytes) -> bool:
        """
        Detect Ctrl+C or Cmd+C for copy.
        """
        return (self.ctrl_is_down or self.cmd_is_down) and data == Keys.Down.CKey

    def is_paste(self, data: bytes) -> bool:
        """
        Detect Ctrl+V or Cmd+V for paste.
        """
        return (self.ctrl_is_down or self.cmd_is_down) and data == Keys.Down.VKey


@dataclasses.dataclass
class Streaming:
    """
    Streaming state.
    """

    client_id: str
    """
    Unique to frontend app instance.
    """

    interactor: Interactor
    """
    Whether the user or the agent are the interactor.
    """

    organization_id: str
    vnc_port: int
    x_api_key: str
    websocket: WebSocket

    # --

    browser_session: AddressablePersistentBrowserSession | None = None
    key_state: KeyState = dataclasses.field(default_factory=KeyState)
    task: Task | None = None
    workflow_run: WorkflowRun | None = None

    def __post_init__(self) -> None:
        add_streaming_client(self)

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state not in (WebSocketState.CONNECTED, WebSocketState.CONNECTING):
            return False

        if not self.task and not self.workflow_run and not self.browser_session:
            return False

        if not get_streaming_client(self.client_id):
            return False

        return True

    async def close(self, code: int = 1000, reason: str | None = None) -> "Streaming":
        LOG.info("Closing Streaming.", reason=reason, code=code)

        self.browser_session = None
        self.task = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_streaming_client(self.client_id)

        return self

    def update_key_state(self, data: bytes) -> None:
        if data == Keys.Down.Ctrl:
            self.key_state.ctrl_is_down = True
        elif data == Keys.Up.Ctrl:
            self.key_state.ctrl_is_down = False
        elif data == Keys.Down.Alt:
            self.key_state.alt_is_down = True
        elif data == Keys.Up.Alt:
            self.key_state.alt_is_down = False
        elif data == Keys.Down.Cmd:
            self.key_state.cmd_is_down = True
        elif data == Keys.Up.Cmd:
            self.key_state.cmd_is_down = False
