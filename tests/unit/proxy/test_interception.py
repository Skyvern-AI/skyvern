"""Command interception through the live relay (SKY-12535).

The seam's promises, proven against the shared-connection server: disabled means
byte-identical pass-through; a synthesized response answers the client under its
own request id with nothing forwarded; a proxy-synthesized request rides the
reserved id lane and its reply never reaches a client; rewrites forward through
remapping; a failing interceptor fails the command closed; and an interceptor
never sees a command the ownership check refuses.
"""

from __future__ import annotations

import asyncio

import pytest

from skyvern.proxy.__main__ import build_interceptors, build_pipeline
from skyvern.proxy.adapters.memory import AllowAllAuth, ForwardAllEventPolicy, InMemorySessionRegistry, NoOpMetrics
from skyvern.proxy.adapters.websocket_server import CdpProxyServer
from skyvern.proxy.core.frames import CdpCommand, CdpResponse, decode_frame
from skyvern.proxy.core.interception_demo import (
    DEMO_GET_VERSION_RESULT,
    DEMO_INTERCEPT_REASON,
    GET_VERSION_METHOD,
    demo_get_version_interceptor,
)
from skyvern.proxy.core.pipeline import (
    INTERCEPTOR_FAILURE_CODE,
    InterceptContext,
    MiddlewarePipeline,
    SynthesizedResponse,
)
from skyvern.proxy.core.session import ProxySession, ResolvedSession
from tests.unit.proxy.test_metrics_contract import _RecordingMetrics
from tests.unit.proxy.test_shared_connection import (
    UPSTREAM_URL,
    _command,
    _ControllableUpstream,
    _ScriptClient,
    _SharedBrowser,
    _until,
)


def _intercept_server(
    browser: object, pipeline: MiddlewarePipeline | None = None, metrics: object | None = None
) -> CdpProxyServer:
    sessions = InMemorySessionRegistry()
    sessions.put(ResolvedSession(session_id="s1", upstream_adapter="memory", upstream_ws_url=UPSTREAM_URL))
    return CdpProxyServer(
        upstream=browser,  # type: ignore[arg-type]
        sessions=sessions,
        auth=AllowAllAuth(),
        metrics=metrics or NoOpMetrics(),  # type: ignore[arg-type]
        event_policy=ForwardAllEventPolicy(),
        pipeline=pipeline,
    )


def _demo_pipeline() -> MiddlewarePipeline:
    return MiddlewarePipeline(interceptors=[demo_get_version_interceptor])


def _upstream_methods(connection: _ControllableUpstream) -> list[str]:
    return [f.method for f in map(decode_frame, connection.sent) if isinstance(f, CdpCommand)]


def _responses(client: _ScriptClient) -> list[CdpResponse]:
    return [f for f in map(decode_frame, client.sent) if isinstance(f, CdpResponse)]


def test_build_interceptors_is_a_closed_switch() -> None:
    assert build_interceptors("") == ()
    assert build_interceptors("demo-get-version") == (demo_get_version_interceptor,)
    assert build_pipeline("forward-all").has_interceptors is False
    assert build_pipeline("forward-all", "demo-get-version").has_interceptors is True
    assert build_pipeline("screencast-v1", "demo-get-version").has_interceptors is True
    with pytest.raises(ValueError):
        build_interceptors("bogus")


@pytest.mark.asyncio
async def test_demo_interceptor_passes_other_methods_untouched() -> None:
    sent: list[CdpCommand] = []

    async def send_proxy_command(command: CdpCommand) -> None:
        sent.append(command)

    command = CdpCommand(id=1, method="Page.enable")
    session = ProxySession(session_id="s1", upstream_ws_url=UPSTREAM_URL)

    outcome = await demo_get_version_interceptor(command, session, InterceptContext(send_proxy_command))

    assert outcome is command
    assert sent == []


