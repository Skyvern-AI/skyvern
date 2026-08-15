"""One websocket, every target: the multiplexing floor of the raw-CDP engine.

Chrome's flat protocol mode carries every attached session over the browser-level socket, tagged with
``sessionId``. This module owns that socket and nothing else: it assigns command ids, resolves the
matching futures, routes events to (event, session) subscribers, and converts socket death into the
engine's target-closed identity for every request still in flight.

Deliberately absent: any notion of pages, frames, or the DevTools domains themselves. Keeping the
transport domain-blind is what lets the layers above enable only the domains they need — the raw-CDP
engine never calls ``Runtime.enable``, whose side effects are the classic automation tell.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Callable
from typing import Any, Protocol

import structlog

from skyvern.webeye.skycdp.errors import CdpTargetClosedError, CdpTimeoutError, protocol_error

LOG = structlog.get_logger()

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30.0

EventHandler = Callable[[dict[str, Any]], None]


class WebSocketLike(Protocol):
    async def send(self, payload: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class CdpTransport:
    def __init__(self, socket: WebSocketLike, *, default_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> None:
        self._socket = socket
        self._default_timeout = default_timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._methods: dict[int, str] = {}
        self._command_sessions: dict[int, str | None] = {}
        self._subscribers: dict[tuple[str, str | None], list[EventHandler]] = {}
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self._reader: asyncio.Task[None] | None = None
        self._closed = False
        self._close_reason = "connection closed"

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def inflight_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop(), name="skycdp-transport-read")

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise CdpTargetClosedError(f"{method}: {self._close_reason}")

        message_id = next(self._ids)
        payload: dict[str, Any] = {"id": message_id, "method": method, "params": params or {}}
        if session_id is not None:
            payload["sessionId"] = session_id

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        self._methods[message_id] = method
        self._command_sessions[message_id] = session_id

        try:
            await self._socket.send(json.dumps(payload))
        except Exception as exc:
            self._forget(message_id)
            raise CdpTargetClosedError(f"{method}: {exc}") from exc

        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout if timeout is not None else self._default_timeout
            )
        except asyncio.TimeoutError as exc:
            self._forget(message_id)
            raise CdpTimeoutError(f"{method}: timed out after {timeout or self._default_timeout}s") from exc
        except asyncio.CancelledError:
            self._forget(message_id)
            raise
        finally:
            self._forget(message_id)

    def on(self, event: str, handler: EventHandler, *, session_id: str | None = None) -> None:
        self._subscribers.setdefault((event, session_id), []).append(handler)

    def off(self, event: str, handler: EventHandler, *, session_id: str | None = None) -> None:
        handlers = self._subscribers.get((event, session_id))
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            self._subscribers.pop((event, session_id), None)

    def drop_session_subscribers(self, session_id: str) -> None:
        for key in [key for key in self._subscribers if key[1] == session_id]:
            self._subscribers.pop(key, None)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        self._disconnect_callbacks.append(callback)

    async def close(self) -> None:
        self._mark_closed("transport closed")
        try:
            await self._socket.close()
        except Exception:
            LOG.debug("skycdp transport socket close raised", exc_info=True)
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._socket.recv()
                try:
                    message = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    LOG.warning("skycdp received a non-JSON frame; ignoring")
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_closed(str(exc) or "connection closed")

    def _dispatch(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if message_id is not None:
            future = self._pending.get(message_id)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(protocol_error(self._methods.get(message_id, "?"), message["error"]))
            else:
                future.set_result(message.get("result") or {})
            return

        method = message.get("method")
        if not method:
            return
        params = message.get("params") or {}
        session_id = message.get("sessionId")
        # A session-scoped event also reaches connection-wide subscribers, which is how session
        # bookkeeping (Target.detachedFromTarget and friends) stays observable in one place.
        for key in ((method, session_id), (method, None)) if session_id else ((method, None),):
            for handler in list(self._subscribers.get(key, ())):
                try:
                    handler(params)
                except Exception:
                    LOG.warning("skycdp event handler raised", cdp_event=method, exc_info=True)

    def fail_session_commands(self, session_id: str, reason: str) -> None:
        """Reject every command still awaiting a reply on a session that has gone away.

        Chrome does not answer commands that were in flight when a session detached, so without this
        the caller waits out its full timeout and then sees a timeout error -- the wrong identity for
        a page that is simply gone, and the one recovery branches key on.
        """
        for message_id in [mid for mid, sid in self._command_sessions.items() if sid == session_id]:
            future = self._pending.get(message_id)
            if future is not None and not future.done():
                future.set_exception(CdpTargetClosedError(f"{self._methods.get(message_id, '?')}: {reason}"))
            self._forget(message_id)

    def _forget(self, message_id: int) -> None:
        self._pending.pop(message_id, None)
        self._methods.pop(message_id, None)
        self._command_sessions.pop(message_id, None)

    def _mark_closed(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_reason = reason
        for message_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(CdpTargetClosedError(f"{self._methods.get(message_id, '?')}: {reason}"))
        self._pending.clear()
        self._methods.clear()
        self._command_sessions.clear()
        self._subscribers.clear()
        for callback in list(self._disconnect_callbacks):
            try:
                callback()
            except Exception:
                LOG.warning("skycdp disconnect callback raised", exc_info=True)
        self._disconnect_callbacks.clear()
