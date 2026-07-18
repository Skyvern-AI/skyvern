"""The command-side seam of the proxy core: an ordered middleware chain over frames,
plus command interception (SKY-12535).

Two hooks with different contracts, run in this order on the client→upstream leg:

1. FRAME MIDDLEWARES (`Middleware`) — pure rewrites over any frame in either
   direction, in registration order; returning None drops the frame. This is the
   right hook for bounding or normalizing what rides through (see
   `bound_screencast_middleware`).
2. COMMAND INTERCEPTORS (`CommandInterceptor`) — client→upstream COMMANDS only,
   after the middlewares and after the driving adapter's session-ownership refusal
   (an interceptor never sees a command addressed to a session its client does not
   own). This is the hook for implementing a CDP operation at the proxy: rewrite
   it, delegate it, or answer it without the browser.

Writing an interceptor — the composition and correlation rules:

- Interceptors run in registration order; each receives the command as the
  previous one left it.
- Return the command (same or rewritten) to continue the chain; the final command
  is forwarded upstream through request-id remapping exactly as an untouched one.
- Return a `SynthesizedResponse` to answer the client at the proxy: the chain
  short-circuits, nothing is forwarded, and the response is delivered carrying the
  command's own id and sessionId — stamped by `SynthesizedResponse.to_response`,
  never hand-built, so a synthesis cannot mis-correlate. Because the command never
  reaches the remapper, no upstream id, latency record, or attach intent exists to
  leak or leave dangling.
- There is no drop verb. A silently dropped command hangs its client forever, so
  blocking a command means synthesizing a deterministic error response (SKY-12538).
- A rewrite must preserve the request id and the session addressing (the frame's
  sessionId and a params-addressed sessionId): ownership was checked on what
  arrived, and the response must reach the id the client is waiting on. Both are
  enforced — a violation raises `InterceptorContractError`.
- An interceptor that raises is FAIL-CLOSED: the driving adapter answers the client
  with `interceptor_failure_response` (a deterministic internal error) and forwards
  nothing. A policy hook that fails must never fail open into the browser.
- `InterceptContext.send_proxy_command` synthesizes a REQUEST: a proxy-originated
  command sent upstream on the proxy's reserved id lane
  (`RequestIdRemapper.to_upstream_as_proxy`), whose response is consumed by the
  proxy and can never collide with, or be delivered as, a client's. Fire-and-forget
  in v1; awaiting the response (full delegation) is a planned extension of this
  context and needs no interceptor signature change.
- Upstream→client frames never reach interceptors; responses can be rewritten by a
  frame middleware, and events belong to `EventPolicyPort`.

A general plugin system is explicitly out of scope: interceptors are proxy-operator
code selected by deployment configuration (see `skyvern.proxy.__main__`).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Union

from skyvern.proxy.core.frames import (
    CdpCommand,
    CdpFrame,
    CdpResponse,
    FrameDecodeError,
    encode_frame,
    params_session_id,
)
from skyvern.proxy.core.session import ProxySession


class Direction(enum.Enum):
    CLIENT_TO_UPSTREAM = "client_to_upstream"
    UPSTREAM_TO_CLIENT = "upstream_to_client"


Middleware = Callable[[CdpFrame, Direction, ProxySession], Awaitable[CdpFrame | None]]


class InterceptorContractError(RuntimeError):
    """An interceptor broke a correlation invariant (changed the request id or the
    session addressing, or returned something that is neither a command nor a
    synthesis). Surfaced to the driving adapter, which fails the command closed."""


# JSON-RPC internal error: the deterministic verdict for a command whose
# interceptor failed. Distinct from CDP's own -32001 session-not-found.
INTERCEPTOR_FAILURE_CODE = -32603
_INTERCEPTOR_FAILURE_MESSAGE = "CDP command interception failed"

# The audit reason for the fail-closed path; SynthesizedResponse reasons share the
# same closed-set discipline (they become metric labels, never free-form).
INTERCEPTOR_FAILURE_REASON = "interceptor_error"


# Reasons become metric labels and log fields, so they are constrained to short
# label-safe identifiers: a reason derived from request data (a URL, an id, error
# text) cannot pass this, which is what bounds cardinality and rules out content
# leakage without a central registry.
_REASON_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True, slots=True)
class SynthesizedResponse:
    """An interceptor's local answer to a command: exactly one of result/error,
    validated eagerly — through the real frame encoder — so a malformed or
    non-encodable synthesis fails at construction (inside the interceptor, where
    the fail-closed rule applies) rather than at delivery, where it would tear the
    relay down instead of answering the client.

    `reason` is the audit tag for the decision trace — a label-safe identifier,
    since it becomes a metric label.
    """

    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    reason: str = "intercepted"

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("synthesized response must carry exactly one of result or error")
        try:
            # The id is a placeholder; to_response stamps the real one. What this
            # probes is everything else: error shape, and that every value in
            # result/error (including error.data) survives CDP JSON encoding.
            encode_frame(CdpResponse(id=0, result=self.result, error=self.error))
        except FrameDecodeError as exc:
            raise ValueError(f"synthesized response is not CDP-encodable: {exc}") from exc
        if not isinstance(self.reason, str) or not _REASON_PATTERN.fullmatch(self.reason):
            raise ValueError("reason must be a short lowercase identifier ([a-z0-9_]{1,64})")

    def to_response(self, command: CdpCommand) -> CdpResponse:
        """Stamp the synthesis with the intercepted command's own id and session
        scope — the correlation guarantee, by construction rather than convention."""
        return CdpResponse(id=command.id, result=self.result, error=self.error, session_id=command.session_id)


@dataclass(frozen=True)
class InterceptContext:
    """Per-connection capabilities the driving adapter hands an interceptor.

    `send_proxy_command` sends a proxy-originated command upstream on the reserved
    proxy id lane; its response is consumed by the proxy and never reaches a
    client. It raises (`RemapperFullError`, `UpstreamClosedError`) when the send
    cannot happen — an interceptor that lets that propagate fails closed.
    """

    send_proxy_command: Callable[[CdpCommand], Awaitable[None]]


InterceptOutcome = Union[CdpCommand, SynthesizedResponse]
CommandInterceptor = Callable[[CdpCommand, ProxySession, InterceptContext], Awaitable[InterceptOutcome]]


def interceptor_failure_response(command: CdpCommand) -> CdpResponse:
    """The deterministic fail-closed answer for a command whose interceptor raised."""
    return CdpResponse(
        id=command.id,
        error={"code": INTERCEPTOR_FAILURE_CODE, "message": _INTERCEPTOR_FAILURE_MESSAGE},
        session_id=command.session_id,
    )


class MiddlewarePipeline:
    """Runs each frame through an ordered middleware chain; returning None drops the
    frame. Command interceptors are a separate, stricter chain — see the module
    docstring for the ordering and composition rules."""

    def __init__(
        self,
        middlewares: Iterable[Middleware] | None = None,
        interceptors: Iterable[CommandInterceptor] | None = None,
    ) -> None:
        self._middlewares = list(middlewares or ())
        self._interceptors = list(interceptors or ())

    def add(self, middleware: Middleware) -> None:
        self._middlewares.append(middleware)

    @property
    def has_interceptors(self) -> bool:
        """False means the adapter's pass-through path runs with zero interception
        overhead — disabled is byte-identical to the pipeline before the seam."""
        return bool(self._interceptors)

    async def process(self, frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame | None:
        current: CdpFrame | None = frame
        for middleware in self._middlewares:
            if current is None:
                return None
            current = await middleware(current, direction, session)
        return current

    async def intercept(
        self, command: CdpCommand, session: ProxySession, context: InterceptContext
    ) -> InterceptOutcome:
        """Run a client→upstream command through the interceptor chain.

        Returns the final command to forward, or the first SynthesizedResponse
        (short-circuit). Contract violations raise InterceptorContractError; an
        interceptor's own exception propagates — the caller fails the command
        closed either way (`interceptor_failure_response`).
        """
        current = command
        for interceptor in self._interceptors:
            outcome = await interceptor(current, session, context)
            if isinstance(outcome, SynthesizedResponse):
                return outcome
            if not isinstance(outcome, CdpCommand):
                raise InterceptorContractError("interceptor must return a command or a synthesized response")
            if (
                outcome.id != command.id
                or outcome.session_id != command.session_id
                or params_session_id(outcome) != params_session_id(command)
            ):
                raise InterceptorContractError("interceptor must preserve the request id and session addressing")
            current = outcome
        return current
