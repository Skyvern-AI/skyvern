"""Driving adapter: client-facing CDP WebSocket endpoint.

Accepts a client connection, resolves its session, and relays frames both ways
through the core pipeline. Also serves the HTTP discovery endpoints CDP clients
(Playwright, puppeteer) probe before the WebSocket upgrade — /json/version,
/json/list, /json — returning proxy-scoped debugger URLs so the upstream browser
URL is never exposed. The client-bound relay is decoupled by a bounded outbound
queue so a slow client cannot back up the proxy or the upstream.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import json
import logging
import os
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Mapping, NamedTuple
from urllib.parse import parse_qs, urlsplit

import structlog
import websockets
from websockets.datastructures import Headers, MultipleValuesError
from websockets.http11 import Request, Response

from skyvern.proxy.core.errors import UpstreamConnectError
from skyvern.proxy.core.frames import (
    KNOWN_CDP_DOMAINS,
    LIFECYCLE_EVENTS,
    PROXY_CLIENT_KEY,
    TARGET_ATTACHED_EVENT,
    TARGET_DETACHED_EVENT,
    CdpCommand,
    CdpEvent,
    CdpFrame,
    CdpResponse,
    FrameDecodeError,
    RemapperFullError,
    RequestIdRemapper,
    decode_frame,
    encode_frame,
    params_session_id,
)
from skyvern.proxy.core.pipeline import (
    INTERCEPTOR_FAILURE_REASON,
    Direction,
    InterceptContext,
    MiddlewarePipeline,
    SynthesizedResponse,
    interceptor_failure_response,
)
from skyvern.proxy.core.policy import Drop, Rewrite
from skyvern.proxy.core.policy_pack import POLICY_PACK_EVENT_METHODS
from skyvern.proxy.core.screencast import (
    SCREENCAST_PACK_EVENT_METHODS,
    is_screencast_frame,
    screencast_frame_ack,
)
from skyvern.proxy.core.session import ProxySession, SessionResolutionStatus, UpstreamClosedError
from skyvern.proxy.ports import (
    AuthPort,
    EventPolicyPort,
    MetricsPort,
    SessionRegistryPort,
    UpstreamBrowserPort,
    UpstreamConnection,
)

LOG = structlog.get_logger(__name__)

# websockets DEBUG-logs handshake URIs and headers, which can carry a client's
# URL-borne API key; pin its logger above DEBUG so a directly-embedded driving
# adapter self-suppresses regardless of import order (never rely on a sibling).
logging.getLogger("websockets").setLevel(logging.INFO)

_UNAUTHORIZED_CLOSE_CODE = 4401
_UNKNOWN_CLOSE_CODE = 4404
_UNKNOWN_CLOSE_REASON = "unknown session"
_UPSTREAM_UNAVAILABLE_CLOSE_CODE = 1011
# WS 1013 "try again later" — the client cannot drain fast enough and is closed
# so its backpressure never reaches the upstream browser (the runner-CPU goal).
_BACKPRESSURE_CLOSE_CODE = 1013
# Cap on in-flight commands per connection, applied to both the id-remap table and
# the command start-times; a stalled/unanswered command must never grow either map
# unbounded. Oldest-eviction FIFO — well above real CDP concurrency, so a legitimate
# client's response or latency is never dropped.
_MAX_PENDING_COMMANDS = 4096

# CDP's own code/message for a sessionId the connection cannot address. Reused
# verbatim when refusing a client's command for a session another client owns, so a
# foreign session is indistinguishable from a nonexistent one (no ownership oracle).
_SESSION_NOT_FOUND_CODE = -32001
_SESSION_NOT_FOUND_MESSAGE = "Session with given id not found."

_SET_AUTO_ATTACH_METHOD = "Target.setAutoAttach"
_ATTACH_TO_TARGET_METHOD = "Target.attachToTarget"
# Commands that address a session inside params rather than by the frame's sessionId.
_SESSION_PARAM_METHODS = frozenset({"Target.detachFromTarget", "Target.sendMessageToTarget"})

# Client-bound outbound queue bounds. A dedicated writer drains this queue to the
# client socket, so a slow client blocks only the writer — never the upstream read
# loop. Overflow of EITHER the frame-count or the total-queued-byte bound tail-drops
# droppable events; a client that stays overflowed for close_after consecutive
# frames (or cannot even take a command response) is closed with
# _BACKPRESSURE_CLOSE_CODE. The byte bound stops a slow client pinning a few huge
# frames (large bodies, screencast) from exhausting proxy memory under the count
# bound. Env-tunable per the proxy's own knobs (zero-dep image, no skyvern.config).
_DEFAULT_CLIENT_QUEUE_MAXSIZE = 1024
_DEFAULT_BACKPRESSURE_CLOSE_AFTER = 512
_DEFAULT_CLIENT_QUEUE_MAXBYTES = 64 * 1024 * 1024
_MAX_CLIENT_QUEUE_BOUND = 1_000_000
_MAX_CLIENT_QUEUE_MAXBYTES = 8 * 1024 * 1024 * 1024
# Bounded window to flush queued frames (e.g. a final command response) to the
# client on a clean upstream close before teardown; a client that cannot take the
# drain in time is closed with _BACKPRESSURE_CLOSE_CODE.
_OUTBOUND_DRAIN_TIMEOUT_SECONDS = 5.0

# HTTP discovery targets a client GETs before the WS upgrade, longest suffix first
# so /json/version and /json/list match before the bare /json alias.
_DISCOVERY_SUFFIXES = ("/json/version", "/json/list", "/json")

# Enqueued by the drain path so the writer flushes all real frames then stops.
_DRAIN_SENTINEL = object()


def _positive_env_int(name: str, default: int, maximum: int) -> int:
    """Env int in [1, maximum], falling back to default on any invalid value so a
    misconfig can neither zero the bound nor make it unbounded."""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if 1 <= value <= maximum else default


def resolve_client_queue_limits() -> tuple[int, int, int]:
    return (
        _positive_env_int("CDP_PROXY_CLIENT_QUEUE_MAXSIZE", _DEFAULT_CLIENT_QUEUE_MAXSIZE, _MAX_CLIENT_QUEUE_BOUND),
        _positive_env_int(
            "CDP_PROXY_CLIENT_BACKPRESSURE_CLOSE_AFTER", _DEFAULT_BACKPRESSURE_CLOSE_AFTER, _MAX_CLIENT_QUEUE_BOUND
        ),
        _positive_env_int(
            "CDP_PROXY_CLIENT_QUEUE_MAXBYTES", _DEFAULT_CLIENT_QUEUE_MAXBYTES, _MAX_CLIENT_QUEUE_MAXBYTES
        ),
    )


def _clamp_client_bound(value: int, maximum: int = _MAX_CLIENT_QUEUE_BOUND) -> int:
    """Force an explicit constructor bound into [1, maximum]; a <=0 maxsize would
    make asyncio.Queue unbounded and silently defeat tail-drop / 1013-close."""
    return max(1, min(value, maximum))


class _OutboundQueue:
    """Client-bound queue bounded by BOTH frame count and total queued bytes.

    put_nowait raises asyncio.QueueFull when either bound would be exceeded, so the
    relay's existing tail-drop/1013 policy fires on byte pressure too. A lone frame
    is always admitted onto an empty queue (mirroring the count bound accepting one
    frame) so an over-budget frame can never permanently wedge delivery.
    """

    def __init__(self, max_frames: int, max_bytes: int) -> None:
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=max_frames)
        self._max_bytes = max_bytes
        self._queued_bytes = 0

    def put_nowait(self, wire: str) -> None:
        if self._queued_bytes and self._queued_bytes + len(wire) > self._max_bytes:
            raise asyncio.QueueFull
        self._queue.put_nowait(wire)
        self._queued_bytes += len(wire)

    async def get(self) -> str | object:
        item = await self._queue.get()
        if isinstance(item, str):
            self._queued_bytes -= len(item)
        return item

    def get_nowait(self) -> str:
        item = self._queue.get_nowait()
        assert isinstance(item, str)
        self._queued_bytes -= len(item)
        return item

    def empty(self) -> bool:
        return self._queue.empty()

    async def put_sentinel(self) -> None:
        # Bypasses the byte bound (the sentinel carries none) and waits for a count
        # slot, which the draining writer frees; the outer drain timeout bounds it.
        await self._queue.put(_DRAIN_SENTINEL)


class _ClientChannel:
    """One client's delivery seat on a shared upstream connection.

    The single upstream reader routes frames into `outbound` (drained by the
    client's own writer), so per-client backpressure stays isolated: one slow
    client is tail-dropped and closed without stalling the shared read loop or the
    other clients. `closed` is the teardown signal the reader raises for this
    client (backpressure, or a deterministic close when the upstream dies).
    """

    def __init__(self, client_key: str, outbound: _OutboundQueue) -> None:
        self.client_key = client_key
        self.outbound = outbound
        self.overflow_streak = 0
        self.closed = asyncio.Event()
        self.drain = False
        self.close_code: int | None = None
        self.close_reason = ""

    def signal(self, *, drain: bool, code: int | None, reason: str) -> None:
        if self.closed.is_set():
            return
        self.drain = drain
        self.close_code = code
        self.close_reason = reason
        self.closed.set()

    async def wait_closed(self) -> None:
        await self.closed.wait()


class _AttachIntent(NamedTuple):
    """A client's attach request, recorded when it is sent and settled when its response
    arrives. Tentative on purpose: the announcement it causes precedes the response, so
    the intent is what routes that announcement — but a command that ERRORS is never
    announced, and its intent must not survive to be paired with another client's later
    attach to the same target."""

    upstream_id: int
    client_key: str
    target_id: str | None  # set for an explicit Target.attachToTarget
    enabled_autoattach: bool  # this command is what turned browser-level autoAttach on
    disabled_autoattach: bool  # this command is what turned this client's autoAttach off


@dataclass
class _SharedUpstream:
    """One upstream browser connection shared by every client on the same browser.

    Keyed by the resolved upstream target and ref-counted by the owning server: the
    connection opens on the first client and closes when the last leaves. The
    request-id remapper, per-command latency starts, and the cdp-sessionId owner map
    are shared so one reader can multiplex several clients over the single socket.

    Implements the send half of `UpstreamConnection` (a serialized send so two
    clients' frames never interleave on the wire); the shared reader owns the
    receive half directly. Reconnection/session-resumption would slot into this
    owner without touching clients — out of scope for v1 (seam only, SKY-12499).
    """

    session: ProxySession
    connection: UpstreamConnection | None = None
    connect_task: asyncio.Future[None] | None = None
    reader: asyncio.Task[None] | None = None
    remapper: RequestIdRemapper = field(default_factory=RequestIdRemapper)
    command_starts: dict[int, tuple[str, float]] = field(default_factory=dict)
    session_owner: dict[str, str] = field(default_factory=dict)
    # Clients that enabled browser-level autoAttach, in the order they asked. Chrome
    # auto-attaches once per target on this one shared connection, so the sessions it
    # creates are owned by the first still-connected asker.
    autoattach_clients: list[str] = field(default_factory=list)
    # Attach requests still in flight, oldest first. Chrome announces an attach with an
    # event that lands BEFORE the command response, so the reply cannot be what tells us
    # who to hand that event to — the intent has to be recorded up front and settled
    # afterwards. Ordered, and not keyed by targetId, because two clients may attach to
    # the SAME target: Chrome opens a session per attach and announces them in the order
    # asked, so the announcements pair off against this queue in order.
    attach_intents: list[_AttachIntent] = field(default_factory=list)
    channels: dict[str, _ClientChannel] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refcount: int = 0
    # Set once the reader has exited: the socket is dead and this entry must never be
    # joined by a new client (they dial fresh). Guards the teardown-window race.
    defunct: bool = False
    _client_seq: int = 0
    _closing: bool = False

    def next_client_key(self) -> str:
        self._client_seq += 1
        return f"c{self._client_seq}"

    async def send(self, raw: str) -> None:
        if self.connection is None:
            raise UpstreamClosedError("shared upstream is not connected")
        async with self.send_lock:
            await self.connection.send(raw)

    async def receive(self) -> str:
        raise RuntimeError("the shared connection is read only by its reader task")

    async def close(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Reap the reader and close the socket. Idempotent, teardown-safe."""
        if self._closing:
            return
        self._closing = True
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.connection is not None:
            await self.connection.close()


# Lifecycle codes are only ever revealed to the owning organization.
_LIFECYCLE_CLOSE_CODES = {
    SessionResolutionStatus.PENDING: (4409, "session not ready"),
    SessionResolutionStatus.CLOSED: (4410, "session closed"),
    SessionResolutionStatus.EXPIRED: (4408, "session expired"),
}

# Metric names (namespace skyvern.cdp_proxy.*). Emitted through the injected
# MetricsPort only — this module never imports a telemetry SDK. Tags carry only
# safe dims (org_id, session_id, cdp method/domain, direction, reason); the
# upstream URL, connect headers, and any credential never become a label.
_METRIC_CLIENT_CONNECTED = "skyvern.cdp_proxy.client_connected"
_METRIC_CLIENT_DISCONNECTED = "skyvern.cdp_proxy.client_disconnected"
_METRIC_ACTIVE_SESSIONS = "skyvern.cdp_proxy.active_sessions"
_METRIC_CONNECTION_REJECTED = "skyvern.cdp_proxy.connection_rejected"
_METRIC_FRAMES_RELAYED = "skyvern.cdp_proxy.frames_relayed"
_METRIC_BYTES_RELAYED = "skyvern.cdp_proxy.bytes_relayed"
_METRIC_COMMAND_LATENCY = "skyvern.cdp_proxy.command_latency_seconds"
_METRIC_FRAMES_DROPPED = "skyvern.cdp_proxy.frames_dropped"
_METRIC_FRAME_DECODE_ERRORS = "skyvern.cdp_proxy.frame_decode_errors"
_METRIC_CLIENT_BACKPRESSURE_CLOSED = "skyvern.cdp_proxy.client_backpressure_closed"
_METRIC_COMMANDS_INTERCEPTED = "skyvern.cdp_proxy.commands_intercepted"


def _org_tag(session: ProxySession) -> dict[str, str]:
    org_id = session.principal.organization_id if session.principal else None
    return {"org_id": org_id} if org_id else {}


# Latency labels are derived from client-supplied method names, so BOTH the method
# and the domain must be bucketed against allowlists — otherwise an authenticated
# client could mint unbounded histogram series or smuggle a token/URL into a label
# (e.g. "Page.<a-token>"). A method is exported verbatim only if it is in the
# method allowlist; otherwise it collapses to "other" (keeping its domain when that
# domain is known, else "other"). The allowlists cover the common CDP surface that
# Playwright/puppeteer/CDP clients drive; anything else buckets to "other" — safe
# degradation, never a client-controlled string. Kept for benchmark (SKY-12503) /
# policy (SKY-12532).
#
# Commands, plus the EVENT methods the policy pack names (SKY-12501): a drop counter
# tagged cdp_method="other" cannot say which stream a reduction came from. Only the
# pack's own methods are listed — the set stays static and small, so an event method
# never reaches a label unless it is written here.

_KNOWN_CDP_METHODS = frozenset(
    {
        *POLICY_PACK_EVENT_METHODS,
        *SCREENCAST_PACK_EVENT_METHODS,
        "Accessibility.disable",
        "Accessibility.enable",
        "Accessibility.getFullAXTree",
        "Accessibility.getPartialAXTree",
        "Accessibility.queryAXTree",
        "Browser.close",
        "Browser.getVersion",
        "Browser.getWindowBounds",
        "Browser.getWindowForTarget",
        "Browser.grantPermissions",
        "Browser.resetPermissions",
        "Browser.setDownloadBehavior",
        "Browser.setWindowBounds",
        "CSS.disable",
        "CSS.enable",
        "CSS.getComputedStyleForNode",
        "CSS.getMatchedStylesForNode",
        "Debugger.disable",
        "Debugger.enable",
        "Debugger.evaluateOnCallFrame",
        "Debugger.removeBreakpoint",
        "Debugger.resume",
        "Debugger.setBreakpointByUrl",
        "Debugger.setPauseOnExceptions",
        "Debugger.stepOver",
        "DOM.describeNode",
        "DOM.disable",
        "DOM.enable",
        "DOM.focus",
        "DOM.getAttributes",
        "DOM.getBoxModel",
        "DOM.getContentQuads",
        "DOM.getDocument",
        "DOM.getOuterHTML",
        "DOM.querySelector",
        "DOM.querySelectorAll",
        "DOM.removeAttribute",
        "DOM.requestNode",
        "DOM.resolveNode",
        "DOM.scrollIntoViewIfNeeded",
        "DOM.setAttributeValue",
        "DOM.setFileInputFiles",
        "DOMSnapshot.captureSnapshot",
        "DOMSnapshot.disable",
        "DOMSnapshot.enable",
        "Emulation.clearDeviceMetricsOverride",
        "Emulation.clearGeolocationOverride",
        "Emulation.setCPUThrottlingRate",
        "Emulation.setDefaultBackgroundColorOverride",
        "Emulation.setDeviceMetricsOverride",
        "Emulation.setEmulatedMedia",
        "Emulation.setGeolocationOverride",
        "Emulation.setLocaleOverride",
        "Emulation.setScriptExecutionDisabled",
        "Emulation.setTimezoneOverride",
        "Emulation.setTouchEmulationEnabled",
        "Emulation.setUserAgentOverride",
        "Fetch.continueRequest",
        "Fetch.continueResponse",
        "Fetch.continueWithAuth",
        "Fetch.disable",
        "Fetch.enable",
        "Fetch.failRequest",
        "Fetch.fulfillRequest",
        "Fetch.getResponseBody",
        "Fetch.takeResponseBodyAsStream",
        "IO.close",
        "IO.read",
        "IO.resolveBlob",
        "Input.dispatchDragEvent",
        "Input.dispatchKeyEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchTouchEvent",
        "Input.insertText",
        "Input.setInterceptDrags",
        "Input.synthesizeScrollGesture",
        "Input.synthesizeTapGesture",
        "Log.clear",
        "Log.disable",
        "Log.enable",
        "Log.startViolationsReport",
        "Network.clearBrowserCache",
        "Network.clearBrowserCookies",
        "Network.continueInterceptedRequest",
        "Network.deleteCookies",
        "Network.disable",
        "Network.emulateNetworkConditions",
        "Network.enable",
        "Network.getAllCookies",
        "Network.getCookies",
        "Network.getRequestPostData",
        "Network.getResponseBody",
        "Network.getResponseBodyForInterception",
        "Network.setBlockedURLs",
        "Network.setCacheDisabled",
        "Network.setCookie",
        "Network.setCookies",
        "Network.setExtraHTTPHeaders",
        "Network.setRequestInterception",
        "Network.setUserAgentOverride",
        "Page.addScriptToEvaluateOnNewDocument",
        "Page.bringToFront",
        "Page.captureScreenshot",
        "Page.close",
        "Page.createIsolatedWorld",
        "Page.crash",
        "Page.disable",
        "Page.enable",
        "Page.getFrameTree",
        "Page.getLayoutMetrics",
        "Page.getNavigationHistory",
        "Page.handleJavaScriptDialog",
        "Page.navigate",
        "Page.navigateToHistoryEntry",
        "Page.printToPDF",
        "Page.reload",
        "Page.removeScriptToEvaluateOnNewDocument",
        "Page.screencastFrameAck",
        "Page.setBypassCSP",
        "Page.setDocumentContent",
        "Page.setDownloadBehavior",
        "Page.setInterceptFileChooserDialog",
        "Page.setLifecycleEventsEnabled",
        "Page.startScreencast",
        "Page.stopLoading",
        "Page.stopScreencast",
        "Performance.disable",
        "Performance.enable",
        "Performance.getMetrics",
        "Profiler.disable",
        "Profiler.enable",
        "Profiler.start",
        "Profiler.stop",
        "Runtime.addBinding",
        "Runtime.awaitPromise",
        "Runtime.callFunctionOn",
        "Runtime.compileScript",
        "Runtime.disable",
        "Runtime.discardConsoleEntries",
        "Runtime.enable",
        "Runtime.evaluate",
        "Runtime.getProperties",
        "Runtime.globalLexicalScopeNames",
        "Runtime.releaseObject",
        "Runtime.releaseObjectGroup",
        "Runtime.removeBinding",
        "Runtime.runScript",
        "Security.disable",
        "Security.enable",
        "Security.setIgnoreCertificateErrors",
        "Storage.clearCookies",
        "Storage.clearDataForOrigin",
        "Storage.getCookies",
        "Storage.getUsageAndQuota",
        "Storage.setCookies",
        "Target.activateTarget",
        "Target.attachToTarget",
        "Target.closeTarget",
        "Target.createBrowserContext",
        "Target.createTarget",
        "Target.detachFromTarget",
        "Target.disposeBrowserContext",
        "Target.getBrowserContexts",
        "Target.getTargetInfo",
        "Target.getTargets",
        "Target.sendMessageToTarget",
        "Target.setAutoAttach",
        "Target.setDiscoverTargets",
    }
)


def _cdp_method_tags(method: str) -> tuple[str, str]:
    if method in _KNOWN_CDP_METHODS:
        return method, method.split(".", 1)[0]
    domain = method.split(".", 1)[0]
    return "other", domain if domain in KNOWN_CDP_DOMAINS else "other"


def _track_command(command_starts: dict[int, tuple[str, float]], upstream_id: int, method: str) -> None:
    if len(command_starts) >= _MAX_PENDING_COMMANDS and upstream_id not in command_starts:
        command_starts.pop(next(iter(command_starts)), None)
    command_starts[upstream_id] = (method, time.monotonic())


def _announcement_target_id(event: CdpFrame) -> str | None:
    target_info = (event.params or {}).get("targetInfo") if isinstance(event, (CdpCommand, CdpEvent)) else None
    target_id = target_info.get("targetId") if isinstance(target_info, dict) else None
    return target_id if isinstance(target_id, str) and target_id else None


def _pending_attach_owner(shared: _SharedUpstream, event: CdpFrame) -> str | None:
    """The client whose in-flight explicit attach this announcement answers: the oldest
    outstanding attach for that target. Consumes that intent."""
    target_id = _announcement_target_id(event)
    if target_id is None:
        return None
    for index, intent in enumerate(shared.attach_intents):
        if intent.target_id == target_id:
            shared.attach_intents.pop(index)
            return intent.client_key
    return None


def _has_pending_explicit_attach(shared: _SharedUpstream, event: CdpFrame) -> bool:
    """Whether an explicit attach for this announcement's target is still in flight,
    WITHOUT consuming its intent — detects the autoAttach/explicit ambiguity."""
    target_id = _announcement_target_id(event)
    if target_id is None:
        return False
    return any(intent.target_id == target_id for intent in shared.attach_intents)


def _owns_session(shared: _SharedUpstream, client_key: str, cdp_session_id: str | None) -> bool:
    """Whether this client may drive that cdp session. An unowned session is left to
    the browser to accept or reject, exactly as it would without the proxy."""
    if cdp_session_id is None:
        return True
    owner_key = shared.session_owner.get(cdp_session_id)
    return owner_key is None or owner_key == client_key


def _owns_addressed_sessions(shared: _SharedUpstream, client_key: str, command: CdpCommand) -> bool:
    """Whether this client may send this command at all. A session is addressed either
    by the frame's own sessionId or, for the target-plumbing commands, inside params —
    both routes reach a co-tenant's session and both are checked."""
    if not _owns_session(shared, client_key, command.session_id):
        return False
    if command.method in _SESSION_PARAM_METHODS:
        return _owns_session(shared, client_key, params_session_id(command))
    return True


def _record_attach_intent(shared: _SharedUpstream, intent: _AttachIntent) -> None:
    # An intent whose response never arrives would otherwise sit here until the client
    # leaves. At the cap, reclaim a slot at the CALLER's own expense — its oldest intent,
    # never a co-tenant's unreconciled announcement — mirroring the remapper's
    # owner-scoped _evict_one_for. A caller with nothing of its own to drop yields the
    # new intent (its announcement falls back to autoAttach/unowned routing) rather than
    # evicting another client's.
    while len(shared.attach_intents) >= _MAX_PENDING_COMMANDS:
        own = next((i for i, held in enumerate(shared.attach_intents) if held.client_key == intent.client_key), None)
        if own is None:
            return
        shared.attach_intents.pop(own)
    shared.attach_intents.append(intent)


def _track_attach_intent(shared: _SharedUpstream, client_key: str, command: CdpCommand, upstream_id: int) -> None:
    """Record who asked for the sessions Chrome is about to announce.

    Both routes matter, because the attach EVENT arrives before any reply that could
    identify its owner: an explicit attachToTarget is matched back by targetId, while
    browser-level autoAttach is credited to the first client that enabled it. A
    session-scoped setAutoAttach needs no record — its children inherit that session's
    owner.
    """
    if command.session_id is not None:
        return
    if command.method == _ATTACH_TO_TARGET_METHOD:
        target_id = (command.params or {}).get("targetId")
        if isinstance(target_id, str) and target_id:
            _record_attach_intent(shared, _AttachIntent(upstream_id, client_key, target_id, False, False))
        return
    if command.method != _SET_AUTO_ATTACH_METHOD:
        return
    if (command.params or {}).get("autoAttach"):
        enabling = client_key not in shared.autoattach_clients
        if enabling:
            shared.autoattach_clients.append(client_key)
        _record_attach_intent(shared, _AttachIntent(upstream_id, client_key, None, enabling, False))
    else:
        # Optimistically drop the client's autoAttach ownership, but record the disable so
        # a rejected command can restore it: without the intent the browser would stay in
        # autoAttach while the proxy believed the client opted out, dropping/misrouting its
        # later attach announcements.
        disabling = client_key in shared.autoattach_clients
        if disabling:
            shared.autoattach_clients.remove(client_key)
        _record_attach_intent(shared, _AttachIntent(upstream_id, client_key, None, False, disabling))


def _reconcile_attach_intent(shared: _SharedUpstream, upstream_id: int, *, failed: bool) -> None:
    """Settle a tentative intent against its response.

    A command that errored was never announced, so its intent must go — left behind, it
    would pair with another client's later attach to the same target and hand them each
    other's session. A successful intent is simply spent (its announcement either
    consumed it already, or autoAttach explained the announcement instead).
    """
    for index, intent in enumerate(shared.attach_intents):
        if intent.upstream_id != upstream_id:
            continue
        shared.attach_intents.pop(index)
        if failed:
            # A rejected command never took effect on the browser, so undo the ownership
            # change THIS command made and nothing else — a co-tenant's (or this client's
            # own later, successful) state must survive: drop an enable, restore a disable.
            if intent.enabled_autoattach and intent.client_key in shared.autoattach_clients:
                shared.autoattach_clients.remove(intent.client_key)
            elif intent.disabled_autoattach and intent.client_key not in shared.autoattach_clients:
                shared.autoattach_clients.append(intent.client_key)
        return


# Query param and path-marker names a client may use to carry the API key when
# it cannot set a WS header (e.g. puppeteer). Path scheme: /<marker>/<key>/<session_id>.
_API_KEY_QUERY_PARAMS = ("x-api-key", "api-key", "api_key", "apikey", "token", "access_token")
_PATH_CREDENTIAL_MARKERS = ("apikey", "api-key", "api_key", "key", "token")


def _parse_request(headers: Mapping[str, str], request_target: str) -> tuple[str, dict[str, str]]:
    """Split a client WS request into (session_id, credentials).

    Credentials may ride in a header, the query string, or a marked path segment;
    the raw request target can therefore carry a secret and must never be logged.
    The session id is always the final path segment.
    """
    split = urlsplit(request_target)
    credentials: dict[str, str] = {}
    lowered = {str(name).lower(): value for name, value in headers.items()}
    if lowered.get("x-api-key"):
        credentials["x-api-key"] = lowered["x-api-key"]
    if lowered.get("authorization"):
        credentials["authorization"] = lowered["authorization"]

    query = parse_qs(split.query)
    for name in _API_KEY_QUERY_PARAMS:
        values = query.get(name)
        if values and values[0]:
            credentials.setdefault("x-api-key", values[0])
            break

    segments = [segment for segment in split.path.split("/") if segment]
    credential_segment: int | None = None
    for index in range(len(segments) - 1):
        if segments[index].lower() in _PATH_CREDENTIAL_MARKERS:
            credentials.setdefault("x-api-key", segments[index + 1])
            credential_segment = index + 1
            break
    # The session id is the final path segment — unless that segment was itself
    # consumed as a credential (a path with no distinct trailing session id), in
    # which case the secret must never land in the session-id argument.
    if segments and credential_segment != len(segments) - 1:
        session_id = segments[-1]
    else:
        session_id = ""
    return session_id, credentials


def _split_discovery_target(request_target: str) -> tuple[str, str] | None:
    """Split a discovery GET into (base_target, suffix) or None if not discovery.

    base_target is the request target with the /json* suffix (and one optional
    trailing slash — Playwright appends `json/version/`) removed, query string
    preserved, so it re-parses through _parse_request exactly like a WS target.
    """
    split = urlsplit(request_target)
    path = split.path[:-1] if split.path.endswith("/") and split.path != "/" else split.path
    for suffix in _DISCOVERY_SUFFIXES:
        if path == suffix or path.endswith(suffix):
            base_path = path[: -len(suffix)] or "/"
            base_target = base_path if not split.query else f"{base_path}?{split.query}"
            return base_target, suffix
    return None


def _proxy_scoped_ws_url(host: str, forwarded_proto: str | None, base_target: str) -> str:
    """Point the client back at this proxy — never the upstream URL. Preserves the
    client's own credential-bearing path/query so a header-less client (puppeteer)
    keeps its URL-carried key on the subsequent WS connect."""
    # XFP is a comma-chain from proxies ("https, http"); the first hop is the client
    # scheme. Compare case-insensitively so "HTTPS"/" https" still yield wss.
    first_proto = (forwarded_proto or "").split(",", 1)[0].strip().lower()
    scheme = "wss" if first_proto == "https" else "ws"
    return f"{scheme}://{host}{base_target}"


def _discovery_payload(suffix: str, ws_url: str, session_id: str) -> dict[str, object] | list[dict[str, object]]:
    if suffix == "/json/version":
        return {"Browser": "Skyvern-CDP-Proxy", "Protocol-Version": "1.3", "webSocketDebuggerUrl": ws_url}
    return [
        {
            "id": session_id or "proxy",
            "type": "page",
            "title": "Skyvern CDP Proxy",
            "url": "about:blank",
            "webSocketDebuggerUrl": ws_url,
        }
    ]


def _json_response(body_text: str) -> Response:
    body = body_text.encode()
    # no-store: the webSocketDebuggerUrl can carry a header-less client's own
    # URL-borne API key, so a shared intermediary must never retain the body.
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Cache-Control", "no-store"),
            ("Pragma", "no-cache"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    return Response(HTTPStatus.OK.value, HTTPStatus.OK.phrase, headers, body)


class CdpProxyServer:
    def __init__(
        self,
        upstream: UpstreamBrowserPort,
        sessions: SessionRegistryPort,
        auth: AuthPort,
        metrics: MetricsPort,
        event_policy: EventPolicyPort,
        pipeline: MiddlewarePipeline | None = None,
        host: str = "0.0.0.0",
        port: int = 9223,
        client_queue_maxsize: int | None = None,
        client_backpressure_close_after: int | None = None,
        client_queue_maxbytes: int | None = None,
    ) -> None:
        self._upstream = upstream
        self._sessions = sessions
        self._auth = auth
        self._metrics = metrics
        self._event_policy = event_policy
        self._pipeline = pipeline or MiddlewarePipeline()
        self._host = host
        self._port = port
        env_maxsize, env_close_after, env_maxbytes = resolve_client_queue_limits()
        self._client_queue_maxsize = (
            _clamp_client_bound(client_queue_maxsize) if client_queue_maxsize is not None else env_maxsize
        )
        self._client_backpressure_close_after = (
            _clamp_client_bound(client_backpressure_close_after)
            if client_backpressure_close_after is not None
            else env_close_after
        )
        self._client_queue_maxbytes = (
            _clamp_client_bound(client_queue_maxbytes, _MAX_CLIENT_QUEUE_MAXBYTES)
            if client_queue_maxbytes is not None
            else env_maxbytes
        )
        self._max_pending_requests = _MAX_PENDING_COMMANDS
        # One shared upstream connection per resolved browser target, ref-counted
        # across clients. The lock guards only the dict + refcount (never held across
        # a dial), so first-client dials of different browsers never serialize.
        self._shared_upstreams: dict[str, _SharedUpstream] = {}
        self._shared_upstreams_lock = asyncio.Lock()

    async def serve_forever(self) -> None:
        async with websockets.serve(
            self._handle_client, self._host, self._port, max_size=None, process_request=self._process_request
        ):
            LOG.info("CDP proxy listening", host=self._host, port=self._port)
            await asyncio.Future()

    async def _process_request(self, connection: websockets.ServerConnection, request: Request) -> Response | None:
        """Serve the pre-upgrade CDP discovery GETs; return None to let a real WS
        upgrade proceed. Authenticated the same way as the WS connection."""
        try:
            split = _split_discovery_target(request.path)
            if split is None:
                return None
            base_target, suffix = split
            headers = dict(request.headers)
            session_id, credentials = _parse_request(headers, base_target)
        except (ValueError, MultipleValuesError):
            self._metrics.increment(
                _METRIC_CONNECTION_REJECTED, tags={"reason": "malformed_request", "phase": "discovery"}
            )
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized")
        if await self._auth.authenticate(credentials) is None:
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": "unauthorized", "phase": "discovery"})
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized")
        ws_url = _proxy_scoped_ws_url(
            headers.get("host", f"{self._host}:{self._port}"), headers.get("x-forwarded-proto"), base_target
        )
        return _json_response(json.dumps(_discovery_payload(suffix, ws_url, session_id)))

    async def _handle_client(self, ws: websockets.ServerConnection) -> None:
        try:
            headers = dict(ws.request.headers) if ws.request else {}
            request_target = ws.request.path if ws.request else "/"
            session_id, credentials = _parse_request(headers, request_target)
        except (ValueError, MultipleValuesError):
            # A malformed request target (urlsplit) or duplicate credential
            # headers (dict(Headers)) are rejected as an auth failure — a clean
            # close, never an uncaught error, and before any session lookup.
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": "malformed_request"})
            await ws.close(code=_UNAUTHORIZED_CLOSE_CODE, reason="unauthorized")
            return
        # The client credential is consumed here and never reaches the upstream
        # dial below (which uses the registry's operator routing/headers only).
        principal = await self._auth.authenticate(credentials)
        if principal is None:
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": "unauthorized"})
            await ws.close(code=_UNAUTHORIZED_CLOSE_CODE, reason="unauthorized")
            return
        # Resolved once per connection — routing never runs per message.
        resolution = await self._sessions.resolve(session_id)
        # authorize() rejects a non-owning org (and an unknown session) with the
        # exact same close as a truly unknown id (the 404-not-403 convention), so
        # a foreign session is indistinguishable from a nonexistent one.
        if not self._auth.authorize(principal, resolution):
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": "unknown_session"})
            await ws.close(code=_UNKNOWN_CLOSE_CODE, reason=_UNKNOWN_CLOSE_REASON)
            return
        resolved = resolution.session
        if resolved is None:
            code, reason = _LIFECYCLE_CLOSE_CODES.get(resolution.status, (_UNKNOWN_CLOSE_CODE, _UNKNOWN_CLOSE_REASON))
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": resolution.status.value})
            await ws.close(code=code, reason=reason)
            return
        # resolved.upstream_adapter selects among configured upstream ports once
        # multi-adapter wiring lands later in the epic; a single port is injected today.
        session = ProxySession(
            session_id=resolved.session_id,
            upstream_ws_url=resolved.upstream_ws_url,
            principal=principal,
            connect_headers=resolved.connect_headers,
        )
        session_tags = {**_org_tag(session), "session_id": session.session_id}
        # client_connected/client_disconnected are attempt/teardown counters: on an
        # upstream-connect failure below, client_connected has already fired but no
        # disconnected follows, so they can skew by 1. active_sessions (the gauge) is
        # the authoritative currently-connected signal and stays balanced regardless.
        self._metrics.increment(_METRIC_CLIENT_CONNECTED, tags=session_tags)
        try:
            # Ref-counted: the shared upstream opens on the first client for this
            # browser and is reused (not re-dialed) by every later client on it.
            shared = await self._acquire_shared(session)
        except UpstreamConnectError:
            # Upstream dial failed (vendor down, transient, config): reject the
            # client cleanly and record it. Reconnection is out of scope (SKY-12499).
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": "upstream_unavailable"})
            await ws.close(code=_UPSTREAM_UNAVAILABLE_CLOSE_CODE, reason="upstream unavailable")
            return
        # A ref is now held on `shared`; every exit path below (including cancellation
        # at the attach seam) must release it, or the entry + socket + reader leak.
        client_key = shared.next_client_key()
        relays: tuple[asyncio.Task[None], ...] = ()
        gauge_up = False
        try:
            # The upstream read loop enqueues here; a dedicated writer drains it to the
            # client socket, so a slow client blocks only its own writer, never the
            # shared read loop or the other clients on this browser.
            outbound = _OutboundQueue(self._client_queue_maxsize, self._client_queue_maxbytes)
            channel = _ClientChannel(client_key, outbound)
            await self._attach_client(shared, channel)
            self._metrics.gauge(_METRIC_ACTIVE_SESSIONS, 1, tags=session_tags)
            gauge_up = True
            writer = asyncio.create_task(self._drain_client_outbound(ws, session, outbound))
            # This client's uplink shares the connection's remapper (keyed by
            # client_key, so responses route back to the right client) and its
            # serialized send.
            relays = (
                asyncio.create_task(
                    self._relay_client_to_upstream(
                        ws, shared, session, shared.remapper, shared.command_starts, client_key, shared
                    )
                ),
                writer,
                asyncio.create_task(channel.wait_closed()),
            )
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
            if channel.closed.is_set():
                if channel.drain:
                    # Clean upstream close: flush queued frames (e.g. a final command
                    # response) before teardown; a client that cannot take the drain
                    # in time is closed 1013 rather than losing the response silently.
                    await self._drain_then_close(ws, outbound, writer)
                elif channel.close_code is not None:
                    with contextlib.suppress(websockets.ConnectionClosed):
                        await ws.close(code=channel.close_code, reason=channel.close_reason)
        except UpstreamConnectError:
            # _attach_client found the shared upstream defunct (its reader had already
            # exited): reject cleanly like a failed dial rather than joining a dead reader.
            self._metrics.increment(_METRIC_CONNECTION_REJECTED, tags={"reason": "upstream_unavailable"})
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.close(code=_UPSTREAM_UNAVAILABLE_CLOSE_CODE, reason="upstream unavailable")
        finally:
            # Cancellation-safe teardown: whatever exit path (a relay finishing,
            # _handle_client cancelled, or the attach rejection above), cancel and reap
            # EVERY child so none leak and every child exception is retrieved.
            for task in relays:
                task.cancel()
            if relays:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*relays, return_exceptions=True)
            shared.channels.pop(client_key, None)
            # Drop everything keyed to this client: pending id mappings, learned
            # cdp-session owners, and its autoAttach claim (so a co-tenant that also
            # asked inherits the sessions Chrome attaches from here on). No await runs
            # between these, so the shared reader cannot observe a half-cleaned client.
            # The command-start map is not exactly evicted here; it stays bounded by
            # _MAX_PENDING_COMMANDS.
            shared.remapper.clear_client(client_key)
            for cdp_session_id in [sid for sid, owner in shared.session_owner.items() if owner == client_key]:
                del shared.session_owner[cdp_session_id]
            shared.attach_intents[:] = [intent for intent in shared.attach_intents if intent.client_key != client_key]
            if client_key in shared.autoattach_clients:
                shared.autoattach_clients.remove(client_key)
            if gauge_up:
                # Decrement the gauge before the upstream close I/O — a real adapter's
                # close() can raise, and active_sessions must still net back to 0.
                self._metrics.gauge(_METRIC_ACTIVE_SESSIONS, -1, tags=session_tags)
                self._metrics.increment(_METRIC_CLIENT_DISCONNECTED, tags=session_tags)
            await self._release_shared(session, shared)

    async def _acquire_shared(self, session: ProxySession) -> _SharedUpstream:
        """Return the shared connection for this browser, opening it on the first
        client and ref-counting every later one. Concurrent first-clients for the
        same target await one dial via a single-flight connect task; a failed dial
        removes the entry and re-raises UpstreamConnectError to the caller."""
        # LOAD-BEARING INVARIANT: the key is the resolved per-browser upstream cdp_url,
        # so same key ⟹ same browser ⟹ same org (auth authorizes org-per-session before
        # this, and distinct vendor/own-infra browsers are distinct endpoints). A future
        # SHARED-GATEWAY adapter (one URL, per-browser routing in a header/selector) would
        # have to fold that routing identity into the key, or reuse would cross browsers.
        key = session.upstream_ws_url
        async with self._shared_upstreams_lock:
            shared = self._shared_upstreams.get(key)
            if shared is None or shared.defunct:
                # No live entry (or a dead one whose reader exited): dial fresh. A
                # defunct entry is overwritten; its own clients still tear it down.
                shared = _SharedUpstream(
                    session=session, remapper=RequestIdRemapper(max_pending=self._max_pending_requests)
                )
                shared.connect_task = asyncio.ensure_future(self._open_shared(session, shared))
                self._shared_upstreams[key] = shared
            shared.refcount += 1
        assert shared.connect_task is not None
        connect_task = shared.connect_task
        try:
            # Shield: one waiter's cancellation must not abort the dial the other
            # co-waiters are still legitimately waiting on. The last waiter out (below)
            # is the one that cancels the dial task.
            await asyncio.shield(connect_task)
        except BaseException:
            async with self._shared_upstreams_lock:
                shared.refcount -= 1
                removed = shared.refcount <= 0 and self._shared_upstreams.get(key) is shared
                if removed:
                    del self._shared_upstreams[key]
            if removed:
                # Last waiter out: cancel the (possibly still in-flight, shielded) dial
                # and close any connection it produced, so nothing is left orphaned.
                connect_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await connect_task
                await shared.aclose()
            raise
        return shared

    async def _open_shared(self, session: ProxySession, shared: _SharedUpstream) -> None:
        shared.connection = await self._upstream.connect(session)

    async def _attach_client(self, shared: _SharedUpstream, channel: _ClientChannel) -> None:
        """Register a client and start the shared reader on the first one. The reader
        must not start before a channel exists, or a burst of upstream frames could
        drain to no one and the client would never be signaled on teardown."""
        async with self._shared_upstreams_lock:
            if shared.defunct:
                # The reader exited (upstream died) after we acquired: never join a dead
                # reader — reject so the caller dials fresh instead of hanging.
                raise UpstreamConnectError("shared upstream is no longer connected")
            shared.channels[channel.client_key] = channel
            if shared.reader is None:
                shared.reader = asyncio.create_task(self._read_shared_upstream(shared))

    async def _evict_shared(self, shared: _SharedUpstream) -> None:
        """Mark a connection defunct and drop it from the pool (by identity) so no new
        client can join its dead reader. Called on every reader exit; idempotent."""
        shared.defunct = True
        key = shared.session.upstream_ws_url
        async with self._shared_upstreams_lock:
            if self._shared_upstreams.get(key) is shared:
                del self._shared_upstreams[key]
        # The session is gone for good; a policy holding per-session state would
        # otherwise keep one entry per session the proxy has ever served.
        # Forgetting only the first client's session_id relies on one upstream_ws_url
        # == one session_id (multi-client fan-in shares it), which is what the shared
        # connection is keyed on. A topology that put distinct session_ids on one url
        # would strand the others' policy state here — see the shared-gateway keying
        # carry-forward (SKY-12535); it would have to forget per owning session.
        self._event_policy.forget(shared.session.session_id)

    async def _release_shared(self, session: ProxySession, shared: _SharedUpstream) -> None:
        key = session.upstream_ws_url
        async with self._shared_upstreams_lock:
            shared.refcount -= 1
            if shared.refcount > 0:
                return
            if self._shared_upstreams.get(key) is shared:
                del self._shared_upstreams[key]
        # Last client left: reap the reader and close the socket. aclose is idempotent.
        await shared.aclose()

    async def _read_shared_upstream(self, shared: _SharedUpstream) -> None:
        """The single reader for one shared upstream: decode each frame, process it
        once, and route it to the owning client(s). On upstream death every client
        sharing this browser is closed deterministically (drain-then-close)."""
        assert shared.connection is not None
        session = shared.session
        direction = Direction.UPSTREAM_TO_CLIENT
        try:
            while True:
                try:
                    frame = decode_frame(await shared.connection.receive())
                except FrameDecodeError:
                    self._metrics.increment(
                        _METRIC_FRAME_DECODE_ERRORS, tags={**_org_tag(session), "direction": direction.value}
                    )
                    # A garbled upstream stream is fatal for every multiplexed client.
                    self._shutdown_clients(shared, drain=False, code=1003, reason="invalid CDP frame")
                    return
                result = await self._process_upstream_frame(
                    frame, session, shared.remapper, shared.command_starts, shared, upstream=shared
                )
                if result is None:
                    continue
                client_key, processed = result
                self._route_upstream_frame(shared, client_key, processed)
        except UpstreamClosedError:
            # Deterministic teardown: every client on this browser drains its queued
            # frames then closes. Reconnection/resumption would re-dial + re-attach
            # here instead — out of scope for v1 (SKY-12499), seam only.
            self._shutdown_clients(shared, drain=True, code=None, reason="")
        except Exception as exc:
            # An unexpected reader failure still tears every client down (rather than
            # leaving them hung on a dead socket). Log the type only — a frame's
            # content must never reach logs. Exhaustive error taxonomy is SKY-12500.
            LOG.warning("shared upstream reader failed", error_type=type(exc).__name__)
            self._shutdown_clients(shared, drain=False, code=_UPSTREAM_UNAVAILABLE_CLOSE_CODE, reason="upstream error")
        finally:
            # Reader is gone: evict so no new client joins its dead socket. Nothing
            # between the reader's exit and _evict_shared setting `defunct` yields, so an
            # acquirer racing this teardown always sees it. Existing clients were signaled
            # above; a new one is rejected at _attach_client's defunct check.
            await self._evict_shared(shared)

    def _route_upstream_frame(self, shared: _SharedUpstream, client_key: str | None, processed: CdpFrame) -> None:
        if client_key == PROXY_CLIENT_KEY:
            # The proxy's own command came back: consumed here. A client must never see
            # a response for an id it did not send.
            self._metrics.increment(
                _METRIC_FRAMES_DROPPED,
                tags={
                    **_org_tag(shared.session),
                    "direction": Direction.UPSTREAM_TO_CLIENT.value,
                    "reason": "proxy_command",
                },
            )
            return
        if client_key is not None:
            # A response for a specific client. Flat-session attach: an attach reply
            # carries the new cdp sessionId, so learn which client owns that session
            # (explicit Target.attachToTarget).
            if isinstance(processed, CdpResponse) and processed.result:
                cdp_session_id = processed.result.get("sessionId")
                if isinstance(cdp_session_id, str) and cdp_session_id:
                    shared.session_owner[cdp_session_id] = client_key
            channel = shared.channels.get(client_key)
            targets: tuple[_ClientChannel, ...] = (channel,) if channel is not None else ()
        else:
            targets = self._event_targets(shared, processed)
        if not targets:
            return
        wire = encode_frame(processed)
        for channel in targets:
            self._deliver_to_channel(shared, channel, processed, wire)

    def _event_targets(self, shared: _SharedUpstream, processed: CdpFrame) -> tuple[_ClientChannel, ...]:
        """Route an upstream-initiated frame to the client that owns its session.

        A session-scoped frame is delivered ONLY to its owner: on one shared
        connection every co-tenant would otherwise see another client's page traffic.
        """
        method = processed.method if isinstance(processed, CdpEvent) else None
        if method == TARGET_ATTACHED_EVENT:
            owner_key = self._learn_attached_session(shared, processed)
        elif method == TARGET_DETACHED_EVENT:
            # The session is gone: hand the notice to its owner and forget it. This is
            # what keeps session_owner flat under a long-lived client's attach/detach
            # churn. Target.targetDestroyed needs no separate GC — Chrome detaches a
            # target's sessions before destroying it — and a client's remaining entries
            # are dropped wholesale when it disconnects.
            detached = params_session_id(processed)
            owner_key = shared.session_owner.pop(detached, None) if detached else None
        elif processed.session_id:
            owner_key = shared.session_owner.get(processed.session_id)
        else:
            # Genuinely browser-level (no sessionId, e.g. Target.targetCreated): it
            # describes the browser rather than one client's session, so the fan-out to
            # every client on this connection is intentional.
            return tuple(shared.channels.values())
        if owner_key is None:
            # Nobody owns this session, so there is no recipient it could go to without
            # handing it to a non-owner. Dropping is the only safe routing.
            self._metrics.increment(
                _METRIC_FRAMES_DROPPED,
                tags={
                    **_org_tag(shared.session),
                    "direction": Direction.UPSTREAM_TO_CLIENT.value,
                    "reason": "unowned_session",
                },
            )
            return ()
        owner = shared.channels.get(owner_key)
        return (owner,) if owner is not None else ()

    def _learn_attached_session(self, shared: _SharedUpstream, event: CdpFrame) -> str | None:
        """Learn who owns a newly attached session: for a nested attach the parent
        session's owner, at browser level the client that enabled autoAttach, else the
        client whose explicit attach this answers. Returns the owner to deliver the
        announcement to, or None to withhold it (still recording durable ownership).

        An announcement carries nothing that distinguishes an autoAttach from an explicit
        attach, so while autoAttach is live the two are indistinguishable here and
        autoAttach wins the DURABLE assignment: the autoAttach half has no response that
        could ever correct it, whereas an explicit attach's response is authoritative and
        reassigns its own session (see `_route_upstream_frame`). Getting that backwards
        durably hands one client's auto-attached session to a co-tenant. But when a
        co-tenant has an explicit attach IN FLIGHT for this same target, the announcement
        itself could be theirs — so it is withheld (fail-closed) rather than delivered to
        the autoAttach client, closing the cross-tenant announcement window. Telling the
        two apart to deliver correctly needs the interception seam (SKY-12535).
        """
        deliver = True
        if event.session_id is not None:
            owner_key = shared.session_owner.get(event.session_id)
        elif shared.autoattach_clients:
            owner_key = shared.autoattach_clients[0]
            if _has_pending_explicit_attach(shared, event):
                deliver = False
        else:
            owner_key = _pending_attach_owner(shared, event)
        attached = params_session_id(event)
        if attached is not None and owner_key is not None:
            shared.session_owner[attached] = owner_key
        return owner_key if deliver else None

    def _deliver_to_channel(
        self, shared: _SharedUpstream, channel: _ClientChannel, processed: CdpFrame, wire: str
    ) -> None:
        if channel.closed.is_set():
            # Already signaled for teardown; don't queue or re-count it.
            return
        try:
            channel.outbound.put_nowait(wire)
            channel.overflow_streak = 0
            return
        except asyncio.QueueFull:
            pass
        session = shared.session
        direction = Direction.UPSTREAM_TO_CLIENT
        # An ordinary event is droppable (clients tolerate event loss); a command
        # response and a session lifecycle event are not — losing either desyncs the
        # client. A client too slow to take one, or one that stays overflowed, is closed
        # 1013 without stalling the shared reader or the other clients.
        if isinstance(processed, CdpEvent) and processed.method not in LIFECYCLE_EVENTS:
            # ponytail: a screencast frame tail-dropped HERE is never acked (unlike a
            # policy-withheld one, which is — SKY-12502), so a client that overflows
            # without hitting the close threshold can leave its own stream stalled on the
            # frames it never saw. Pre-existing and self-inflicted by a client too slow to
            # drain; acking from this sync path needs an owned task. Fix when a real slow
            # client is seen wedging a screencast, not before.
            channel.overflow_streak += 1
            self._metrics.increment(
                _METRIC_FRAMES_DROPPED,
                tags={**_org_tag(session), "direction": direction.value, "reason": "backpressure"},
            )
            if channel.overflow_streak < self._client_backpressure_close_after:
                return
        self._metrics.increment(
            _METRIC_CLIENT_BACKPRESSURE_CLOSED, tags={**_org_tag(session), "session_id": session.session_id}
        )
        channel.signal(drain=False, code=_BACKPRESSURE_CLOSE_CODE, reason="client too slow")

    def _shutdown_clients(self, shared: _SharedUpstream, *, drain: bool, code: int | None, reason: str) -> None:
        for channel in list(shared.channels.values()):
            channel.signal(drain=drain, code=code, reason=reason)

    async def _drain_then_close(
        self, ws: websockets.ServerConnection, outbound: _OutboundQueue, writer: asyncio.Task[None]
    ) -> None:
        if writer.done():
            return

        async def _flush() -> None:
            await outbound.put_sentinel()
            await writer  # FIFO: the writer sends every queued frame, then the sentinel stops it

        try:
            await asyncio.wait_for(_flush(), timeout=_OUTBOUND_DRAIN_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, websockets.ConnectionClosed, UpstreamClosedError):
            with contextlib.suppress(websockets.ConnectionClosed):
                await ws.close(code=_BACKPRESSURE_CLOSE_CODE, reason="client too slow")

    def _record_relayed(self, session: ProxySession, direction: Direction, wire: str) -> None:
        tags = {**_org_tag(session), "direction": direction.value}
        self._metrics.increment(_METRIC_FRAMES_RELAYED, tags=tags)
        self._metrics.increment(_METRIC_BYTES_RELAYED, amount=len(wire), tags=tags)

    def _record_command_latency(self, session: ProxySession, started: tuple[str, float] | None) -> None:
        if started is None:
            return
        method, start = started
        cdp_method, cdp_domain = _cdp_method_tags(method)
        self._metrics.observe(
            _METRIC_COMMAND_LATENCY,
            time.monotonic() - start,
            tags={**_org_tag(session), "cdp_method": cdp_method, "cdp_domain": cdp_domain},
        )

    async def _relay_client_to_upstream(
        self,
        ws: websockets.ServerConnection,
        connection: UpstreamConnection,
        session: ProxySession,
        remapper: RequestIdRemapper,
        command_starts: dict[int, tuple[str, float]],
        client_key: str = "client",
        shared: _SharedUpstream | None = None,
    ) -> None:
        # shared is None only on the pinned single-client relay contract, where there
        # is no co-tenant to steer another client's session or race its autoAttach.
        direction = Direction.CLIENT_TO_UPSTREAM
        context: InterceptContext | None = None
        if self._pipeline.has_interceptors:

            async def send_proxy_command(command: CdpCommand) -> None:
                # The proxy's reserved id lane: the response is consumed by the
                # upstream reader's PROXY_CLIENT_KEY branch, never sent to a client.
                upstream_command = remapper.to_upstream_as_proxy(command)
                try:
                    await connection.send(encode_frame(upstream_command))
                except BaseException:
                    # ANY post-allocation failure (encode, send, cancellation) must
                    # free the mapping: proxy-lane entries are never evictable by
                    # clients, so a leaked one is a slot lost to co-tenants forever.
                    remapper.discard(upstream_command.id)
                    raise

            context = InterceptContext(send_proxy_command=send_proxy_command)
        async for raw in ws:
            try:
                frame = decode_frame(raw)
            except FrameDecodeError:
                self._metrics.increment(
                    _METRIC_FRAME_DECODE_ERRORS, tags={**_org_tag(session), "direction": direction.value}
                )
                await ws.close(code=1003, reason="invalid CDP frame")
                return
            processed = await self._pipeline.process(frame, direction, session)
            if processed is None:
                self._metrics.increment(
                    _METRIC_FRAMES_DROPPED,
                    tags={**_org_tag(session), "direction": direction.value, "reason": "pipeline"},
                )
                continue
            if isinstance(processed, CdpCommand):
                if shared is not None and not _owns_addressed_sessions(shared, client_key, processed):
                    self._refuse_foreign_session(shared, session, client_key, processed)
                    continue
                if context is not None:
                    # After the ownership refusal (an interceptor never sees a
                    # foreign-session command) and before remapping (a synthesized
                    # response reuses the client's own id; only a forwarded command
                    # ever allocates an upstream one).
                    verdict = await self._intercept_command(processed, session, context)
                    if isinstance(verdict, CdpResponse):
                        await self._deliver_intercept_response(ws, shared, session, client_key, verdict)
                        continue
                    processed = verdict
                try:
                    upstream_command = remapper.to_upstream(client_key, processed)
                except RemapperFullError:
                    # This client has flooded the shared pending table and has nothing of
                    # its own left to reclaim. Close it rather than admit the command by
                    # dropping a co-tenant's promised response.
                    self._metrics.increment(
                        _METRIC_CLIENT_BACKPRESSURE_CLOSED,
                        tags={**_org_tag(session), "session_id": session.session_id},
                    )
                    await ws.close(code=_BACKPRESSURE_CLOSE_CODE, reason="too many in-flight commands")
                    return
                if shared is not None:
                    # No await runs between this and the serialized send below, so the
                    # queue is ordered exactly as the attaches reach the wire — which is
                    # what lets each announcement pair off with the attach that caused it.
                    _track_attach_intent(shared, client_key, processed, upstream_command.id)
                # Declared interest is read from what the client actually asked for, so
                # this sees the client's own command before the id is remapped upstream.
                self._event_policy.observe_command(processed, session)
                _track_command(command_starts, upstream_command.id, processed.method)
                processed = upstream_command
            elif isinstance(processed, (CdpEvent, CdpResponse)):
                # A CDP client only ever sends COMMANDS (method + id) to the browser;
                # events and responses flow the other way. A method-bearing frame
                # sent without an id decodes as an event (frames.py), so forwarding a
                # client event raw would let a client smuggle a command past command
                # interception — e.g. a denied Target.sendMessageToTarget stripped of
                # its id would tunnel a denied inner method past the denylist. Dropped,
                # never forwarded: closing the bypass here rather than trusting the
                # browser to reject an id-less command.
                self._metrics.increment(
                    _METRIC_FRAMES_DROPPED,
                    tags={**_org_tag(session), "direction": direction.value, "reason": "non_command_upstream"},
                )
                continue
            wire = encode_frame(processed)
            await connection.send(wire)
            self._record_relayed(session, direction, wire)

    def _refuse_foreign_session(
        self, shared: _SharedUpstream, session: ProxySession, client_key: str, command: CdpCommand
    ) -> None:
        """Answer a command for a session another client owns with CDP's own
        session-not-found error, so the client fails at once instead of hanging on a
        command that was never sent upstream."""
        self._metrics.increment(
            _METRIC_FRAMES_DROPPED,
            tags={
                **_org_tag(session),
                "direction": Direction.CLIENT_TO_UPSTREAM.value,
                "reason": "session_not_owned",
            },
        )
        channel = shared.channels.get(client_key)
        if channel is None:
            return
        refusal = CdpResponse(
            id=command.id,
            error={"code": _SESSION_NOT_FOUND_CODE, "message": _SESSION_NOT_FOUND_MESSAGE},
            session_id=command.session_id,
        )
        self._deliver_to_channel(shared, channel, refusal, encode_frame(refusal))

    async def _intercept_command(
        self, command: CdpCommand, session: ProxySession, context: InterceptContext
    ) -> CdpCommand | CdpResponse:
        """Run the interceptor chain over a client command, fail-closed: a raising
        interceptor (or a contract violation) answers the client with a deterministic
        internal error and forwards nothing — a policy hook that fails must never
        fail open into the browser."""
        try:
            outcome = await self._pipeline.intercept(command, session, context)
        except Exception as exc:
            # Type only — a frame's content must never reach logs.
            LOG.warning("command interceptor failed", error_type=type(exc).__name__, cdp_method=command.method)
            self._record_intercepted(session, command, INTERCEPTOR_FAILURE_REASON)
            return interceptor_failure_response(command)
        if isinstance(outcome, SynthesizedResponse):
            self._record_intercepted(session, command, outcome.reason, audit_method=outcome.audit_method)
            return outcome.to_response(command)
        return outcome

    def _record_intercepted(
        self, session: ProxySession, command: CdpCommand, reason: str, audit_method: str | None = None
    ) -> None:
        """The audit trail for a command answered at the proxy (SKY-12538): a counter
        under bounded labels plus one structured line naming the exact method. The
        synthesis's audit_method wins over the wrapper it arrived in, so a denial
        tunneled through Target.sendMessageToTarget is recorded against the method
        actually blocked."""
        method = audit_method or command.method
        cdp_method, cdp_domain = _cdp_method_tags(method)
        self._metrics.increment(
            _METRIC_COMMANDS_INTERCEPTED,
            tags={**_org_tag(session), "reason": reason, "cdp_method": cdp_method, "cdp_domain": cdp_domain},
        )
        LOG.info(
            "CDP command intercepted",
            session_id=session.session_id,
            cdp_method=method,
            reason=reason,
            **_org_tag(session),
        )

    async def _deliver_intercept_response(
        self,
        ws: websockets.ServerConnection,
        shared: _SharedUpstream | None,
        session: ProxySession,
        client_key: str,
        response: CdpResponse,
    ) -> None:
        wire = encode_frame(response)
        if shared is not None:
            channel = shared.channels.get(client_key)
            if channel is not None:
                # A response is non-droppable in _deliver_to_channel: a client too
                # slow to take it is closed rather than left hanging on the command.
                self._deliver_to_channel(shared, channel, response, wire)
            return
        await ws.send(wire)
        self._record_relayed(session, Direction.UPSTREAM_TO_CLIENT, wire)

    async def _pay_screencast_ack(
        self,
        upstream: UpstreamConnection | None,
        remapper: RequestIdRemapper,
        ack: CdpCommand | None,
    ) -> bool:
        """Send an ack the client will not, so the browser releases the next frame. False
        if it could not be sent at all — the caller must then deliver the ORIGINAL frame
        rather than withhold one nothing will ever ack.

        The ack rides the proxy's own lane, so its reply is consumed by the reader and a
        client never sees a response for a command it did not send. It is not latency
        tracked: the histogram is meant to describe what CLIENTS asked for.
        """
        if ack is None or upstream is None:
            return False
        try:
            upstream_ack = remapper.to_upstream_as_proxy(ack)
        except RemapperFullError:
            # The pending table is full of responses already promised to clients, and the
            # proxy may not evict one of those to make room for its own command.
            return False
        try:
            await upstream.send(encode_frame(upstream_ack))
        except UpstreamClosedError:
            # The socket died under us; the reader's next receive tears every client down.
            remapper.discard(upstream_ack.id)
            return False
        return True

    async def _process_upstream_frame(
        self,
        frame: CdpFrame,
        session: ProxySession,
        remapper: RequestIdRemapper,
        command_starts: dict[int, tuple[str, float]],
        shared: _SharedUpstream | None = None,
        upstream: UpstreamConnection | None = None,
    ) -> tuple[str | None, CdpFrame] | None:
        """Run one upstream frame through the pipeline, remap + latency-account a
        response, and apply the event policy. Returns the routing owner and the
        frame to deliver, or None if the frame was dropped (metric already emitted).
        The owner is the response's client key; None for events/commands (the caller
        routes those by sessionId or broadcast). Shared by the single-client relay
        and the shared multi-client reader so the two never drift.

        `upstream` is the sender a withheld screencast frame's ack goes back out on
        (SKY-12502); both callers pass their own, since the ack is owed on whichever
        connection the frame arrived on."""
        direction = Direction.UPSTREAM_TO_CLIENT
        # What the browser is waiting on for THIS frame, read from the frame as it
        # ARRIVED — before any middleware or policy can suppress or alter it. The ack is
        # owed by the original or the stream stalls, so it cannot be derived from whatever
        # comes out the far end: a rewrite would ack a frame the browser never sent, and a
        # suppression would ack nothing at all (SKY-12502).
        owed_ack = screencast_frame_ack(frame)
        awaits_ack = is_screencast_frame(frame)
        processed = await self._pipeline.process(frame, direction, session)
        if processed is None:
            if awaits_ack and not await self._pay_screencast_ack(upstream, remapper, owed_ack):
                # Nothing can ack it, so it must not be suppressed: deliver the original
                # and let the client's own ack keep the stream alive.
                return None, frame
            # A response that will never be delivered still has to free every table its
            # upstream id sits in, or the mapping outlives the exchange.
            if isinstance(frame, CdpResponse):
                command_starts.pop(frame.id, None)
                remapper.discard(frame.id)
                if shared is not None:
                    # The suppressed frame still carries its own verdict — settle the
                    # intent by it, or a dropped enable rejection would leave the client
                    # credited and the next browser-level attach handed to it.
                    _reconcile_attach_intent(shared, frame.id, failed=frame.error is not None)
            self._metrics.increment(
                _METRIC_FRAMES_DROPPED, tags={**_org_tag(session), "direction": direction.value, "reason": "pipeline"}
            )
            return None
        if isinstance(processed, CdpResponse):
            if shared is not None:
                # Before to_client rewrites the id back to the client's own.
                _reconcile_attach_intent(shared, processed.id, failed=processed.error is not None)
            mapped = remapper.to_client(processed)
            if mapped is None:
                command_starts.pop(processed.id, None)
                self._metrics.increment(
                    _METRIC_FRAMES_DROPPED,
                    tags={**_org_tag(session), "direction": direction.value, "reason": "unmatched_response"},
                )
                return None
            self._record_command_latency(session, command_starts.pop(processed.id, None))
            return mapped
        if isinstance(processed, CdpEvent):
            # On the shared path `session` is the first client's, while interest is
            # recorded under each client's own session (see observe_command below).
            # The two agree only because one upstream_ws_url == one session_id, so an
            # interest-relaxable rule (SKY-12501) reads the interest it meant to. A
            # topology putting distinct session_ids on one url must resolve the event
            # owner's session first — see the shared-gateway carry-forward (SKY-12535).
            decision = self._event_policy.decide(processed, session)
            if isinstance(decision, Drop):
                if awaits_ack and not await self._pay_screencast_ack(upstream, remapper, owed_ack):
                    # A screencast frame may only be withheld once the browser has been
                    # acked for it: the stream is ack-driven, so an unacked drop stops it
                    # for good rather than thinning it (SKY-12502). Nothing can ack this
                    # one, so deliver the ORIGINAL and let the client's own ack keep the
                    # stream alive — fail open, the way the engine treats unobserved
                    # interest.
                    return None, frame
                # The decision trace. The reason is a closed set, but the method is
                # not — the policy is a port, so an adapter may drop any method at all,
                # and an arbitrary one would export a series per value and put the
                # string itself in a label. Bucketed like command latency (SKY-12510).
                # _KNOWN_CDP_METHODS is a command allowlist, so an event buckets to
                # "other" and only its domain carries through today; SKY-12501 can name
                # the events its rules gate to get them back per-method.
                cdp_method, cdp_domain = _cdp_method_tags(processed.method)
                self._metrics.increment(
                    _METRIC_FRAMES_DROPPED,
                    tags={
                        **_org_tag(session),
                        "direction": direction.value,
                        "reason": decision.reason.value,
                        "cdp_method": cdp_method,
                        "cdp_domain": cdp_domain,
                    },
                )
                return None
            if isinstance(decision, Rewrite):
                processed = decision.event
        if awaits_ack and screencast_frame_ack(processed) != owed_ack:
            # Something is delivered, but not the frame the browser is waiting on: a
            # middleware or a policy Rewrite replaced it, so the client's ack (if it sends
            # one at all) names the replacement and the original stays unacked. Pay it
            # here. An equal ack means the replacement still names the same frame on the
            # same session — the client settles that one itself, and a second ack would
            # spend the browser's in-flight allowance twice.
            await self._pay_screencast_ack(upstream, remapper, owed_ack)
        return None, processed

    async def _relay_upstream_to_client(
        self,
        ws: websockets.ServerConnection,
        connection: UpstreamConnection,
        session: ProxySession,
        remapper: RequestIdRemapper,
        command_starts: dict[int, tuple[str, float]],
        outbound: _OutboundQueue | None = None,
    ) -> None:
        direction = Direction.UPSTREAM_TO_CLIENT
        # ponytail: single-subscriber relay retained as the pinned per-client
        # contract (metrics + backpressure). The served path multiplexes many
        # clients over one connection via _read_shared_upstream; both share
        # _process_upstream_frame. outbound=None is the direct-send test fallback.
        overflow_streak = 0
        while True:
            try:
                frame = decode_frame(await connection.receive())
            except FrameDecodeError:
                self._metrics.increment(
                    _METRIC_FRAME_DECODE_ERRORS, tags={**_org_tag(session), "direction": direction.value}
                )
                await ws.close(code=1003, reason="invalid CDP frame")
                return
            result = await self._process_upstream_frame(frame, session, remapper, command_starts, upstream=connection)
            if result is None:
                continue
            client_key, processed = result
            if client_key == PROXY_CLIENT_KEY:
                # The proxy's own command came back (a synthesized screencast ack):
                # consumed here, exactly as the shared reader does. A client must never
                # see a response for an id it did not send — this relay hands frames
                # straight to one client, so an unconsumed proxy reply would arrive as a
                # response to whatever it has in flight under that id.
                self._metrics.increment(
                    _METRIC_FRAMES_DROPPED,
                    tags={**_org_tag(session), "direction": direction.value, "reason": "proxy_command"},
                )
                continue
            wire = encode_frame(processed)
            if outbound is None:
                await ws.send(wire)
                self._record_relayed(session, direction, wire)
                continue
            try:
                outbound.put_nowait(wire)
                overflow_streak = 0
            except asyncio.QueueFull:
                # An ordinary event is droppable (clients tolerate event loss); a command
                # response and a session lifecycle event are not — losing either desyncs
                # the client, so a client too slow to take one, or one that stays
                # overflowed, is closed cleanly.
                # ponytail: tail-drop; head-drop for screencast freshness is EventPolicy (SKY-12532).
                if isinstance(processed, CdpEvent) and processed.method not in LIFECYCLE_EVENTS:
                    overflow_streak += 1
                    self._metrics.increment(
                        _METRIC_FRAMES_DROPPED,
                        tags={**_org_tag(session), "direction": direction.value, "reason": "backpressure"},
                    )
                    if overflow_streak < self._client_backpressure_close_after:
                        continue
                await self._close_slow_client(ws, session)
                return

    async def _drain_client_outbound(
        self, ws: websockets.ServerConnection, session: ProxySession, outbound: _OutboundQueue
    ) -> None:
        direction = Direction.UPSTREAM_TO_CLIENT
        while True:
            item = await outbound.get()
            if not isinstance(item, str):  # the drain sentinel: all real frames sent, stop
                return
            await ws.send(item)
            self._record_relayed(session, direction, item)

    async def _close_slow_client(self, ws: websockets.ServerConnection, session: ProxySession) -> None:
        self._metrics.increment(
            _METRIC_CLIENT_BACKPRESSURE_CLOSED, tags={**_org_tag(session), "session_id": session.session_id}
        )
        await ws.close(code=_BACKPRESSURE_CLOSE_CODE, reason="client too slow")