@pytest.mark.asyncio
async def test_disabled_interception_forwards_get_version_upstream() -> None:
    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection))
    client = _ScriptClient([_command(1, GET_VERSION_METHOD)])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.echoes())

    # Pass-through unchanged: the browser answered, the proxy synthesized nothing.
    assert client.echoes() == {GET_VERSION_METHOD}
    assert _upstream_methods(connection) == [GET_VERSION_METHOD]

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_demo_synthesizes_the_response_under_the_clients_own_id() -> None:
    connection = _ControllableUpstream()
    metrics = _RecordingMetrics()
    server = _intercept_server(_SharedBrowser(connection), _demo_pipeline(), metrics)
    client = _ScriptClient([_command(5, GET_VERSION_METHOD)])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.sent)

    # RESPONSE synthesis: answered locally, under the client's own id.
    [response] = _responses(client)
    assert response.id == 5
    assert response.result == DEMO_GET_VERSION_RESULT
    # REQUEST synthesis: the upstream still saw one getVersion — the proxy's own,
    # whose echo reply was consumed on the proxy lane and never sent to the client.
    assert _upstream_methods(connection) == [GET_VERSION_METHOD]
    assert len(client.sent) == 1
    # Remapping is unbroken: once the proxy-lane reply lands, no mapping dangles.
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert await _until(lambda: shared.remapper.pending_count == 0)
    # The audit trail names the interception.
    reasons = [tags.get("reason") for op, name, _, tags in metrics.calls if name.endswith("commands_intercepted")]
    assert reasons == [DEMO_INTERCEPT_REASON]

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_interception_does_not_disturb_a_co_tenants_remapping() -> None:
    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection), _demo_pipeline())
    # Both clients use id 1: one is answered by the proxy, one by the browser.
    client_a = _ScriptClient([_command(1, GET_VERSION_METHOD)])
    client_b = _ScriptClient([_command(1, "Page.enable")])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    assert await _until(lambda: client_a.sent and client_b.echoes())

    [synthesized] = _responses(client_a)
    assert synthesized.id == 1
    assert synthesized.result == DEMO_GET_VERSION_RESULT
    assert client_b.echoes() == {"Page.enable"}
    assert all(decode_frame(wire).id == 1 for wire in client_b.sent)

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_a_rewriting_interceptor_forwards_through_remapping() -> None:
    async def bound_params(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        if command.method != "Page.navigate":
            return command
        return CdpCommand(id=command.id, method=command.method, params={**(command.params or {}), "bounded": True})

    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection), MiddlewarePipeline(interceptors=[bound_params]))
    client = _ScriptClient([_command(3, "Page.navigate", {"url": "about:blank"})])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.echoes())

    [forwarded] = [f for f in map(decode_frame, connection.sent) if isinstance(f, CdpCommand)]
    assert forwarded.params == {"url": "about:blank", "bounded": True}
    [response] = _responses(client)
    assert response.id == 3

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_failing_interceptor_fails_the_command_closed() -> None:
    async def broken(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        raise RuntimeError("interceptor bug")

    connection = _ControllableUpstream()
    metrics = _RecordingMetrics()
    server = _intercept_server(_SharedBrowser(connection), MiddlewarePipeline(interceptors=[broken]), metrics)
    client = _ScriptClient([_command(4, "Page.enable")])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.sent)

    # Deterministic error to the client, nothing forwarded upstream.
    [response] = _responses(client)
    assert response.id == 4
    assert response.error is not None
    assert response.error["code"] == INTERCEPTOR_FAILURE_CODE
    assert connection.sent == []
    reasons = [tags.get("reason") for op, name, _, tags in metrics.calls if name.endswith("commands_intercepted")]
    assert reasons == ["interceptor_error"]

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_an_invalid_synthesis_fails_closed_without_killing_the_relay() -> None:
    async def bad_synthesis(command: CdpCommand, session: ProxySession, context: InterceptContext) -> object:
        if command.method == "Page.enable":
            return SynthesizedResponse(result={"blob": b"not-json"})
        return command

    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection), MiddlewarePipeline(interceptors=[bad_synthesis]))
    client = _ScriptClient([_command(1, "Page.enable"), _command(2, "Runtime.enable")])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: len(client.sent) >= 2)

    # The bad synthesis became the deterministic error, and the relay survived to
    # serve the next command normally.
    [failure] = [r for r in _responses(client) if r.error is not None]
    assert failure.id == 1
    assert failure.error["code"] == INTERCEPTOR_FAILURE_CODE
    assert client.echoes() == {"Runtime.enable"}

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_a_failed_proxy_lane_send_leaves_no_mapping_behind() -> None:
    async def sends_unencodable(command: CdpCommand, session: ProxySession, context: InterceptContext) -> object:
        if command.method == "Page.enable":
            # Encoding fails after the mapping is allocated; the closure must
            # discard it or repeated failures eat the shared table for good.
            await context.send_proxy_command(CdpCommand(id=0, method="Proxy.probe", params={"blob": b"x"}))
        return command

    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection), MiddlewarePipeline(interceptors=[sends_unencodable]))
    client = _ScriptClient([_command(1, "Page.enable")])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: client.sent)

    [failure] = _responses(client)
    assert failure.id == 1
    assert failure.error is not None
    assert failure.error["code"] == INTERCEPTOR_FAILURE_CODE
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert shared.remapper.pending_count == 0
    assert connection.sent == []

    client.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_an_interceptor_never_sees_a_foreign_session_command() -> None:
    seen: list[tuple[str, str | None]] = []

    async def recorder(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        seen.append((command.method, command.session_id))
        return command

    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection), MiddlewarePipeline(interceptors=[recorder]))
    client_a = _ScriptClient([_command(1, "Target.attachToTarget", {"targetId": "tA", "flatten": True})])
    client_b = _ScriptClient([])

    task_a = asyncio.create_task(server._handle_client(client_a))  # type: ignore[arg-type]
    task_b = asyncio.create_task(server._handle_client(client_b))  # type: ignore[arg-type]
    # Wait until A owns session tA (its attach response landed), then B intrudes.
    assert await _until(lambda: client_a.sent)
    client_b.feed(_command(2, "Runtime.evaluate", {"expression": "1"}))
    # Give B's command a distinct marker frame so we know it was processed.
    client_b.feed(_command(3, "Runtime.evaluate"))
    shared = server._shared_upstreams[UPSTREAM_URL]
    assert await _until(lambda: len(seen) >= 2)
    seen.clear()

    client_b.feed(
        # Addressed to A's session: refused with CDP's own session-not-found,
        # without the interceptor ever seeing it.
        '{"id": 9, "method": "Runtime.evaluate", "sessionId": "tA"}'
    )
    assert await _until(lambda: any(r.id == 9 for r in _responses(client_b)))

    [refusal] = [r for r in _responses(client_b) if r.id == 9]
    assert refusal.error is not None
    assert refusal.error["code"] == -32001
    assert seen == []
    assert shared.session_owner.get("tA") == "c1"

    client_a.release()
    client_b.release()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5)


@pytest.mark.asyncio
async def test_a_synthesizing_interceptor_leaves_no_upstream_state_behind() -> None:
    async def answer(command: CdpCommand, session: ProxySession, context: InterceptContext) -> SynthesizedResponse:
        return SynthesizedResponse(result={"ok": True}, reason="test")

    connection = _ControllableUpstream()
    server = _intercept_server(_SharedBrowser(connection), MiddlewarePipeline(interceptors=[answer]))
    client = _ScriptClient([_command(1, "Page.enable"), _command(2, "Page.enable")])

    task = asyncio.create_task(server._handle_client(client))  # type: ignore[arg-type]
    assert await _until(lambda: len(client.sent) == 2)

    shared = server._shared_upstreams[UPSTREAM_URL]
    # No forward, no remap entry, no latency start, no attach intent: the command
    # was answered before any of those tables could learn about it.
    assert connection.sent == []
    assert shared.remapper.pending_count == 0
    assert shared.command_starts == {}
    assert shared.attach_intents == []

    client.release()
    await asyncio.wait_for(task, timeout=5)
