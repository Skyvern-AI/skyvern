from __future__ import annotations

import pytest

from skyvern.proxy.core.frames import CdpCommand, CdpFrame
from skyvern.proxy.core.pipeline import (
    Direction,
    InterceptContext,
    InterceptorContractError,
    MiddlewarePipeline,
    SynthesizedResponse,
    interceptor_failure_response,
)
from skyvern.proxy.core.session import Principal, ProxySession


def make_session() -> ProxySession:
    return ProxySession(
        session_id="session",
        upstream_ws_url="ws://localhost:1",
        principal=Principal(principal_id="owner"),
    )


def make_context(sent: list[CdpCommand] | None = None) -> InterceptContext:
    async def send_proxy_command(command: CdpCommand) -> None:
        if sent is not None:
            sent.append(command)

    return InterceptContext(send_proxy_command=send_proxy_command)


@pytest.mark.asyncio
async def test_pipeline_composes_middlewares_in_order() -> None:
    calls: list[tuple[str, Direction, str]] = []

    async def add_first_param(frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame:
        calls.append(("first", direction, session.session_id))
        assert isinstance(frame, CdpCommand)
        return CdpCommand(id=frame.id, method=frame.method, params={"first": True})

    async def add_second_param(frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame:
        calls.append(("second", direction, session.session_id))
        assert isinstance(frame, CdpCommand)
        return CdpCommand(id=frame.id, method=frame.method, params={**(frame.params or {}), "second": True})

    pipeline = MiddlewarePipeline([add_first_param])
    pipeline.add(add_second_param)

    result = await pipeline.process(
        CdpCommand(id=1, method="Runtime.enable"), Direction.CLIENT_TO_UPSTREAM, make_session()
    )

    assert result == CdpCommand(id=1, method="Runtime.enable", params={"first": True, "second": True})
    assert calls == [
        ("first", Direction.CLIENT_TO_UPSTREAM, "session"),
        ("second", Direction.CLIENT_TO_UPSTREAM, "session"),
    ]


@pytest.mark.asyncio
async def test_pipeline_stops_after_middleware_drops_frame() -> None:
    calls: list[str] = []

    async def drop(frame: CdpFrame, direction: Direction, session: ProxySession) -> None:
        calls.append("drop")
        return None

    async def should_not_run(frame: CdpFrame, direction: Direction, session: ProxySession) -> CdpFrame:
        calls.append("unexpected")
        return frame

    pipeline = MiddlewarePipeline([drop, should_not_run])

    result = await pipeline.process(
        CdpCommand(id=1, method="Runtime.enable"), Direction.UPSTREAM_TO_CLIENT, make_session()
    )

    assert result is None
    assert calls == ["drop"]


@pytest.mark.asyncio
async def test_empty_pipeline_returns_original_frame() -> None:
    frame = CdpCommand(id=1, method="Runtime.enable")

    assert await MiddlewarePipeline().process(frame, Direction.CLIENT_TO_UPSTREAM, make_session()) == frame


@pytest.mark.asyncio
async def test_interceptors_run_in_order_and_rewrite_the_command() -> None:
    calls: list[str] = []

    async def first(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        calls.append("first")
        return CdpCommand(id=command.id, method=command.method, params={"first": True})

    async def second(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        calls.append("second")
        return CdpCommand(id=command.id, method=command.method, params={**(command.params or {}), "second": True})

    pipeline = MiddlewarePipeline(interceptors=[first, second])

    result = await pipeline.intercept(CdpCommand(id=7, method="Page.navigate"), make_session(), make_context())

    assert result == CdpCommand(id=7, method="Page.navigate", params={"first": True, "second": True})
    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_synthesized_response_short_circuits_later_interceptors() -> None:
    calls: list[str] = []

    async def answer(command: CdpCommand, session: ProxySession, context: InterceptContext) -> SynthesizedResponse:
        calls.append("answer")
        return SynthesizedResponse(result={"ok": True}, reason="test")

    async def never_runs(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        calls.append("never")
        return command

    pipeline = MiddlewarePipeline(interceptors=[answer, never_runs])
    command = CdpCommand(id=9, method="Page.navigate", session_id="sess-1")

    outcome = await pipeline.intercept(command, make_session(), make_context())

    assert isinstance(outcome, SynthesizedResponse)
    assert calls == ["answer"]
    # Stamping is the correlation guarantee: the response reuses the command's own
    # id and session scope, so an interceptor cannot mis-correlate a synthesis.
    response = outcome.to_response(command)
    assert response.id == 9
    assert response.session_id == "sess-1"
    assert response.result == {"ok": True}
    assert response.error is None


def test_synthesized_response_requires_exactly_one_of_result_and_error() -> None:
    with pytest.raises(ValueError):
        SynthesizedResponse()
    with pytest.raises(ValueError):
        SynthesizedResponse(result={}, error={"code": 1, "message": "x"})


def test_synthesized_error_must_be_a_valid_cdp_error() -> None:
    with pytest.raises(ValueError):
        SynthesizedResponse(error={"code": 1})
    with pytest.raises(ValueError):
        SynthesizedResponse(error={"message": "no code"})
    with pytest.raises(ValueError):
        SynthesizedResponse(error={"code": 1, "message": "x", "extra": True})


def test_synthesized_response_requires_a_reason() -> None:
    with pytest.raises(ValueError):
        SynthesizedResponse(result={}, reason="")


def test_synthesized_payloads_must_be_cdp_encodable() -> None:
    # A synthesis that cannot be JSON-encoded must fail at construction — inside
    # the interceptor, where the fail-closed rule converts it into a deterministic
    # error — never at delivery, where it would tear the relay down instead.
    with pytest.raises(ValueError):
        SynthesizedResponse(result={"blob": b"bytes"})
    with pytest.raises(ValueError):
        SynthesizedResponse(result={"nan": float("nan")})
    with pytest.raises(ValueError):
        SynthesizedResponse(result={"obj": object()})
    with pytest.raises(ValueError):
        SynthesizedResponse(error={"code": 1, "message": "x", "data": {"blob": b"bytes"}})


def test_reason_must_be_a_label_safe_identifier() -> None:
    # The reason becomes a metric label: request-derived content (URLs, ids,
    # error text) must be unrepresentable, which is what bounds cardinality.
    for bad in ("has space", "https://example.com", "UPPER", "x" * 65, "dash-ed"):
        with pytest.raises(ValueError):
            SynthesizedResponse(result={}, reason=bad)
    assert SynthesizedResponse(result={}, reason="org_denylist_v2").reason == "org_denylist_v2"


@pytest.mark.asyncio
async def test_interceptor_must_preserve_the_request_id() -> None:
    async def renumber(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        return CdpCommand(id=command.id + 1, method=command.method)

    pipeline = MiddlewarePipeline(interceptors=[renumber])

    with pytest.raises(InterceptorContractError):
        await pipeline.intercept(CdpCommand(id=1, method="Page.navigate"), make_session(), make_context())


@pytest.mark.asyncio
async def test_interceptor_must_preserve_session_addressing() -> None:
    async def rescope(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        return CdpCommand(id=command.id, method=command.method, session_id="other-session")

    async def readdress(command: CdpCommand, session: ProxySession, context: InterceptContext) -> CdpCommand:
        return CdpCommand(id=command.id, method=command.method, params={"sessionId": "other-session"})

    with pytest.raises(InterceptorContractError):
        await MiddlewarePipeline(interceptors=[rescope]).intercept(
            CdpCommand(id=1, method="Page.navigate", session_id="mine"), make_session(), make_context()
        )
    with pytest.raises(InterceptorContractError):
        await MiddlewarePipeline(interceptors=[readdress]).intercept(
            CdpCommand(id=1, method="Target.detachFromTarget", params={"sessionId": "mine"}),
            make_session(),
            make_context(),
        )


@pytest.mark.asyncio
async def test_interceptor_must_return_a_command_or_a_synthesis() -> None:
    async def drops(command: CdpCommand, session: ProxySession, context: InterceptContext) -> None:
        return None

    pipeline = MiddlewarePipeline(interceptors=[drops])

    # A silently dropped command would hang its client forever, so an interceptor
    # has no drop verb at all — blocking means synthesizing an error response.
    with pytest.raises(InterceptorContractError):
        await pipeline.intercept(CdpCommand(id=1, method="Page.navigate"), make_session(), make_context())


@pytest.mark.asyncio
async def test_no_interceptors_is_identity() -> None:
    command = CdpCommand(id=1, method="Page.navigate")
    pipeline = MiddlewarePipeline()

    assert pipeline.has_interceptors is False
    assert await pipeline.intercept(command, make_session(), make_context()) is command
    assert MiddlewarePipeline(interceptors=[]).has_interceptors is False


def test_interceptor_failure_response_is_deterministic() -> None:
    command = CdpCommand(id=13, method="Page.navigate", session_id="sess-1")

    response = interceptor_failure_response(command)

    assert response.id == 13
    assert response.session_id == "sess-1"
    assert response.result is None
    assert response.error is not None
    assert response.error["code"] == -32603
    assert response == interceptor_failure_response(command)
