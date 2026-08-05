from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
from weakref import ReferenceType, ref

import structlog

from skyvern.forge.sdk.browser_action_policy import AuthorityState, RuntimeOriginAuthority
from skyvern.forge.sdk.browser_effect_approval import (
    ConsumedEffect,
    EffectApprovalRejected,
    canonicalize_effect_method,
    canonicalize_effect_target,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

LOG = structlog.get_logger()
_MAX_CONSUMED_APPROVALS = 10_000
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_PASSIVE_TYPES = frozenset({"font", "image", "manifest", "media", "script", "stylesheet", "texttrack"})
_BLOCK_CONNECT_CSP = "connect-src 'none'"
_BLOCK_NON_HTTP_EGRESS = """
(() => {
  class BlockedNetworkAPI { constructor() { throw new DOMException("Blocked by browser egress policy.", "SecurityError"); } }
  for (const name of ["WebSocket", "WebTransport", "Worker", "SharedWorker", "RTCPeerConnection", "webkitRTCPeerConnection"])
    Object.defineProperty(globalThis, name, {configurable: false, value: BlockedNetworkAPI, writable: false});
})();
"""


class BrowserNetworkDenialReason(StrEnum):
    APPROVAL_CAPACITY_EXHAUSTED = "causal_approval_capacity_exhausted"
    APPROVAL_REPLAYED = "causal_approval_replayed"
    CANONICAL_TARGET_UNSUPPORTED = "canonical_target_unsupported"
    CAUSAL_EPOCH_REQUIRED = "causal_epoch_required"
    FRESH_APPROVAL_REQUIRED = "fresh_approval_required"
    INVALID_REQUEST = "invalid_request"
    MONITOR_INVALIDATED = "monitor_invalidated"
    ORIGIN_NOT_AUTHORIZED = "origin_not_authorized"
    RUNTIME_AUTHORITY_REQUIRED = "runtime_authority_required"
    UNENROLLED = "unenrolled"


@runtime_checkable
class ConsumedBrowserActionApproval(Protocol):
    # Structural checks do not prove provenance; Contract 2's ConsumedEffect does.
    @property
    def consumption_id(self) -> str: ...


def _header(headers: dict[str, str], name: str) -> tuple[str | None, str | None]:
    return next(((key, value) for key, value in headers.items() if key.lower() == name.lower()), (None, None))


class BrowserNetworkEgressMonitor:
    def __init__(self) -> None:
        self._install_started = False
        self._installed = False
        self._authority = RuntimeOriginAuthority(AuthorityState.UNWIRED)
        self._invalidated = False
        self._invalidation_reason: BrowserNetworkDenialReason | None = None
        self._epoch = 0
        self._used_ids: set[str] = set()
        self._open_ids: set[str] = set()
        self._initial_slots: dict[str, tuple[str, str]] = {}
        # This is capability registration, not collaborator discovery: the injected CDP owner
        # declares live per-page coverage; no caller can retrieve a collaborator through this map.
        self._active_request_interceptors: dict[int, tuple[ReferenceType[object], object]] = {}
        self._scope_id: ContextVar[str | None] = ContextVar("browser_network_consumption_id", default=None)

    @classmethod
    def unenrolled(cls) -> BrowserNetworkEgressMonitor:
        monitor = cls()
        monitor._invalidate(BrowserNetworkDenialReason.UNENROLLED)
        return monitor

    @property
    def invalidation_reason(self) -> BrowserNetworkDenialReason | None:
        return self._invalidation_reason

    async def install(self, context: BrowserContext) -> None:
        if self._install_started:
            raise RuntimeError("Browser network egress monitor is already installed")
        self._install_started = True
        try:
            options = context._impl_obj._options
            if not isinstance(options, dict) or options.get("serviceWorkers") != "block" or context.service_workers:
                raise RuntimeError("Browser network egress monitor requires blocked service workers")
            if context.pages:
                raise RuntimeError("Browser network egress monitor requires a context without existing pages")
            context.on("page", self._on_page)
            context.on("close", self._on_context_close)
            await context.add_init_script(_BLOCK_NON_HTTP_EGRESS)
            await context.route("**/*", self.handle_route)
            route_web_socket = getattr(context, "route_web_socket", None)
            if callable(route_web_socket):
                await route_web_socket("**/*", self.handle_websocket)
            if context.pages or self._invalidated:
                raise RuntimeError("Browser network egress monitor lost the pre-page installation race")
            self._installed = True
        except Exception:
            self._invalidate(BrowserNetworkDenialReason.MONITOR_INVALIDATED)
            raise

    def bind_authority(self, authority: RuntimeOriginAuthority) -> None:
        if not isinstance(authority, RuntimeOriginAuthority):
            self.invalidate()
            raise TypeError("Browser network egress monitor requires RuntimeOriginAuthority")
        if self._invalidated:
            return
        established = self._authority.state is AuthorityState.ESTABLISHED
        if authority.state is AuthorityState.INVALIDATED or (
            established
            and (authority.state is not AuthorityState.ESTABLISHED or authority.origins != self._authority.origins)
        ):
            self.invalidate()
        else:
            self._authority = authority

    @contextmanager
    def open_causal_epoch(self, consumed_approval: ConsumedBrowserActionApproval) -> Iterator[None]:
        if not self._installed or self._invalidated or self._authority.state is not AuthorityState.ESTABLISHED:
            raise RuntimeError("Browser network egress monitor cannot open a causal epoch")
        consumption_id = self._consumption_id(consumed_approval, "Browser network causal epoch")
        if consumption_id in self._used_ids:
            self._invalidate(BrowserNetworkDenialReason.APPROVAL_REPLAYED)
            raise RuntimeError("Consumed approval already opened a browser network causal epoch")
        if len(self._used_ids) >= _MAX_CONSUMED_APPROVALS:
            self._invalidate(BrowserNetworkDenialReason.APPROVAL_CAPACITY_EXHAUSTED)
            raise RuntimeError("Browser network causal approval capacity is exhausted")
        self._used_ids.add(consumption_id)
        self._open_ids.add(consumption_id)
        token = self._scope_id.set(consumption_id)
        self._advance_epoch()
        try:
            yield
        finally:
            self._initial_slots.pop(consumption_id, None)
            self._open_ids.discard(consumption_id)
            self._scope_id.reset(token)
            self._advance_epoch()

    def arm_initial_effect(self, consumed_approval: ConsumedBrowserActionApproval, *, method: str, url: str) -> None:
        consumption_id = self._consumption_id(consumed_approval, "Initial browser effect")
        if (
            self._invalidated
            or consumption_id != self._scope_id.get()
            or consumption_id not in self._open_ids
            or consumption_id in self._initial_slots
        ):
            self.invalidate()
            raise RuntimeError("Initial browser effect must be armed once inside its consumed dispatch scope")
        try:
            normalized_method = canonicalize_effect_method(method)
            normalized_url = canonicalize_effect_target(url)
        except (EffectApprovalRejected, TypeError, ValueError):
            self.invalidate()
            raise ValueError("Initial browser effect requires a canonical method and target")
        self._initial_slots[consumption_id] = (normalized_method, normalized_url)

    def register_active_request_interceptor(self, *, page: object, owner: object) -> None:
        if not self._installed or self._invalidated or page is None or owner is None:
            self.invalidate()
            raise RuntimeError("Active-request interception cannot be registered")
        page_id = id(page)
        existing = self._active_request_interceptors.get(page_id)
        if existing is not None and (existing[0]() is not page or existing[1] is not owner):
            if existing[0]() is not None:
                self.invalidate()
                raise RuntimeError("A different active-request interceptor already owns this page identity")
            del self._active_request_interceptors[page_id]
        try:
            page_ref = ref(page, lambda dead_ref: self._drop_active_request_interceptor(page_id, dead_ref))
        except TypeError:
            self.invalidate()
            raise TypeError("Active-request interception requires a weak-referenceable page") from None
        self._active_request_interceptors[page_id] = (page_ref, owner)

    def unregister_active_request_interceptor(self, *, page: object, owner: object) -> None:
        page_id = id(page)
        existing = self._active_request_interceptors.get(page_id)
        if existing is None:
            return
        if existing[0]() is page and existing[1] is owner:
            del self._active_request_interceptors[page_id]
        elif existing[0]() is not None:
            self.invalidate()

    def authorize_request(self, *, method: str, url: str, resource_type: str, frame: object | None) -> bool:
        allowed, reason = self._authorization(method, url, resource_type, frame, allow_initial_effect=True)
        if not allowed:
            LOG.warning("Browser network request denied", reason=reason, decision="block")
        return allowed

    def invalidate(self) -> None:
        self._invalidate(BrowserNetworkDenialReason.MONITOR_INVALIDATED)

    def _consumption_id(self, consumed: ConsumedBrowserActionApproval, purpose: str) -> str:
        if not isinstance(consumed, ConsumedEffect):
            self.invalidate()
            raise TypeError(f"{purpose} requires an active consumed approval")
        try:
            consumption_id = consumed.consumption_id
        except Exception:
            consumption_id = None
        if not isinstance(consumption_id, str) or not consumption_id:
            self.invalidate()
            raise TypeError(f"{purpose} requires an active consumed approval")
        return consumption_id

    def _invalidate(self, reason: BrowserNetworkDenialReason) -> None:
        if self._invalidated:
            return
        self._invalidated = True
        self._invalidation_reason = reason
        self._open_ids.clear()
        self._initial_slots.clear()
        self._active_request_interceptors.clear()
        self._authority = RuntimeOriginAuthority(AuthorityState.INVALIDATED)
        self._advance_epoch()

    def _authorization(
        self, method: str, url: str, resource_type: str, frame: object | None, *, allow_initial_effect: bool
    ) -> tuple[bool, BrowserNetworkDenialReason | None]:
        if self._invalidated:
            return False, self._invalidation_reason or BrowserNetworkDenialReason.MONITOR_INVALIDATED
        if self._authority.state is not AuthorityState.ESTABLISHED:
            return False, BrowserNetworkDenialReason.RUNTIME_AUTHORITY_REQUIRED
        resource_type = resource_type.lower() if isinstance(resource_type, str) else ""
        try:
            normalized_method = canonicalize_effect_method(method)
        except (TypeError, ValueError):
            return False, BrowserNetworkDenialReason.INVALID_REQUEST
        if resource_type == "serviceworker":
            return False, BrowserNetworkDenialReason.INVALID_REQUEST
        try:
            normalized_url = canonicalize_effect_target(url)
        except EffectApprovalRejected:
            return False, BrowserNetworkDenialReason.CANONICAL_TARGET_UNSUPPORTED
        parsed_url = urlsplit(normalized_url)
        # The shared canonicalizer has already normalized and restricted the target. This only
        # slices its exact origin for the runtime-authority ceiling; it never aliases URL schemes.
        target_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
        if all(target_origin != origin.canonical for origin in self._authority.origins):
            return False, BrowserNetworkDenialReason.ORIGIN_NOT_AUTHORIZED
        passive = normalized_method in {"GET", "HEAD"} and resource_type in _PASSIVE_TYPES and frame is not None
        if passive:
            return (True, None) if self._open_ids else (False, BrowserNetworkDenialReason.CAUSAL_EPOCH_REQUIRED)
        if allow_initial_effect and resource_type in {"document", "download"}:
            exact = (normalized_method, normalized_url)
            slot_id = next(
                (key for key, slot in self._initial_slots.items() if key in self._open_ids and slot == exact), None
            )
            if slot_id is not None:
                del self._initial_slots[slot_id]
                return True, None
        return False, BrowserNetworkDenialReason.FRESH_APPROVAL_REQUIRED

    async def handle_route(self, route: Any, request: Any) -> None:
        try:
            resource_type = request.resource_type.lower() if isinstance(request.resource_type, str) else ""
            active_request = resource_type in {"document", "download"}
            cdp_owned = active_request and self._active_interceptor_owner(request.frame) is not None
            allowed, reason = (
                (True, None)
                if cdp_owned
                else self._authorization(
                    request.method, request.url, request.resource_type, request.frame, allow_initial_effect=True
                )
            )
            if not allowed:
                await self._abort(route, reason)
                return
            if resource_type == "document":
                self._advance_epoch()
            marker = self._epoch
            response = await route.fetch(max_redirects=0)
            post_allowed, post_reason = self._authorization(
                request.method, request.url, request.resource_type, request.frame, allow_initial_effect=False
            )
            still_allowed = marker == self._epoch and (active_request or post_allowed)
            if marker != self._epoch and post_reason is None:
                post_reason = BrowserNetworkDenialReason.CAUSAL_EPOCH_REQUIRED
            headers = dict(response.headers)
            _, location = _header(headers, "location")
            if response.status in _REDIRECTS and location:
                await self._abort(route, BrowserNetworkDenialReason.FRESH_APPROVAL_REQUIRED)
                return
            if not still_allowed:
                await self._abort(route, post_reason)
                return
            csp_key, existing_csp = _header(headers, "content-security-policy")
            headers[csp_key or "Content-Security-Policy"] = (
                f"{existing_csp}, {_BLOCK_CONNECT_CSP}" if existing_csp else _BLOCK_CONNECT_CSP
            )
            await route.fulfill(response=response, headers=headers)
        except Exception as exc:
            LOG.warning("Browser network request failed closed", error_class=type(exc).__name__, decision="block")
            await route.abort("blockedbyclient")

    async def _abort(self, route: Any, reason: BrowserNetworkDenialReason | None) -> None:
        LOG.warning("Browser network request denied", reason=reason, decision="block")
        await route.abort("blockedbyclient")

    async def handle_websocket(self, web_socket: Any) -> None:
        await web_socket.close(code=1008, reason="Browser egress policy blocked this connection.")

    def _active_interceptor_owner(self, frame: object | None) -> object | None:
        try:
            # One synchronous read makes registration and the pass-through decision atomic.
            page = frame.page  # type: ignore[union-attr]
            registered = self._active_request_interceptors.get(id(page))
            return registered[1] if registered is not None and registered[0]() is page else None
        except AttributeError:
            return None

    def _drop_active_request_interceptor(self, page_id: int, dead_ref: ReferenceType[object]) -> None:
        registered = self._active_request_interceptors.get(page_id)
        if registered is not None and registered[0] is dead_ref:
            del self._active_request_interceptors[page_id]

    def _advance_epoch(self, *_: object) -> None:
        self._epoch += 1

    def _on_page(self, page: Any) -> None:
        self._advance_epoch()
        if not self._installed:
            self.invalidate()
            return
        try:
            page.on("framenavigated", self._on_frame_navigated)
            page.on("close", self._on_page_close)
        except Exception:
            self.invalidate()

    def _on_frame_navigated(self, frame: Any) -> None:
        try:
            if frame.parent_frame is None:
                self._advance_epoch()
        except Exception:
            self.invalidate()

    def _on_page_close(self, page: object) -> None:
        registered = self._active_request_interceptors.get(id(page))
        if registered is not None and registered[0]() is page:
            del self._active_request_interceptors[id(page)]
        self._advance_epoch()

    def _on_context_close(self, *_: object) -> None:
        self.invalidate()
