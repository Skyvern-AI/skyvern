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


# Commands


# a global registry for WS command clients
command_channels: dict[str, "CommandChannel"] = {}


def add_command_client(command_channel: "CommandChannel") -> None:
    command_channels[command_channel.client_id] = command_channel


def get_command_client(client_id: str) -> t.Union["CommandChannel", None]:
    return command_channels.get(client_id, None)


def del_command_client(client_id: str) -> None:
    try:
        del command_channels[client_id]
    except KeyError:
        pass


@dataclasses.dataclass
class CommandChannel:
    client_id: str
    organization_id: str
    websocket: WebSocket

    # --

    browser_session: AddressablePersistentBrowserSession | None = None
    workflow_run: WorkflowRun | None = None

    def __post_init__(self) -> None:
        add_command_client(self)

    async def close(self, code: int = 1000, reason: str | None = None) -> "CommandChannel":
        LOG.info("Closing command stream.", reason=reason, code=code)

        self.browser_session = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_command_client(self.client_id)

        return self

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state not in (WebSocketState.CONNECTED, WebSocketState.CONNECTING):
            return False

        if not self.workflow_run and not self.browser_session:
            return False

        if not get_command_client(self.client_id):
            return False

        return True


CommandKinds = t.Literal["take-control", "cede-control"]


@dataclasses.dataclass
class Command:
    kind: CommandKinds


@dataclasses.dataclass
class CommandTakeControl(Command):
    kind: t.Literal["take-control"] = "take-control"


@dataclasses.dataclass
class CommandCedeControl(Command):
    kind: t.Literal["cede-control"] = "cede-control"


ChannelCommand = t.Union[CommandTakeControl, CommandCedeControl]


def reify_channel_command(data: dict) -> ChannelCommand:
    kind = data.get("kind", None)

    match kind:
        case "take-control":
            return CommandTakeControl()
        case "cede-control":
            return CommandCedeControl()
        case _:
            raise ValueError(f"Unknown command kind: '{kind}'")


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
        OKey = b"\x04\x01\x00\x00\x00\x00\x00o"

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
    o_is_down: bool = False

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
