from __future__ import annotations

import asyncio
import os
import time

import structlog

from skyvern.proxy.adapters.local_chrome import LocalChromeUpstreamBrowser
from skyvern.proxy.adapters.memory import (
    AllowAllAuth,
    ForwardAllEventPolicy,
    InMemorySessionRegistry,
    InMemoryUpstreamBrowser,
    NoOpMetrics,
)
from skyvern.proxy.adapters.websocket_server import CdpProxyServer
from skyvern.proxy.adapters.websocket_upstream import WebSocketUpstreamBrowser
from skyvern.proxy.core.denylist import MethodPatternSet, org_denylist_interceptor
from skyvern.proxy.core.interception_demo import demo_get_version_interceptor
from skyvern.proxy.core.pipeline import CommandInterceptor, MiddlewarePipeline
from skyvern.proxy.core.policy import EventPolicyEngine
from skyvern.proxy.core.policy_pack import NOISY_EVENT_PACK_V1
from skyvern.proxy.core.screencast import SCREENCAST_PACK_V1, screencast_pipeline
from skyvern.proxy.core.session import ProxySession
from skyvern.proxy.ports import EventPolicyPort, UpstreamBrowserPort

LOG = structlog.get_logger(__name__)


def build_upstream(kind: str) -> UpstreamBrowserPort:
    """Config-only adapter switch: swapping upstreams never touches the core."""
    if kind == "websocket":
        return WebSocketUpstreamBrowser()
    if kind == "local-chrome":
        return LocalChromeUpstreamBrowser()
    if kind == "memory":
        return InMemoryUpstreamBrowser()
    raise ValueError(f"unknown CDP_PROXY_UPSTREAM adapter: {kind!r}")


def build_event_policy(kind: str) -> EventPolicyPort:
    """Config-only policy switch. Filtering is opt-in: the default forwards every
    event unchanged, and turning a pack on for real traffic is a rollout decision
    (SKY-12537), not a consequence of shipping the rules.

    Throttle windows run on a monotonic clock — a wall-clock step would otherwise
    hand out a free budget or stall one.
    """
    if kind == "forward-all":
        return ForwardAllEventPolicy()
    if kind == "noisy-v1":
        return EventPolicyEngine(config=NOISY_EVENT_PACK_V1, clock=time.monotonic)
    if kind == "screencast-v1":
        return EventPolicyEngine(config=SCREENCAST_PACK_V1, clock=time.monotonic)
    raise ValueError(f"unknown CDP_PROXY_EVENT_POLICY: {kind!r}")


def build_interceptors(kind: str, denylist: str = "") -> tuple[CommandInterceptor, ...]:
    """Config-only interceptor switch (SKY-12535). The default is none: with
    interception disabled the command path is exactly the pre-seam pass-through.

    `denylist` (CDP_PROXY_DENYLIST, comma-separated method patterns) is the
    generic wiring for SKY-12538: one static list applied to every session this
    proxy serves, compiled at startup so a bad pattern fails the boot rather than
    a command. The per-organization source (cloud/cdp_proxy/denylist.py) replaces
    the lookup in the cloud composition root. The denylist runs FIRST: a denied
    method is denied before any other interceptor can see or rewrite it.
    """
    interceptors: list[CommandInterceptor] = []
    if denylist:
        patterns = MethodPatternSet.compile([part.strip() for part in denylist.split(",") if part.strip()])

        async def deny_all_sessions(session: ProxySession) -> MethodPatternSet | None:
            return patterns

        interceptors.append(org_denylist_interceptor(deny_all_sessions))
    if kind == "demo-get-version":
        interceptors.append(demo_get_version_interceptor)
    elif kind:
        raise ValueError(f"unknown CDP_PROXY_INTERCEPTORS: {kind!r}")
    return tuple(interceptors)


def build_pipeline(kind: str, interceptors_kind: str = "", denylist: str = "") -> MiddlewarePipeline:
    """The command half of the selected policy, keyed off the same switch.

    Only the screencast policy has one: bounding what a client asks Page.startScreencast
    for is a command rewrite, and EventPolicyPort decides events only, so the two halves
    of that one policy reach the proxy through different seams and must be turned on
    together. A pack combining screencast with noisy-v1 would have to select both halves
    here too — the packs' rules do not overlap, so nothing but this switch prevents it.

    Interceptors are orthogonal to the policy pack and compose into the same pipeline.
    """
    interceptors = build_interceptors(interceptors_kind, denylist)
    if kind == "screencast-v1":
        return screencast_pipeline(interceptors)
    return MiddlewarePipeline(interceptors=interceptors)


def main() -> None:
    # Dev wiring: in-memory registry, allow-all auth. Production adapters are
    # injected here in follow-up issues.
    upstream_kind = os.environ.get("CDP_PROXY_UPSTREAM", "websocket")
    policy_kind = os.environ.get("CDP_PROXY_EVENT_POLICY", "forward-all")
    interceptors_kind = os.environ.get("CDP_PROXY_INTERCEPTORS", "")
    denylist = os.environ.get("CDP_PROXY_DENYLIST", "")
    server = CdpProxyServer(
        upstream=build_upstream(upstream_kind),
        sessions=InMemorySessionRegistry(),
        auth=AllowAllAuth(),
        metrics=NoOpMetrics(),
        event_policy=build_event_policy(policy_kind),
        pipeline=build_pipeline(policy_kind, interceptors_kind, denylist),
        host=os.environ.get("CDP_PROXY_HOST", "0.0.0.0"),
        port=int(os.environ.get("CDP_PROXY_PORT", "9223")),
    )
    LOG.info(
        "starting CDP proxy",
        upstream=upstream_kind,
        event_policy=policy_kind,
        interceptors=interceptors_kind,
        denylist_patterns=len([part for part in denylist.split(",") if part.strip()]),
    )
    asyncio.run(server.serve_forever())


if __name__ == "__main__":
    main()
