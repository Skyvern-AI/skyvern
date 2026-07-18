"""Org-level CDP operation denylists (SKY-12538) on the interception seam.

A denied command is answered with a deterministic synthesized CDP error under the
client's own request id — never a silent drop — and never reaches the browser.
The pattern grammar is a superset of the policy packs' exact method names: exact
("Page.navigate") or trailing-'*' prefix ("Network.*", "*").
"""

from __future__ import annotations

import asyncio
import json

import pytest

from skyvern.proxy.__main__ import build_interceptors, build_pipeline
from skyvern.proxy.adapters.memory import ForwardAllEventPolicy, InMemorySessionRegistry, StaticKeyAuth
from skyvern.proxy.adapters.websocket_server import CdpProxyServer
from skyvern.proxy.core.denylist import (
    DENIED_ERROR_CODE,
    DENYLIST_REASON,
    MethodPatternError,
    MethodPatternSet,
    denied_error,
    denied_method,
    org_denylist_interceptor,
)
from skyvern.proxy.core.frames import CdpCommand, CdpEvent, decode_frame
from skyvern.proxy.core.pipeline import InterceptContext, MiddlewarePipeline, SynthesizedResponse
from skyvern.proxy.core.session import Principal, ProxySession, ResolvedSession
from tests.unit.proxy.test_interception import _responses, _upstream_methods
from tests.unit.proxy.test_metrics_contract import _RecordingMetrics
from tests.unit.proxy.test_shared_connection import (
    UPSTREAM_URL,
    _command,
    _ControllableUpstream,
    _ScriptClient,
    _SharedBrowser,
    _until,
)


def test_exact_patterns_match_only_their_method() -> None:
    patterns = MethodPatternSet.compile(["Page.navigate"])

    assert patterns.matches("Page.navigate")
    assert not patterns.matches("Page.navigateToHistoryEntry")
    assert not patterns.matches("Page.enable")
    assert bool(patterns)


def test_trailing_star_is_a_prefix_wildcard() -> None:
    patterns = MethodPatternSet.compile(["Network.*", "Page.nav*"])

    assert patterns.matches("Network.enable")
    assert patterns.matches("Network.setCookie")
    assert patterns.matches("Page.navigate")
    assert patterns.matches("Page.navigateToHistoryEntry")
    assert not patterns.matches("Page.enable")
    assert not patterns.matches("Runtime.evaluate")


def test_bare_star_matches_every_method() -> None:
    patterns = MethodPatternSet.compile(["*"])

    assert patterns.matches("Page.enable")
    assert patterns.matches("anything")


def test_empty_pattern_set_matches_nothing() -> None:
    patterns = MethodPatternSet.compile([])

    assert not patterns.matches("Page.enable")
    assert not bool(patterns)


@pytest.mark.parametrize("bad", ["", "  ", "Page .navigate", "Page.*.x", "*.enable", "Pa*ge", "**", None, 7])
def test_invalid_patterns_are_rejected(bad: object) -> None:
    with pytest.raises(MethodPatternError):
        MethodPatternSet.compile([bad])  # type: ignore[list-item]


def test_denied_error_is_deterministic() -> None:
    # Exact on purpose: the AC promises a deterministic error, so the message is
    # contract, not prose.
    assert denied_error("Page.navigate") == {
        "code": DENIED_ERROR_CODE,
        "message": "'Page.navigate' is not allowed by organization policy",
    }


def _session(organization_id: str | None = "org-a") -> ProxySession:
    principal = Principal(principal_id="p", organization_id=organization_id) if organization_id else None
    return ProxySession(session_id="s1", upstream_ws_url="ws://x/y", principal=principal)


def _context() -> InterceptContext:
    async def send_proxy_command(command: CdpCommand) -> None:
        raise AssertionError("a denylist never issues upstream traffic")

    return InterceptContext(send_proxy_command=send_proxy_command)


@pytest.mark.asyncio
async def test_denied_method_synthesizes_the_deterministic_error() -> None:
    async def lookup(session: ProxySession) -> MethodPatternSet | None:
        return MethodPatternSet.compile(["Network.*"])

    interceptor = org_denylist_interceptor(lookup)

    outcome = await interceptor(CdpCommand(id=1, method="Network.enable"), _session(), _context())

    assert isinstance(outcome, SynthesizedResponse)
    assert outcome.error == denied_error("Network.enable")
    assert outcome.reason == DENYLIST_REASON


@pytest.mark.asyncio
async def test_unmatched_and_unconfigured_commands_pass_through() -> None:
    async def some(session: ProxySession) -> MethodPatternSet | None:
        return MethodPatternSet.compile(["Network.*"])

    async def none(session: ProxySession) -> MethodPatternSet | None:
        return None

    command = CdpCommand(id=1, method="Page.enable")

    assert await org_denylist_interceptor(some)(command, _session(), _context()) is command
    assert await org_denylist_interceptor(none)(command, _session(), _context()) is command


