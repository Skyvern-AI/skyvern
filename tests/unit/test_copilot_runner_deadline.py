"""Tests for the per-iteration Runner deadline (SKY-9243)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.copilot.enforcement import (
    CopilotTotalTimeoutError,
    run_with_enforcement,
)


def _fake_result() -> MagicMock:
    r = MagicMock()
    r.final_output = None
    r.new_items = []
    r.to_input_list.return_value = []
    r.raw_responses = []
    return r


@pytest.mark.asyncio
async def test_runner_deadline_raises_total_timeout_when_tool_exceeds_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.05)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: _fake_result(),
    )

    async def hanging_stream(result: Any, s: Any, c: Any) -> None:
        await asyncio.sleep(5.0)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
        hanging_stream,
    )

    ctx = MagicMock()
    with pytest.raises(CopilotTotalTimeoutError):
        await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )


@pytest.mark.asyncio
async def test_runner_deadline_protects_context_overflow_recovery_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.05)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    call_count = {"n": 0}

    def fake_run_streamed(*a: Any, **kw: Any) -> Any:
        call_count["n"] += 1
        return _fake_result()

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        fake_run_streamed,
    )

    async def stream_impl(result: Any, s: Any, c: Any) -> None:
        if call_count["n"] == 1:
            raise Exception("context_length_exceeded: message too long")
        await asyncio.sleep(5.0)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
        stream_impl,
    )

    async def fake_recover(session: Any, current_input: Any) -> Any:
        return current_input, False

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement._recover_from_context_overflow",
        fake_recover,
    )

    ctx = MagicMock()
    with pytest.raises(CopilotTotalTimeoutError):
        await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
    assert call_count["n"] == 2, "recovery path should have triggered a second Runner call"


@pytest.mark.asyncio
async def test_runner_deadline_does_not_fire_when_tool_completes_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 5.0)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    fake = _fake_result()

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: fake,
    )

    async def quick_stream(result: Any, s: Any, c: Any) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
        quick_stream,
    )

    ctx = MagicMock()
    returned = await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
    )
    assert returned is fake
