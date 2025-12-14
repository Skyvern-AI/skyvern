"""
A channel for streaming the VNC protocol data between our frontend and a
persistent browser instance.

This is a pass-thru channel, through our API server. As such, we can monitor and/or
intercept RFB protocol messages as needed.

What this channel looks like:

    [Skyvern App] <--> [API Server] <--> [websockified noVNC] <--> [Browser]


Channel data:

    One could call this RFB over WebSockets (rockets?), as the protocol data streaming
    over the WebSocket is raw RFB protocol data.
"""

import asyncio
import dataclasses
import typing as t
from enum import IntEnum
from urllib.parse import urlparse

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets import ConnectionClosedError, Data

from skyvern.config import settings
from skyvern.forge.sdk.routes.streaming.auth import get_x_api_key
from skyvern.forge.sdk.routes.streaming.channels.execution import execution_channel
from skyvern.forge.sdk.routes.streaming.registries import (
    add_vnc_channel,
    del_vnc_channel,
    get_message_channel,
    get_vnc_channel,
)
from skyvern.forge.sdk.routes.streaming.verify import (
    loop_verify_browser_session,
    loop_verify_task,
    loop_verify_workflow_run,
    verify_browser_session,
    verify_task,
    verify_workflow_run,
)
from skyvern.forge.sdk.schemas.persistent_browser_sessions import AddressablePersistentBrowserSession
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.forge.sdk.utils.aio import collect
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun

LOG = structlog.get_logger()


Interactor = t.Literal["agent", "user"]
"""
NOTE: we don't really have an "agent" at this time. But any control of the
browser that is not user-originated is kinda' agent-like, by some
definition of "agent". Here, we do not have an "AI agent". Future work may
alter this state of affairs - and some "agent" could operate the browser
automatically. In any case, if the interactor is not a "user", we assume
it is an "agent".
"""


Loops = list[asyncio.Task]  # aka "queue-less actors"; or "programs"


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
class VncChannel:
    """
    A VNC channel for streaming RFB protocol data between our frontend app, and
    a remote browser.
    """

    client_id: str
    """
    Unique to a frontend app instance.
    """

    organization_id: str
    vnc_port: int
    x_api_key: str
    websocket: WebSocket

    initial_interactor: dataclasses.InitVar[Interactor]
    """
    The role of the entity interacting with the channel, either "agent" or "user".
    """

    _interactor: Interactor = dataclasses.field(init=False, repr=False)

    # --

    browser_session: AddressablePersistentBrowserSession | None = None
    key_state: KeyState = dataclasses.field(default_factory=KeyState)
    task: Task | None = None
    workflow_run: WorkflowRun | None = None

    def __post_init__(self, initial_interactor: Interactor) -> None:
        self.interactor = initial_interactor
        add_vnc_channel(self)

    @property
    def class_name(self) -> str:
        return self.__class__.__name__

    @property
    def identity(self) -> dict:
        base = {"organization_id": self.organization_id}

        if self.task:
            return base | {"task_id": self.task.task_id}
        elif self.workflow_run:
            return base | {"workflow_run_id": self.workflow_run.workflow_run_id}
        elif self.browser_session:
            return base | {"browser_session_id": self.browser_session.persistent_browser_session_id}
        else:
            return base | {"client_id": self.client_id}

    @property
    def interactor(self) -> Interactor:
        return self._interactor

    @interactor.setter
    def interactor(self, value: Interactor) -> None:
        self._interactor = value

        LOG.info(f"{self.class_name} Setting interactor to {value}", **self.identity)

    @property
    def is_open(self) -> bool:
        if self.websocket.client_state != WebSocketState.CONNECTED:
            return False

        if not self.task and not self.workflow_run and not self.browser_session:
            return False

        if not get_vnc_channel(self.client_id):
            return False

        return True

    async def close(self, code: int = 1000, reason: str | None = None) -> t.Self:
        LOG.info(f"{self.class_name} closing.", reason=reason, code=code, **self.identity)

        self.browser_session = None
        self.task = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_vnc_channel(self.client_id)

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


async def copy_text(vnc_channel: VncChannel) -> None:
    class_name = vnc_channel.class_name
    LOG.info(f"{class_name} Retrieving selected text via CDP", **vnc_channel.identity)

    try:
        async with execution_channel(vnc_channel) as execute:
            copied_text = await execute.get_selected_text()

            message_channel = get_message_channel(vnc_channel.client_id)

            if message_channel:
                await message_channel.send_copied_text(copied_text)
            else:
                LOG.warning(
                    f"{class_name} No message channel found for client, or it is not open",
                    message_channel=message_channel,
                    **vnc_channel.identity,
                )
    except Exception:
        LOG.exception(f"{class_name} Failed to retrieve selected text via CDP", **vnc_channel.identity)