def _tunneled(method: str, params: dict | None = None, depth: int = 1) -> CdpCommand:
    inner: dict = {"id": 1, "method": method, "params": params or {}}
    for _ in range(depth):
        inner = {"id": 1, "method": "Target.sendMessageToTarget", "params": {"message": json.dumps(inner)}}
    return CdpCommand(id=1, method=inner["method"], params=inner["params"])


def test_the_legacy_send_message_tunnel_cannot_bypass_the_denylist() -> None:
    patterns = MethodPatternSet.compile(["Page.navigate"])

    # The inner method is what Chrome dispatches, so it is what gets matched.
    assert denied_method(patterns, _tunneled("Page.navigate")) == "Page.navigate"
    assert denied_method(patterns, _tunneled("Page.navigate", depth=3)) == "Page.navigate"
    assert denied_method(patterns, _tunneled("Page.enable")) is None
    # Denying the tunnel itself still works by pattern.
    assert denied_method(MethodPatternSet.compile(["Target.*"]), _tunneled("Page.enable")) == (
        "Target.sendMessageToTarget"
    )


def test_an_undispatchable_tunnel_message_passes() -> None:
    patterns = MethodPatternSet.compile(["Page.navigate"])
    for params in (
        None,
        {},
        {"message": 7},
        {"message": "not json"},
        {"message": json.dumps(["not", "an", "object"])},
        {"message": json.dumps({"id": 1})},
    ):
        command = CdpCommand(id=1, method="Target.sendMessageToTarget", params=params)
        # Chrome cannot dispatch any of these either, so there is nothing to deny.
        assert denied_method(patterns, command) is None


def test_a_tunnel_nested_past_the_depth_cap_is_denied() -> None:
    patterns = MethodPatternSet.compile(["Page.navigate"])

    assert denied_method(patterns, _tunneled("Page.enable", depth=20)) == "Target.sendMessageToTarget"


@pytest.mark.asyncio
async def test_the_interceptor_denies_the_tunneled_inner_method() -> None:
    async def lookup(session: ProxySession) -> MethodPatternSet | None:
        return MethodPatternSet.compile(["Page.navigate"])

    interceptor = org_denylist_interceptor(lookup)

    outcome = await interceptor(_tunneled("Page.navigate"), _session(), _context())

    assert isinstance(outcome, SynthesizedResponse)
    # The error names the method that was actually denied, not the wrapper.
    assert outcome.error == denied_error("Page.navigate")


@pytest.mark.asyncio
async def test_a_tunneled_denial_is_audited_against_the_inner_method() -> None:
    connection = _ControllableUpstream()
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    metrics = _RecordingMetrics()
    server = CdpProxyServer(
        upstream=_SharedBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=StaticKeyAuth({"key": Principal(principal_id="p", organization_id="org")}),
        metrics=metrics,  # type: ignore[arg-type]
        event_policy=ForwardAllEventPolicy(),
        pipeline=build_pipeline("forward-all", denylist="Page.navigate"),
    )
    tunneled = _tunneled("Page.navigate")
    client = _ScriptClient([json.dumps({"id": 1, "method": tunneled.method, "params": tunneled.params})])
    client.request.headers = {"x-api-key": "key"}

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.sent)

    [denied] = _responses(client)
    assert denied.error == denied_error("Page.navigate")
    # An org monitoring Page.navigate denials sees the tunneled attempt too: the
    # audit records the blocked method, not the wrapper it rode in.
    [tags] = [tags for op, name, _, tags in metrics.calls if name.endswith("commands_intercepted")]
    assert tags["cdp_method"] == "Page.navigate"
    assert tags["cdp_domain"] == "Page"
    assert tags["reason"] == DENYLIST_REASON

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_an_id_less_tunnel_cannot_bypass_the_denylist() -> None:
    # A denied Target.sendMessageToTarget stripped of its top-level id decodes as a
    # CdpEvent, not a CdpCommand — the interception path is command-only, so without
    # this guard the frame would be forwarded upstream raw, tunneling the denied
    # inner method past the denylist. It must be dropped, never forwarded.
    connection = _ControllableUpstream()
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    server = CdpProxyServer(
        upstream=_SharedBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=StaticKeyAuth({"key": Principal(principal_id="p", organization_id="org")}),
        metrics=_RecordingMetrics(),  # type: ignore[arg-type]
        event_policy=ForwardAllEventPolicy(),
        pipeline=build_pipeline("forward-all", denylist="Page.navigate"),
    )
    inner = json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "about:blank"}})
    # No top-level "id": this frame decodes as an event and skips command interception.
    id_less_tunnel = json.dumps({"method": "Target.sendMessageToTarget", "params": {"message": inner}})
    # A following command with an id proves the connection stayed usable.
    client = _ScriptClient([id_less_tunnel, _command(2, "Runtime.enable")])
    client.request.headers = {"x-api-key": "key"}

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.echoes())

    # The id-less tunnel never reached the browser (checked over ALL forwarded
    # frames, since the tunnel decodes as an event, not a command); only the
    # legitimate command did. Without the guard this frame is forwarded raw and
    # "Target.sendMessageToTarget" appears here.
    upstream = [decode_frame(wire) for wire in connection.sent]
    upstream_methods = [f.method for f in upstream if isinstance(f, (CdpCommand, CdpEvent))]
    assert "Target.sendMessageToTarget" not in upstream_methods
    assert upstream_methods == ["Runtime.enable"]

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_the_static_env_denylist_wiring_denies_through_a_live_server() -> None:
    pipeline = build_pipeline("forward-all", denylist="Network.*, Page.navigate")
    assert pipeline.has_interceptors

    connection = _ControllableUpstream()
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    server = CdpProxyServer(
        upstream=_SharedBrowser(connection),  # type: ignore[arg-type]
        sessions=sessions,
        auth=StaticKeyAuth({"key": Principal(principal_id="p", organization_id="org")}),
        metrics=_RecordingMetrics(),  # type: ignore[arg-type]
        event_policy=ForwardAllEventPolicy(),
        pipeline=pipeline,
    )
    client = _ScriptClient([_command(1, "Network.enable"), _command(2, "Runtime.enable")])
    client.request.headers = {"x-api-key": "key"}

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: len(client.sent) >= 2)

    [denied] = [r for r in _responses(client) if r.error is not None]
    assert denied.id == 1
    assert denied.error == denied_error("Network.enable")
    assert _upstream_methods(connection) == ["Runtime.enable"]

    client.release()
    await asyncio.wait_for(task, timeout=5)


