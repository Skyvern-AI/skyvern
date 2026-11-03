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
import time
import typing as t
from enum import IntEnum
from urllib.parse import parse_qs, urlparse

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets import ConnectionClosedError, ConnectionClosedOK, Data

from skyvern.config import settings
from skyvern.exceptions import MissingRoutedVncAddressError
from skyvern.forge.sdk.routes.streaming.auth import get_x_api_key
from skyvern.forge.sdk.routes.streaming.channels.execution import execution_channel
from skyvern.forge.sdk.routes.streaming.run_stream_outcome import RunStreamConnection
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
from skyvern.forge.sdk.streaming.registries import (
    add_vnc_channel,
    del_vnc_channel,
    get_message_channel,
    get_vnc_channel,
)
from skyvern.forge.sdk.utils.aio import collect
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun
from skyvern.webeye.cdp_connection import redact_cdp_url
from skyvern.webeye.cdp_credentials import LIVE_VIEW_PATH_PREFIX

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
    ClientCutText = 6


REMOTE_CLIPBOARD_SYNC_PASTE_GRACE_SECONDS = 2.0
STREAM_AUTHORIZATION_RECHECK_SECONDS = 5.0


class Keys:
    """
    VNC RFB keycodes. There's likely a pithier repr (indexes 6-7). This is ok for now.

    ref: https://www.notion.so/References-21c426c42cd480fb9258ecc9eb8f09b4
    ref: https://github.com/novnc/noVNC/blob/master/docs/rfbproto-3.8.pdf
    """

    class Down:
        Ctrl = b"\x04\x01\x00\x00\x00\x00\xff\xe3"
        Cmd = b"\x04\x01\x00\x00\x00\x00\xff\xe9"
        Super = b"\x04\x01\x00\x00\x00\x00\xff\xeb"  # Cmd, from current frontends
        Alt = b"\x04\x01\x00\x00\x00\x00\xff~"  # option
        CKey = b"\x04\x01\x00\x00\x00\x00\x00c"
        OKey = b"\x04\x01\x00\x00\x00\x00\x00o"
        VKey = b"\x04\x01\x00\x00\x00\x00\x00v"

    class Up:
        Ctrl = b"\x04\x00\x00\x00\x00\x00\xff\xe3"
        Cmd = b"\x04\x00\x00\x00\x00\x00\xff\xe9"
        Super = b"\x04\x00\x00\x00\x00\x00\xff\xeb"
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
    stream_authorizer: t.Callable[[str], t.Awaitable[bool]] | None = dataclasses.field(default=None, repr=False)
    stream_authorization_probe: t.Callable[[], t.Awaitable[bool]] | None = dataclasses.field(default=None, repr=False)
    stream_authorization_reauthorizer: t.Callable[[str], t.Awaitable[bool]] | None = dataclasses.field(
        default=None, repr=False
    )
    _stream_authorized_state: tuple[str | None, str | None, str | None] | None = dataclasses.field(
        init=False, default=None, repr=False
    )
    _stream_authorized_at: float = dataclasses.field(init=False, default=0.0, repr=False)
    _stream_authorization_failed_at: float | None = dataclasses.field(init=False, default=None, repr=False)
    _stream_authorization_attempted_state: tuple[str | None, str | None, str | None] | None = dataclasses.field(
        init=False, default=None, repr=False
    )
    _stream_authorization_attempted_at: float = dataclasses.field(init=False, default=0.0, repr=False)
    _stream_authorization_task: asyncio.Task[bool] | None = dataclasses.field(init=False, default=None, repr=False)
    _stream_authorization_lock: asyncio.Lock = dataclasses.field(init=False, default_factory=asyncio.Lock, repr=False)
    key_state: KeyState = dataclasses.field(default_factory=KeyState)
    remote_clipboard_synced_at: float | None = None
    task: Task | None = None
    workflow_run: WorkflowRun | None = None
    live_stream: RunStreamConnection = dataclasses.field(init=False, repr=False)

    def __post_init__(self, initial_interactor: Interactor) -> None:
        self.interactor = initial_interactor
        # The viewer redials a dropped VNC socket, and a run view falls back to the session's screencast.
        self.live_stream = RunStreamConnection(
            organization_id=self.organization_id,
            viewer_reconnects=True,
            transport="vnc",
            browser_session_id=self.browser_session.persistent_browser_session_id if self.browser_session else None,
        )
        if self.browser_session:
            self.live_stream.observe_session(self.browser_session)
            self._stream_authorized_state = self._authorization_state(self.browser_session)
            self._stream_authorized_at = time.monotonic()
            self._stream_authorization_attempted_state = self._stream_authorized_state
            self._stream_authorization_attempted_at = self._stream_authorized_at
        add_vnc_channel(self)

    @staticmethod
    def _authorization_state(
        session: AddressablePersistentBrowserSession,
    ) -> tuple[str | None, str | None, str | None]:
        return (session.runnable_type, session.runnable_id, session.runnable_generation_id)

    async def authorize_stream_relay(self, *, wait_for_pass: bool = False) -> bool:
        if self.stream_authorizer is None:
            return True

        wait_for_state_change = False
        async with self._stream_authorization_lock:
            session = self.browser_session
            if session is None:
                session_id = None
                task = None
            else:
                state = self._authorization_state(session)
                wait_for_state_change = state != self._stream_authorized_state
                session_id = session.persistent_browser_session_id
                task = self._stream_authorization_task
                due = (
                    state != self._stream_authorization_attempted_state
                    or time.monotonic() - self._stream_authorization_attempted_at
                    >= STREAM_AUTHORIZATION_RECHECK_SECONDS
                )
                if (task is None or task.done()) and due:
                    now = time.monotonic()
                    if (
                        self._stream_authorization_failed_at is not None
                        and now - self._stream_authorization_attempted_at >= STREAM_AUTHORIZATION_RECHECK_SECONDS * 3
                    ):
                        self._stream_authorization_failed_at = None
                    self._stream_authorization_attempted_state = state
                    self._stream_authorization_attempted_at = now
                    task = asyncio.create_task(self._reauthorize_stream(session_id, state))
                    self._stream_authorization_task = task

        if session_id is None:
            await self.close(code=4403, reason="browser-session-access-denied")
            return False
        if (wait_for_pass or wait_for_state_change) and task is not None:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    return False
                raise
        return True

    async def check_stream_authorization(self) -> bool:
        if self.stream_authorizer is not None:
            return await self.authorize_stream_relay()
        if self.stream_authorization_probe is None or self.stream_authorization_reauthorizer is None:
            return True
        try:
            required = await self.stream_authorization_probe()
        except PermissionError:
            await self.close(code=4403, reason="browser-session-access-denied")
            return False
        except Exception:
            LOG.warning("Browser-session enforcement-state check failed; retrying", exc_info=True)
            return True
        if not required:
            return True
        self.stream_authorizer = self.stream_authorization_reauthorizer
        self.stream_authorization_probe = None
        self._stream_authorization_attempted_state = None
        return await self.authorize_stream_relay(wait_for_pass=True)

    async def on_session_verified(self, session: AddressablePersistentBrowserSession | None) -> None:
        self.live_stream.observe_session(session)
        await self.check_stream_authorization()

    async def _reauthorize_stream(self, session_id: str, state: tuple[str | None, str | None, str | None]) -> bool:
        authorizer = self.stream_authorizer
        if authorizer is None:
            return True
        while True:
            attempt_started_at = time.monotonic()
            failed_at = self._stream_authorization_failed_at
            remaining = (
                STREAM_AUTHORIZATION_RECHECK_SECONDS * 3
                if failed_at is None
                else max(0.001, STREAM_AUTHORIZATION_RECHECK_SECONDS * 3 - (attempt_started_at - failed_at))
            )
            try:
                authorized = await asyncio.wait_for(authorizer(session_id), timeout=remaining)
            except PermissionError:
                authorized = False
            except Exception:
                LOG.warning(
                    "Browser-session stream reauthorization failed.",
                    browser_session_id=session_id,
                    organization_id=self.organization_id,
                    exc_info=True,
                )
                authorized = None

            retry_state_change = False
            async with self._stream_authorization_lock:
                session = self.browser_session
                now = time.monotonic()
                if session is None:
                    authorized = False
                elif self._authorization_state(session) != state:
                    state = self._authorization_state(session)
                    session_id = session.persistent_browser_session_id
                    self._stream_authorization_failed_at = None
                    self._stream_authorization_attempted_state = state
                    self._stream_authorization_attempted_at = now
                    continue
                elif authorized is True:
                    self._stream_authorized_state = state
                    self._stream_authorized_at = now
                    self._stream_authorization_failed_at = None
                    self._stream_authorization_attempted_state = state
                    self._stream_authorization_attempted_at = now
                    return True
                elif authorized is None:
                    if self._stream_authorization_failed_at is None:
                        self._stream_authorization_failed_at = attempt_started_at
                    within_grace = now - self._stream_authorization_failed_at < 3 * STREAM_AUTHORIZATION_RECHECK_SECONDS
                    self._stream_authorization_attempted_at = now
                    if within_grace:
                        if state != self._stream_authorized_state:
                            retry_state_change = True
                        else:
                            return True
                    else:
                        retry_state_change = False
                else:
                    retry_state_change = False

            if authorized is None and retry_state_change:
                await asyncio.sleep(STREAM_AUTHORIZATION_RECHECK_SECONDS)
                continue

            LOG.info(
                "Rejecting VNC relay after browser-session authorization failed.",
                browser_session_id=session_id,
                organization_id=self.organization_id,
            )
            await self.close(code=4403, reason="browser-session-access-denied")
            return False

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
        LOG.debug(f"{self.class_name} closing.", reason=reason, code=code, **self.identity)

        task = self._stream_authorization_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.browser_session = None
        self.task = None
        self.workflow_run = None

        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass

        del_vnc_channel(self.client_id, expected=self)

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
        elif data in (Keys.Down.Cmd, Keys.Down.Super):
            self.key_state.cmd_is_down = True
        elif data in (Keys.Up.Cmd, Keys.Up.Super):
            self.key_state.cmd_is_down = False

    def mark_remote_clipboard_synced(self) -> None:
        self.remote_clipboard_synced_at = time.monotonic()

    def remote_clipboard_was_recently_synced(self) -> bool:
        if self.remote_clipboard_synced_at is None:
            return False
        return time.monotonic() - self.remote_clipboard_synced_at <= REMOTE_CLIPBOARD_SYNC_PASTE_GRACE_SECONDS


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
                LOG.info(
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
            LOG.info(
                f"{class_name} No message channel found for client, or it is not open",
                message_channel=message_channel,
                **vnc_channel.identity,
            )
    except Exception:
        LOG.exception(f"{class_name} Failed to ask for clipboard via CDP", **vnc_channel.identity)


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _build_vnc_url_from_browser_address(browser_address: str | None, vnc_port: int) -> str | None:
    """The routed, credential-bearing VNC address for a session, or None if none can be built.

    Both routed shapes carry the same credential their CDP address carries, and both are checked
    at the edge before any byte reaches the browser:

        router:  wss://{host}/{session_id}?token={token}
              -> wss://{host}/vnc/{session_id}?token={token}
        legacy:  wss://{host}/{session_id}/{token}/devtools/browser/{browser_id}
              -> wss://{host}/vnc/{session_id}/{token}

    Both are shapes ``redact_cdp_url`` knows how to mask; the prefix is shared with it and with
    the router's own live-view routing so a rename cannot silently unmask the legacy token.

    A loopback address is the single-host case (local dev, single-container self-hosting): the
    websockify listener is on the API host itself, inside one trust domain, with no edge to route
    through. Every other address returns None rather than naming the browser directly — reaching
    a private pod or VPC address would stream a customer live view with no credential on the
    wire, which is worse than no live view at all (SKY-13287).
    """
    if not browser_address:
        return None

    parsed = urlparse(browser_address)
    if parsed.scheme not in ("wss", "ws"):
        return None
    if parsed.hostname in LOOPBACK_HOSTS:
        return f"ws://{parsed.hostname}:{vnc_port}"

    scheme = "wss" if parsed.scheme == "wss" else "ws"
    path_parts = [part for part in parsed.path.split("/") if part]

    # The query is passed through verbatim rather than re-encoded: it is what the router
    # re-parses to recover the session token, and re-encoding could alter it.
    if len(path_parts) == 1 and parse_qs(parsed.query).get("token"):
        return f"{scheme}://{parsed.netloc}{LIVE_VIEW_PATH_PREFIX}{path_parts[0]}?{parsed.query}"

    if len(path_parts) >= 4 and path_parts[2] == "devtools":
        return f"{scheme}://{parsed.netloc}{LIVE_VIEW_PATH_PREFIX}{path_parts[0]}/{path_parts[1]}"

    return None


async def loop_stream_vnc(vnc_channel: VncChannel) -> None:
    """
    Actually stream the VNC data between a frontend and a browser.

    Loops until the task is cleared or the websocket is closed.
    """

    browser_session = vnc_channel.browser_session
    class_name = vnc_channel.class_name

    if not browser_session:
        raise Exception(f"{class_name} No browser session associated with vnc channel.")

    vnc_url = _build_vnc_url_from_browser_address(browser_session.browser_address, vnc_channel.vnc_port)
    if not vnc_url:
        raise MissingRoutedVncAddressError(browser_session.persistent_browser_session_id)

    LOG.debug(
        f"{class_name} Connecting to vnc url.",
        vnc_url=redact_cdp_url(vnc_url),
        **vnc_channel.identity,
    )

    # For routed VNC URLs (wss://), we need to pass the x-api-key header for authentication
    extra_headers: dict[str, str] = {}
    if vnc_url.startswith("wss://") and vnc_channel.x_api_key:
        extra_headers["x-api-key"] = vnc_channel.x_api_key

    async def authorize_relay() -> bool:
        if vnc_channel.stream_authorizer is None:
            return True
        return await vnc_channel.authorize_stream_relay()

    async with websockets.connect(vnc_url, additional_headers=extra_headers) as novnc_ws:

        async def frontend_to_browser() -> None:
            nonlocal class_name

            LOG.debug(f"{class_name} Starting frontend-to-browser data transfer.", **vnc_channel.identity)
            data: Data | None = None

            while vnc_channel.is_open:
                try:
                    data = await vnc_channel.websocket.receive_bytes()

                    if data and not await authorize_relay():
                        return

                    if data:
                        message_type = data[0]

                        if message_type == MessageType.Keyboard.value:
                            vnc_channel.update_key_state(data)

                            if vnc_channel.key_state.is_copy(data):
                                await copy_text(vnc_channel)

                            if (
                                vnc_channel.key_state.is_paste(data)
                                and not vnc_channel.remote_clipboard_was_recently_synced()
                            ):
                                await ask_for_clipboard(vnc_channel)

                            if vnc_channel.key_state.is_forbidden(data):
                                continue

                        if message_type == MessageType.ClientCutText.value:
                            vnc_channel.mark_remote_clipboard_synced()

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
                    vnc_channel.live_stream.end_first("viewer_left")
                    LOG.debug(f"{class_name} Frontend disconnected.", **vnc_channel.identity)
                    raise
                except ConnectionClosedError:
                    vnc_channel.live_stream.end_first("viewer_left")
                    LOG.debug(f"{class_name} Frontend closed the vnc channel.", **vnc_channel.identity)
                    raise
                except ConnectionClosedOK:
                    vnc_channel.live_stream.end_first("viewer_left")
                    LOG.debug(f"{class_name} Frontend closed the vnc channel cleanly.", **vnc_channel.identity)
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.exception(f"{class_name} An unexpected exception occurred.", **vnc_channel.identity)
                    raise

                if not data:
                    continue

                try:
                    await novnc_ws.send(data)
                except WebSocketDisconnect:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.debug(f"{class_name} Browser disconnected from vnc.", **vnc_channel.identity)
                    raise
                except ConnectionClosedError:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.debug(f"{class_name} Browser closed vnc.", **vnc_channel.identity)
                    raise
                except ConnectionClosedOK:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.debug(f"{class_name} Browser closed vnc cleanly.", **vnc_channel.identity)
                    raise
                except asyncio.CancelledError:
                    pass
                except Exception:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.exception(
                        f"{class_name} An unexpected exception occurred in frontend-to-browser loop.",
                        **vnc_channel.identity,
                    )
                    raise

        async def browser_to_frontend() -> None:
            nonlocal class_name

            LOG.debug(f"{class_name} Starting browser-to-frontend data transfer.", **vnc_channel.identity)
            data: Data | None = None

            while vnc_channel.is_open:
                try:
                    data = await novnc_ws.recv()
                    if data and not await authorize_relay():
                        return

                except WebSocketDisconnect:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.debug(
                        f"{class_name} Browser disconnected from the vnc channel session.", **vnc_channel.identity
                    )
                    await vnc_channel.close(reason="browser-disconnected")
                except ConnectionClosedError:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.debug(f"{class_name} Browser closed the vnc channel session.", **vnc_channel.identity)
                    await vnc_channel.close(reason="browser-closed")
                except ConnectionClosedOK:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.debug(f"{class_name} Browser closed the vnc channel session cleanly.", **vnc_channel.identity)
                    await vnc_channel.close(reason="browser-closed-ok")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    vnc_channel.live_stream.end_first("stream_error")
                    LOG.exception(
                        f"{class_name} An unexpected exception occurred in browser-to-frontend loop.",
                        **vnc_channel.identity,
                    )
                    await vnc_channel.close(reason="browser-error")
                    raise

                if not data:
                    continue

                try:
                    if vnc_channel.websocket.client_state != WebSocketState.CONNECTED:
                        continue
                    await vnc_channel.websocket.send_bytes(data)
                    # The first relayed message is the display server's handshake: the browser is on the wire.
                    vnc_channel.live_stream.frame_sent()
                except WebSocketDisconnect:
                    vnc_channel.live_stream.end_first("viewer_left")
                    LOG.debug(
                        f"{class_name} Frontend disconnected from the vnc channel session.", **vnc_channel.identity
                    )
                    await vnc_channel.close(reason="frontend-disconnected")
                except ConnectionClosedError:
                    vnc_channel.live_stream.end_first("viewer_left")
                    LOG.debug(f"{class_name} Frontend closed the vnc channel session.", **vnc_channel.identity)
                    await vnc_channel.close(reason="frontend-closed")
                except ConnectionClosedOK:
                    vnc_channel.live_stream.end_first("viewer_left")
                    LOG.debug(f"{class_name} Frontend closed the vnc channel session cleanly.", **vnc_channel.identity)
                    await vnc_channel.close(reason="frontend-closed-ok")
                except asyncio.CancelledError:
                    pass
                except Exception:
                    vnc_channel.live_stream.end_first("stream_error")
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
        except ConnectionClosedOK:
            LOG.debug(f"{class_name} Connection closed cleanly in loop stream.", **vnc_channel.identity)
        except Exception:
            vnc_channel.live_stream.end_first("stream_error")
            LOG.exception(f"{class_name} An exception occurred in loop stream.", **vnc_channel.identity)
        finally:
            LOG.debug(f"{class_name} Closing the loop stream.", **vnc_channel.identity)
            await vnc_channel.close(reason="loop-stream-vnc-closed")


async def _stream_authorization_callbacks(
    authorize: t.Callable[[str], t.Awaitable[bool]] | None,
    should_reauthorize: t.Callable[[], t.Awaitable[bool]] | None,
    reauthorize: t.Callable[[str], t.Awaitable[bool]] | None,
    monitor_enforcement: t.Callable[[], t.Awaitable[bool]] | None,
) -> tuple[
    t.Callable[[str], t.Awaitable[bool]] | None,
    t.Callable[[], t.Awaitable[bool]] | None,
    t.Callable[[str], t.Awaitable[bool]] | None,
]:
    if authorize is None:
        return None, None, None
    callback = reauthorize or authorize
    if should_reauthorize is None or await should_reauthorize():
        return callback, None, callback
    if monitor_enforcement is not None and await monitor_enforcement():
        return None, should_reauthorize, callback
    return None, None, None


async def get_vnc_channel_for_browser_session(
    client_id: str,
    browser_session_id: str,
    organization_id: str,
    websocket: WebSocket,
    authorize_stream: t.Callable[[str], t.Awaitable[bool]] | None = None,
    should_reauthorize_stream: t.Callable[[], t.Awaitable[bool]] | None = None,
    reauthorize_stream: t.Callable[[str], t.Awaitable[bool]] | None = None,
    monitor_enforcement: t.Callable[[], t.Awaitable[bool]] | None = None,
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

    if authorize_stream is not None and not await authorize_stream(browser_session_id):
        return None

    (
        stream_authorizer,
        stream_authorization_probe,
        stream_authorization_reauthorizer,
    ) = await _stream_authorization_callbacks(
        authorize_stream, should_reauthorize_stream, reauthorize_stream, monitor_enforcement
    )

    x_api_key = await get_x_api_key(organization_id)

    try:
        vnc_channel = VncChannel(
            client_id=client_id,
            initial_interactor="agent",
            organization_id=organization_id,
            vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
            browser_session=browser_session,
            stream_authorizer=stream_authorizer,
            stream_authorization_probe=stream_authorization_probe,
            stream_authorization_reauthorizer=stream_authorization_reauthorizer,
            x_api_key=x_api_key,
            websocket=websocket,
        )
    except Exception as e:
        LOG.exception("Failed to create VncChannel.", error=str(e))
        return None

    LOG.debug("Got vnc context for browser session.", vnc_channel=vnc_channel)

    loops = [
        asyncio.create_task(loop_verify_browser_session(vnc_channel, on_session=vnc_channel.on_session_verified)),
        asyncio.create_task(loop_stream_vnc(vnc_channel)),
    ]

    return vnc_channel, loops


async def get_vnc_channel_for_task(
    client_id: str,
    task_id: str,
    organization_id: str,
    websocket: WebSocket,
    authorize_stream: t.Callable[[str], t.Awaitable[bool]] | None = None,
    should_reauthorize_stream: t.Callable[[], t.Awaitable[bool]] | None = None,
    reauthorize_stream: t.Callable[[str], t.Awaitable[bool]] | None = None,
    monitor_enforcement: t.Callable[[], t.Awaitable[bool]] | None = None,
) -> tuple[VncChannel, Loops] | None:
    """
    Return a vnc channel for a task, with a list of loops to run concurrently.
    """

    task, browser_session = await verify_task(task_id=task_id, organization_id=organization_id)

    if not task:
        LOG.debug("No initial task found.", task_id=task_id, organization_id=organization_id)
        return None

    if not browser_session:
        return None

    if authorize_stream is not None and not await authorize_stream(browser_session.persistent_browser_session_id):
        return None

    (
        stream_authorizer,
        stream_authorization_probe,
        stream_authorization_reauthorizer,
    ) = await _stream_authorization_callbacks(
        authorize_stream, should_reauthorize_stream, reauthorize_stream, monitor_enforcement
    )

    x_api_key = await get_x_api_key(organization_id)

    vnc_channel = VncChannel(
        client_id=client_id,
        initial_interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        x_api_key=x_api_key,
        websocket=websocket,
        browser_session=browser_session,
        stream_authorizer=stream_authorizer,
        stream_authorization_probe=stream_authorization_probe,
        stream_authorization_reauthorizer=stream_authorization_reauthorizer,
        task=task,
    )

    loops = [
        asyncio.create_task(loop_verify_task(vnc_channel, on_session=vnc_channel.on_session_verified)),
        asyncio.create_task(loop_stream_vnc(vnc_channel)),
    ]

    return vnc_channel, loops


async def get_vnc_channel_for_workflow_run(
    client_id: str,
    workflow_run_id: str,
    organization_id: str,
    websocket: WebSocket,
    authorize_stream: t.Callable[[str], t.Awaitable[bool]] | None = None,
    should_reauthorize_stream: t.Callable[[], t.Awaitable[bool]] | None = None,
    reauthorize_stream: t.Callable[[str], t.Awaitable[bool]] | None = None,
    monitor_enforcement: t.Callable[[], t.Awaitable[bool]] | None = None,
) -> tuple[VncChannel, Loops] | None:
    """
    Return a vnc channel for a workflow run, with a list of loops to run concurrently.
    """

    LOG.debug("Getting vnc channel for workflow run.", workflow_run_id=workflow_run_id)

    workflow_run, browser_session = await verify_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    if not workflow_run:
        LOG.debug("No initial workflow run found.", workflow_run_id=workflow_run_id, organization_id=organization_id)
        return None

    if not browser_session:
        return None

    if authorize_stream is not None and not await authorize_stream(browser_session.persistent_browser_session_id):
        return None

    (
        stream_authorizer,
        stream_authorization_probe,
        stream_authorization_reauthorizer,
    ) = await _stream_authorization_callbacks(
        authorize_stream, should_reauthorize_stream, reauthorize_stream, monitor_enforcement
    )

    x_api_key = await get_x_api_key(organization_id)

    vnc_channel = VncChannel(
        client_id=client_id,
        initial_interactor="agent",
        organization_id=organization_id,
        vnc_port=settings.SKYVERN_BROWSER_VNC_PORT,
        browser_session=browser_session,
        stream_authorizer=stream_authorizer,
        stream_authorization_probe=stream_authorization_probe,
        stream_authorization_reauthorizer=stream_authorization_reauthorizer,
        workflow_run=workflow_run,
        x_api_key=x_api_key,
        websocket=websocket,
    )

    LOG.debug("Got vnc channel context for workflow run.", vnc_channel=vnc_channel)

    loops = [
        asyncio.create_task(loop_verify_workflow_run(vnc_channel, on_session=vnc_channel.on_session_verified)),
        asyncio.create_task(loop_stream_vnc(vnc_channel)),
    ]

    return vnc_channel, loops