async def ask_for_clipboard(vnc_channel: VncChannel) -> None:
    class_name = vnc_channel.class_name
    LOG.info(f"{class_name} Asking for clipboard data via CDP", **vnc_channel.identity)

    try:
        message_channel = get_message_channel(vnc_channel.client_id)

        if message_channel:
            await message_channel.ask_for_clipboard()
        else:
            LOG.warning(
                f"{class_name} No message channel found for client, or it is not open",
                message_channel=message_channel,
                **vnc_channel.identity,
            )
    except Exception:
        LOG.exception(f"{class_name} Failed to ask for clipboard via CDP", **vnc_channel.identity)


async def loop_stream_vnc(vnc_channel: VncChannel) -> None:
    """
    Actually stream the VNC data between a frontend and a browser.

    Loops until the task is cleared or the websocket is closed.
    """

    vnc_url: str = ""
    browser_session = vnc_channel.browser_session
    class_name = vnc_channel.class_name

    if browser_session:
        if browser_session.ip_address:
            if ":" in browser_session.ip_address:
                ip, _ = browser_session.ip_address.split(":")
                vnc_url = f"ws://{ip}:{vnc_channel.vnc_port}"
            else:
                vnc_url = f"ws://{browser_session.ip_address}:{vnc_channel.vnc_port}"
        else:
            browser_address = browser_session.browser_address

            parsed_browser_address = urlparse(browser_address)
            host = parsed_browser_address.hostname
            vnc_url = f"ws://{host}:{vnc_channel.vnc_port}"
    else:
        raise Exception(f"{class_name} No browser session associated with vnc channel.")

    # NOTE(jdo:streaming-local-dev)
    # vnc_url = "ws://localhost:6080"

    LOG.info(
        f"{class_name} Connecting to vnc url.",
        vnc_url=vnc_url,
        **vnc_channel.identity,
    )

    async with websockets.connect(vnc_url) as novnc_ws:

        async def frontend_to_browser() -> None:
            nonlocal class_name

            LOG.info(f"{class_name} Starting frontend-to-browser data transfer.", **vnc_channel.identity)
            data: Data | None = None

            while vnc_channel.is_open:
                try:
                    data = await vnc_channel.websocket.receive_bytes()

                    if data:
                        message_type = data[0]

                        if message_type == MessageType.Keyboard.value:
                            vnc_channel.update_key_state(data)

                            if vnc_channel.key_state.is_copy(data):
                                await copy_text(vnc_channel)

                            if vnc_channel.key_state.is_paste(data):
                                await ask_for_clipboard(vnc_channel)

                            if vnc_channel.key_state.is_forbidden(data):
                                continue

                        # prevent right-mouse-button clicks for "security" reasons
                        if message_type == MessageType.Mouse.value:
                            if Mouse.Up.Right(data):
                                continue

                        if not vnc_channel.interactor == "user" and message_type in (
                            MessageType.Keyboard.value,
                            MessageType.Mouse.value,
                        ):
                            LOG.debug(f"{class_name} Blocking user message.", **vnc_channel.identity)
                            continue

                except WebSocketDisconnect:
                    LOG.info(f"{class_name} Frontend disconnected.", **vnc_channel.identity)
                    raise
                except ConnectionClosedError:
                    LOG.info(f"{class_name} Frontend closed the vnc channel.", **vnc_channel.identity)
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(f"{class_name} An unexpected exception occurred.", **vnc_channel.identity)
                    raise

                if not data:
                    continue

                try:
                    await novnc_ws.send(data)
                except WebSocketDisconnect:
                    LOG.info(f"{class_name} Browser disconnected from vnc.", **vnc_channel.identity)
                    raise
                except ConnectionClosedError:
                    LOG.info(f"{class_name} Browser closed vnc.", **vnc_channel.identity)
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(
                        f"{class_name} An unexpected exception occurred in frontend-to-browser loop.",
                        **vnc_channel.identity,
                    )
                    raise

        async def browser_to_frontend() -> None:
            nonlocal class_name

            LOG.info(f"{class_name} Starting browser-to-frontend data transfer.", **vnc_channel.identity)
            data: Data | None = None

            while vnc_channel.is_open:
                try:
                    data = await novnc_ws.recv()

                except WebSocketDisconnect:
                    LOG.info(f"{class_name} Browser disconnected from the vnc channel session.", **vnc_channel.identity)
                    await vnc_channel.close(reason="browser-disconnected")
                except ConnectionClosedError:
                    LOG.info(f"{class_name} Browser closed the vnc channel session.", **vnc_channel.identity)
                    await vnc_channel.close(reason="browser-closed")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(
                        f"{class_name} An unexpected exception occurred in browser-to-frontend loop.",
                        **vnc_channel.identity,
                    )
                    raise

                if not data:
                    continue

                try:
                    if vnc_channel.websocket.client_state != WebSocketState.CONNECTED:
                        continue
                    await vnc_channel.websocket.send_bytes(data)
                except WebSocketDisconnect:
                    LOG.info(
                        f"{class_name} Frontend disconnected from the vnc channel session.", **vnc_channel.identity
                    )
                    await vnc_channel.close(reason="frontend-disconnected")
                except ConnectionClosedError:
                    LOG.info(f"{class_name} Frontend closed the vnc channel session.", **vnc_channel.identity)
                    await vnc_channel.close(reason="frontend-closed")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOG.exception(f"{class_name} An unexpected exception occurred.", **vnc_channel.identity)
                    raise

        loops = [
            asyncio.create_task(frontend_to_browser()),
            asyncio.create_task(browser_to_frontend()),
        ]

        try:
            await collect(loops)
        except WebSocketDisconnect:
            pass
        except Exception:
            LOG.exception(f"{class_name} An exception occurred in loop stream.", **vnc_channel.identity)
        finally:
            LOG.info(f"{class_name} Closing the loop stream.", **vnc_channel.identity)
            await vnc_channel.close(reason="loop-stream-vnc-closed")