def test_a_bad_env_denylist_pattern_fails_the_boot_not_a_command() -> None:
    with pytest.raises(MethodPatternError):
        build_interceptors("", "Network.*,bad*pattern")
    assert build_interceptors("", "") == ()


@pytest.mark.asyncio
async def test_denylists_apply_per_organization_across_all_its_sessions() -> None:
    """AC: an org with a denylist gets deterministic errors on banned methods on
    every session it owns, while another org's identical command passes."""
    denylists = {"org-a": MethodPatternSet.compile(["Runtime.*"])}

    async def lookup(session: ProxySession) -> MethodPatternSet | None:
        organization_id = session.principal.organization_id if session.principal else None
        return denylists.get(organization_id) if organization_id else None

    connection_a, connection_b = _ControllableUpstream(), _ControllableUpstream()

    class _TwoBrowsers:
        async def connect(self, session: ProxySession) -> _ControllableUpstream:
            return connection_a if "org-a" in session.upstream_ws_url else connection_b

    sessions = InMemorySessionRegistry()
    sessions.put(
        ResolvedSession(
            session_id="sa", upstream_adapter="memory", upstream_ws_url="ws://org-a/b", organization_id="org-a"
        )
    )
    sessions.put(
        ResolvedSession(
            session_id="sb", upstream_adapter="memory", upstream_ws_url="ws://org-b/b", organization_id="org-b"
        )
    )
    metrics = _RecordingMetrics()
    server = CdpProxyServer(
        upstream=_TwoBrowsers(),  # type: ignore[arg-type]
        sessions=sessions,
        auth=StaticKeyAuth(
            {
                "key-a": Principal(principal_id="a", organization_id="org-a"),
                "key-b": Principal(principal_id="b", organization_id="org-b"),
            }
        ),
        metrics=metrics,  # type: ignore[arg-type]
        event_policy=ForwardAllEventPolicy(),
        pipeline=MiddlewarePipeline(interceptors=[org_denylist_interceptor(lookup)]),
    )

    client_a = _ScriptClient([_command(1, "Runtime.evaluate"), _command(2, "Page.enable")])
    client_a.request.path = "/sa"
    client_a.request.headers = {"x-api-key": "key-a"}
    client_b = _ScriptClient([_command(1, "Runtime.evaluate")])
    client_b.request.path = "/sb"
    client_b.request.headers = {"x-api-key": "key-b"}

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: len(client_a.sent) >= 2 and client_b.sent)

    # org-a: the banned method is answered with the deterministic error, its other
    # command still reaches the browser; the banned one never does.
    [denied] = [r for r in _responses(client_a) if r.error is not None]
    assert denied.id == 1
    assert denied.error == denied_error("Runtime.evaluate")
    assert _upstream_methods(connection_a) == ["Page.enable"]
    # org-b: the same method sails through.
    assert client_b.echoes() == {"Runtime.evaluate"}
    assert _upstream_methods(connection_b) == ["Runtime.evaluate"]
    # AC: denied attempts are visible in the audit trail, attributed to the org.
    audits = [tags for op, name, _, tags in metrics.calls if name.endswith("commands_intercepted")]
    assert audits == [
        {"org_id": "org-a", "reason": DENYLIST_REASON, "cdp_method": "Runtime.evaluate", "cdp_domain": "Runtime"}
    ]

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)
