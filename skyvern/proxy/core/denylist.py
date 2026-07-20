"""Org-level CDP operation denylists (SKY-12538), as an interceptor on the
command-interception seam (SKY-12535).

A denied command is answered with a deterministic synthesized CDP error under the
client's own request id — never a silent drop, which would hang the client, and
never a forward, which would defeat the policy. Running as an interceptor keeps
the denylist inside the policy engine's decision path rather than a bypass: the
audit trail (the commands_intercepted counter plus the adapter's structured log)
comes with the seam.

Matching covers the legacy `Target.sendMessageToTarget` tunnel: the inner
serialized command's method is matched too (see `denied_method`), so wrapping a
denied method does not bypass the policy. A tunneled denial surfaces as an error
on the WRAPPER id — the same in-protocol outcome as Chrome refusing the send
itself (invalid or closed session), which legacy clients must and do handle by
failing the inner call (pre-flat puppeteer rejects the inner callback on a
wrapper error). Synthesizing the target-side `Target.receivedMessageFromTarget`
reply instead would need an event-synthesis verb the interception seam
deliberately does not have (SKY-12535 v1), for a client shape no worse off than
against a real browser refusing the send.

The METHOD-PATTERN GRAMMAR is a strict superset of the policy packs' exact method
names, so any method string a pack rule names is a valid pattern here:

- exact: ``Page.navigate`` matches only that method;
- trailing ``*``: a prefix wildcard — ``Network.*`` denies a domain, ``*`` denies
  everything. ``*`` anywhere else is rejected at compile time, not silently
  ignored: a pattern that quietly matched nothing would read as protection.

WHO IS DENIED comes from `DenylistLookup`: the session's organization resolves to
its compiled pattern set (the cloud ConfigStore-backed source lives in
cloud/cdp_proxy/denylist.py; its cache TTL is the propagation window). A lookup
returning None means no denylist — the source absorbs config-infrastructure
failures by failing OPEN, matching the event policy's forwarding-is-the-safe
default, while a lookup that raises fails the command CLOSED via the seam's rule.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from skyvern.proxy.core.frames import CdpCommand
from skyvern.proxy.core.pipeline import CommandInterceptor, InterceptContext, InterceptOutcome, SynthesizedResponse
from skyvern.proxy.core.session import ProxySession

DENYLIST_REASON = "org_denylist"

# JSON-RPC generic server error; distinct from CDP's -32001 session-not-found and
# the seam's -32603 interceptor failure, so a denial is tellable from both.
DENIED_ERROR_CODE = -32000


class MethodPatternError(ValueError):
    pass


def denied_error(method: str) -> dict[str, Any]:
    return {"code": DENIED_ERROR_CODE, "message": f"'{method}' is not allowed by organization policy"}


@dataclass(frozen=True)
class MethodPatternSet:
    """A compiled denylist: exact methods plus '*'-terminated prefixes."""

    exact: frozenset[str]
    prefixes: tuple[str, ...]

    @classmethod
    def compile(cls, patterns: Iterable[str]) -> MethodPatternSet:
        exact: set[str] = set()
        prefixes: list[str] = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern or any(c.isspace() for c in pattern):
                raise MethodPatternError(f"invalid method pattern: {pattern!r}")
            star = pattern.find("*")
            if star == -1:
                exact.add(pattern)
            elif star == len(pattern) - 1:
                prefixes.append(pattern[:-1])
            else:
                raise MethodPatternError(f"'*' is only valid as the final character: {pattern!r}")
        return cls(frozenset(exact), tuple(prefixes))

    def matches(self, method: str) -> bool:
        return method in self.exact or any(method.startswith(prefix) for prefix in self.prefixes)

    def __bool__(self) -> bool:
        return bool(self.exact or self.prefixes)


DenylistLookup = Callable[[ProxySession], Awaitable["MethodPatternSet | None"]]

_SEND_MESSAGE_METHOD = "Target.sendMessageToTarget"
# Legitimate clients do not nest the legacy tunnel at all; a chain deeper than
# this is denied outright rather than left unverifiable.
_MAX_TUNNEL_DEPTH = 8


def denied_method(patterns: MethodPatternSet, command: CdpCommand) -> str | None:
    """The method this command would run that the denylist matches, or None.

    Target.sendMessageToTarget is the legacy non-flat tunnel: its `message` param
    carries a serialized inner command that Chrome unwraps and dispatches, so the
    inner method must be matched too — recursively, since the tunnel can wrap
    itself — or denying a method is bypassed by wrapping it. An undecodable
    message passes (Chrome cannot dispatch it either, so there is nothing to
    deny); a chain past the depth cap is denied as-is.
    """
    method = command.method
    params: dict[str, Any] = command.params or {}
    for _ in range(_MAX_TUNNEL_DEPTH + 1):
        if patterns.matches(method):
            return method
        if method != _SEND_MESSAGE_METHOD:
            return None
        message = params.get("message")
        if not isinstance(message, str):
            return None
        try:
            inner = json.loads(message)
        except ValueError:
            return None
        if not isinstance(inner, dict) or not isinstance(inner.get("method"), str):
            return None
        method = inner["method"]
        inner_params = inner.get("params")
        params = inner_params if isinstance(inner_params, dict) else {}
    return method


def org_denylist_interceptor(lookup: DenylistLookup) -> CommandInterceptor:
    async def intercept(command: CdpCommand, session: ProxySession, context: InterceptContext) -> InterceptOutcome:
        patterns = await lookup(session)
        if patterns is not None:
            matched = denied_method(patterns, command)
            if matched is not None:
                # audit_method: a tunneled denial is audited against the method it
                # blocked, not the Target.sendMessageToTarget wrapper it rode in.
                return SynthesizedResponse(error=denied_error(matched), reason=DENYLIST_REASON, audit_method=matched)
        return command

    return intercept


__all__ = [
    "DENIED_ERROR_CODE",
    "DENYLIST_REASON",
    "DenylistLookup",
    "MethodPatternError",
    "MethodPatternSet",
    "denied_error",
    "denied_method",
    "org_denylist_interceptor",
]