async def get_vnc_channel_for_browser_session(
    client_id: str,
    browser_session_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[VncChannel, Loops] | None:
    """
    Return a vnc channel for a browser session, with a list of loops to run concurrently.
    """

    browser_session = await verify_browser_session(
        browser_session_id=browser_session_id,
        organization_id=organization_id,
    )

    if not browser_session:
        return None

    x_api_key = await get_x_api_key(organization_id)

    try:
        vnc_channel = VncChannel(
            client_id=client_id,
            initial_interactor="agent",
            organization_id=organization_id,
            vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
            browser_session=browser_session,
            x_api_key=x_api_key,
            websocket=websocket,
        )
    except Exception as e:
        LOG.exception("Failed to create VncChannel.", error=str(e))
        return None

    LOG.info("Got vnc context for browser session.", vnc_channel=vnc_channel)

    loops = [
        asyncio.create_task(loop_verify_browser_session(vnc_channel)),
        asyncio.create_task(loop_stream_vnc(vnc_channel)),
    ]

    return vnc_channel, loops


async def get_vnc_channel_for_task(
    client_id: str,
    task_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[VncChannel, Loops] | None:
    """
    Return a vnc channel for a task, with a list of loops to run concurrently.
    """

    task, browser_session = await verify_task(task_id=task_id, organization_id=organization_id)

    if not task:
        LOG.info("No initial task found.", task_id=task_id, organization_id=organization_id)
        return None

    if not browser_session:
        return None

    x_api_key = await get_x_api_key(organization_id)

    vnc_channel = VncChannel(
        client_id=client_id,
        initial_interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        x_api_key=x_api_key,
        websocket=websocket,
        browser_session=browser_session,
        task=task,
    )

    loops = [
        asyncio.create_task(loop_verify_task(vnc_channel)),
        asyncio.create_task(loop_stream_vnc(vnc_channel)),
    ]

    return vnc_channel, loops


async def get_vnc_channel_for_workflow_run(
    client_id: str,
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
) -> tuple[VncChannel, Loops] | None:
    """
    Return a vnc channel for a workflow run, with a list of loops to run concurrently.
    """

    LOG.info("Getting vnc channel for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.info("No initial workflow run found.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        return None

    if not browser_session:
        return None

    x_api_key = await get_x_api_key(organization_id)

    vnc_channel = VncChannel(
        client_id=client_id,
        initial_interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        browser_session=browser_session,
        workflow_run=workflow_run,
        x_api_key=x_api_key,
        websocket=websocket,
    )

    LOG.info("Got vnc channel context for workflow run.", vnc_channel=vnc_channel)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(vnc_channel)),
        asyncio.create_task(loop_stream_vnc(vnc_channel)),
    ]

    return vnc_channel, loops
