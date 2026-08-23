"""Target and session bookkeeping over a single flat-mode CDP transport.

One websocket carries every attached target. This layer turns that stream into per-target sessions:
it attaches with ``flatten=true``, re-arms auto-attach on each new session so nested frames and
workers cascade, tracks execution contexts per frame, and tears a session's state down when its
target detaches.

Execution contexts come from ``Runtime.enable``. That domain is enabled deliberately: the repo's own
three-arm probe (``tests/cloud/cdp_proxy_probe/BASELINE.md``) measured no page-observable difference
from an attached client, and suppressing the domain is a documented deadlock — so there is nothing to
buy by withholding it, and frame-scoped evaluation is far simpler with it on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from skyvern.webeye.skycdp.errors import CdpTargetClosedError
from skyvern.webeye.skycdp.transport import CdpTransport

LOG = structlog.get_logger()


@dataclass
class ExecutionContext:
    context_id: int
    frame_id: str | None
    is_default: bool
    name: str


@dataclass
class TargetInfo:
    target_id: str
    type: str
    url: str
    browser_context_id: str | None = None
    # The target that opened this one. Set for window.open and target=_blank, and the only thing
    # that distinguishes a popup from any other new page.
    opener_id: str | None = None


class CdpSession:
    """A session attached to one target. Commands sent here carry its ``sessionId``."""

    def __init__(self, connection: CdpConnection, session_id: str, target: TargetInfo) -> None:
        self._connection = connection
        self.session_id = session_id
        self.target = target
        self.detached = False
        self._contexts: dict[int, ExecutionContext] = {}
        self._frame_contexts: dict[str, int] = {}
        self._context_waiters: list[asyncio.Future[None]] = []

    async def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict:
        if self.detached:
            raise CdpTargetClosedError(f"{method}: session {self.session_id} is detached")
        return await self._connection.transport.send(method, params, session_id=self.session_id, timeout=timeout)

    def on(self, event: str, handler: Callable[[dict], None]) -> None:
        self._connection.transport.on(event, handler, session_id=self.session_id)

    def off(self, event: str, handler: Callable[[dict], None]) -> None:
        self._connection.transport.off(event, handler, session_id=self.session_id)

    # -- execution contexts -------------------------------------------------

    def note_context_created(self, params: dict) -> None:
        description = params.get("context") or {}
        context_id = description.get("id")
        if context_id is None:
            return
        aux = description.get("auxData") or {}
        context = ExecutionContext(
            context_id=int(context_id),
            frame_id=aux.get("frameId"),
            is_default=bool(aux.get("isDefault")),
            name=str(description.get("name", "")),
        )
        self._contexts[context.context_id] = context
        # Only the default (main) world of a frame is addressable by frame id; isolated worlds share
        # the frame but must never shadow it.
        if context.frame_id and context.is_default:
            self._frame_contexts[context.frame_id] = context.context_id
        for waiter in self._context_waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._context_waiters.clear()

    def note_context_destroyed(self, params: dict) -> None:
        context_id = params.get("executionContextId")
        if context_id is None:
            return
        context = self._contexts.pop(int(context_id), None)
        if context and context.frame_id and self._frame_contexts.get(context.frame_id) == context_id:
            self._frame_contexts.pop(context.frame_id, None)

    def note_contexts_cleared(self, _: dict) -> None:
        self._contexts.clear()
        self._frame_contexts.clear()

    def context_for_frame(self, frame_id: str | None) -> int | None:
        if frame_id is None:
            return None
        return self._frame_contexts.get(frame_id)

    def known_frame_ids(self) -> list[str]:
        return list(self._frame_contexts)

    async def wait_for_any_context(self, timeout: float = 5.0) -> None:
        if self._contexts:
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._context_waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            return

    def mark_detached(self) -> None:
        self.detached = True
        self._contexts.clear()
        self._frame_contexts.clear()
        for waiter in self._context_waiters:
            if not waiter.done():
                waiter.set_exception(CdpTargetClosedError("session detached"))
        self._context_waiters.clear()


class CdpConnection:
    """Browser-level connection: owns the transport and every attached session."""

    def __init__(self, transport: CdpTransport) -> None:
        self.transport = transport
        self.sessions: dict[str, CdpSession] = {}
        self.targets: dict[str, TargetInfo] = {}
        self._target_sessions: dict[str, str] = {}
        self._attach_lock = asyncio.Lock()
        self._closed = False
        self._page_listeners: list[Callable[[CdpSession], None]] = []
        self._detach_listeners: list[Callable[[CdpSession], None]] = []
        self._disconnect_listeners: list[Callable[[], None]] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.transport.on("Target.attachedToTarget", self._on_attached)
        self.transport.on("Target.detachedFromTarget", self._on_detached)
        self.transport.on("Target.targetDestroyed", self._on_target_destroyed)
        self.transport.on("Target.targetInfoChanged", self._on_target_info_changed)
        self.transport.on_disconnect(self._on_disconnect)
        await self.transport.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        await self.transport.send("Target.setDiscoverTargets", {"discover": True})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.transport.close()

    @property
    def is_closed(self) -> bool:
        return self._closed or self.transport.is_closed

    def on_page_session(self, callback: Callable[[CdpSession], None]) -> None:
        self._page_listeners.append(callback)

    def on_session_detached(self, callback: Callable[[CdpSession], None]) -> None:
        self._detach_listeners.append(callback)

    def on_disconnected(self, callback: Callable[[], None]) -> None:
        self._disconnect_listeners.append(callback)

    # -- targets ------------------------------------------------------------

    async def list_targets(self) -> list[TargetInfo]:
        result = await self.transport.send("Target.getTargets")
        infos = []
        for raw in result.get("targetInfos", []):
            info = TargetInfo(
                target_id=raw["targetId"],
                type=raw.get("type", ""),
                url=raw.get("url", ""),
                browser_context_id=raw.get("browserContextId"),
            )
            self.targets[info.target_id] = info
            infos.append(info)
        return infos

    async def attach(self, target_id: str) -> CdpSession:
        async with self._attach_lock:
            existing = self._target_sessions.get(target_id)
            if existing and existing in self.sessions:
                return self.sessions[existing]
            result = await self.transport.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session_id = result["sessionId"]
            session = self.sessions.get(session_id)
            if session is None:
                session = self._register(session_id, self.targets.get(target_id) or TargetInfo(target_id, "page", ""))
            return session

    async def attach_supplementary(self, target_id: str) -> CdpSession:
        """A second session on an already-attached target, never the target's primary.

        Domain state -- Fetch patterns, paused requests, enabled domains -- lives per session, not
        per target. A caller that runs Fetch alongside the page's own route machinery therefore
        needs its own session, which is exactly what Playwright's ``new_cdp_session`` attaches.
        """
        result = await self.transport.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = result["sessionId"]
        session = self.sessions.get(session_id)
        if session is None:
            target = self.targets.get(target_id) or TargetInfo(target_id, "page", "")
            session = self._register(session_id, target, primary=False)
        return session

    async def create_target(self, url: str = "about:blank", browser_context_id: str | None = None) -> CdpSession:
        params: dict[str, Any] = {"url": url}
        if browser_context_id:
            params["browserContextId"] = browser_context_id
        result = await self.transport.send("Target.createTarget", params)
        return await self.attach(result["targetId"])

    async def close_target(self, target_id: str) -> None:
        try:
            await self.transport.send("Target.closeTarget", {"targetId": target_id})
        except CdpTargetClosedError:
            return

    # -- session setup ------------------------------------------------------

    def _register(self, session_id: str, target: TargetInfo, *, primary: bool = True) -> CdpSession:
        session = CdpSession(self, session_id, target)
        self.sessions[session_id] = session
        if primary:
            self._target_sessions[target.target_id] = session_id
        self.targets.setdefault(target.target_id, target)
        # Context bookkeeping is subscribed per session rather than connection-wide, because the
        # events carry no target identity in their params -- only the frame's routing does.
        session.on("Runtime.executionContextCreated", session.note_context_created)
        session.on("Runtime.executionContextDestroyed", session.note_context_destroyed)
        session.on("Runtime.executionContextsCleared", session.note_contexts_cleared)
        return session

    async def prepare_page_session(self, session: CdpSession) -> None:
        """Enable the domains a page session needs, and cascade auto-attach to its children."""
        for method, params in (
            ("Page.enable", None),
            ("Runtime.enable", None),
            ("DOM.enable", None),
            # Only one page in a browser is really focused, and Chrome makes an unfocused renderer
            # wait before acknowledging input -- Input.dispatchMouseEvent to a background page blocks
            # for a full five seconds. Focus emulation makes every attached page behave as focused,
            # which is the difference between a pod driving several runs at once and one driving them
            # at roughly 5s per click. Measured: the agent workload goes from 15.4s back to 0.4s.
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
            # Unconditional, not gated on anyone listening: without it a click on a file input opens
            # the operating system's file dialog, which no headless run can dismiss and which blocks
            # the renderer until the action times out. Interception turns that into an event.
            ("Page.setInterceptFileChooserDialog", {"enabled": True}),
            (
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            ),
            # Last, and only after the domains above are enabled: an auto-attached target can arrive
            # paused, and a paused renderer blocks the *opener* -- the click that called window.open
            # never gets its Input.dispatchMouseEvent acknowledged, so the action times out 30s later
            # with nothing to show for it. Enabling instrumentation first and resuming here is the
            # standard order; on a target that was never waiting this is a no-op.
            ("Runtime.runIfWaitingForDebugger", None),
        ):
            try:
                await session.send(method, params)
            except CdpTargetClosedError:
                return
            except Exception:
                LOG.debug("skycdp could not prepare session domain", cdp_method=method, exc_info=True)

    # -- event handlers -----------------------------------------------------

    def _on_attached(self, params: dict) -> None:
        raw = params.get("targetInfo") or {}
        session_id = params.get("sessionId")
        if not session_id or not raw.get("targetId"):
            return
        target = TargetInfo(
            target_id=raw["targetId"],
            type=raw.get("type", ""),
            url=raw.get("url", ""),
            browser_context_id=raw.get("browserContextId"),
            opener_id=raw.get("openerId"),
        )
        if session_id in self.sessions:
            self.sessions[session_id].target = target
            return
        primary_id = self._target_sessions.get(target.target_id)
        if primary_id and primary_id in self.sessions:
            # A second session on a target that already has one is a supplementary attach: raw
            # scoped access, not a new page. Announcing it would build a duplicate Page facade,
            # and mapping it would displace the page's own session.
            self._register(session_id, target, primary=False)
            return
        session = self._register(session_id, target)
        if target.type in ("page", "iframe"):
            asyncio.ensure_future(self._announce_page(session))

    async def _announce_page(self, session: CdpSession) -> None:
        await self.prepare_page_session(session)
        for callback in list(self._page_listeners):
            try:
                callback(session)
            except Exception:
                LOG.warning("skycdp page-session listener raised", exc_info=True)

    def _on_detached(self, params: dict) -> None:
        session_id = params.get("sessionId")
        if not session_id:
            return
        self._drop_session(session_id)

    def _on_target_destroyed(self, params: dict) -> None:
        target_id = params.get("targetId")
        if not target_id:
            return
        self.targets.pop(target_id, None)
        self._target_sessions.pop(target_id, None)
        # The mapping names only the primary; a target can also carry supplementary sessions, and
        # a destroyed target must reap every one or they stay live in bookkeeping and on the
        # transport's subscriber index.
        for session_id, session in list(self.sessions.items()):
            if session.target.target_id == target_id:
                self._drop_session(session_id, already_unmapped=True)

    def _on_target_info_changed(self, params: dict) -> None:
        raw = params.get("targetInfo") or {}
        target_id = raw.get("targetId")
        if not target_id:
            return
        info = self.targets.get(target_id)
        if info is None:
            self.targets[target_id] = TargetInfo(
                target_id=target_id,
                type=raw.get("type", ""),
                url=raw.get("url", ""),
                browser_context_id=raw.get("browserContextId"),
            )
            return
        info.url = raw.get("url", info.url)

    def _drop_session(self, session_id: str, *, already_unmapped: bool = False) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        # Only the mapped (primary) session owns the target entry; a supplementary session going
        # away must not strand the page's live session without its mapping.
        if not already_unmapped and self._target_sessions.get(session.target.target_id) == session_id:
            self._target_sessions.pop(session.target.target_id, None)
        session.mark_detached()
        self.transport.fail_session_commands(session_id, "session detached")
        self.transport.drop_session_subscribers(session_id)
        for callback in list(self._detach_listeners):
            try:
                callback(session)
            except Exception:
                LOG.warning("skycdp detach listener raised", exc_info=True)

    def _on_disconnect(self) -> None:
        self._closed = True
        for session in list(self.sessions.values()):
            session.mark_detached()
        self.sessions.clear()
        self._target_sessions.clear()
        for callback in list(self._disconnect_listeners):
            try:
                callback()
            except Exception:
                LOG.warning("skycdp disconnect listener raised", exc_info=True)
        self._disconnect_listeners.clear()
