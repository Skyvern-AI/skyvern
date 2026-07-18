from __future__ import annotations

import pytest

from skyvern.proxy.core.frames import CdpCommand, CdpFrame
from skyvern.proxy.core.pipeline import Direction, MiddlewarePipeline
from skyvern.proxy.core.session import Principal, ProxySession


def make_session() -> ProxySession:
    return ProxySession(
        session_id="session",
        upstream_ws_url="ws://localhost:1",
        principal=Principal(principal_id="owner"),
    )


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
