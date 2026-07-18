"""Flag-gated demonstrator for the command-interception seam (SKY-12535).

One interceptor proving both halves of interception against a live upstream,
without any behavior a real client depends on:

- REQUEST synthesis: a proxy-originated Browser.getVersion is sent upstream on the
  proxy's reserved id lane, so the upstream still sees the traffic and its reply is
  consumed by the proxy — never delivered to any client, never colliding with a
  client's in-flight ids.
- RESPONSE synthesis: the client's own command is answered locally with the proxy's
  identity (matching the /json/version discovery payload), under the client's own
  request id and session scope.

Enabled only by `CDP_PROXY_INTERCEPTORS=demo-get-version`; never a default. If the
proxy-lane send fails (remapper full, upstream gone) the interceptor raises and the
command fails closed — the deterministic behavior the seam guarantees.
"""

from __future__ import annotations

from skyvern.proxy.core.frames import CdpCommand
from skyvern.proxy.core.pipeline import InterceptContext, InterceptOutcome, SynthesizedResponse
from skyvern.proxy.core.session import ProxySession

GET_VERSION_METHOD = "Browser.getVersion"
DEMO_INTERCEPT_REASON = "demo_get_version"

# Mirrors the discovery endpoint's identity, so an intercepted response is
# self-evidently the proxy's rather than a mangled browser reply.
DEMO_GET_VERSION_RESULT = {
    "protocolVersion": "1.3",
    "product": "Skyvern-CDP-Proxy",
    "revision": "@proxy",
    "userAgent": "Skyvern-CDP-Proxy",
    "jsVersion": "0",
}


async def demo_get_version_interceptor(
    command: CdpCommand, session: ProxySession, context: InterceptContext
) -> InterceptOutcome:
    if command.method != GET_VERSION_METHOD:
        return command
    # id=0 is a placeholder: to_upstream_as_proxy allocates the real upstream id.
    await context.send_proxy_command(CdpCommand(id=0, method=GET_VERSION_METHOD))
    return SynthesizedResponse(result=dict(DEMO_GET_VERSION_RESULT), reason=DEMO_INTERCEPT_REASON)


__all__ = [
    "DEMO_GET_VERSION_RESULT",
    "DEMO_INTERCEPT_REASON",
    "GET_VERSION_METHOD",
    "demo_get_version_interceptor",
]
