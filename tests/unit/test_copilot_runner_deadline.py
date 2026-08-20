"""Tests for the per-iteration Runner deadline (SKY-9243)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.enforcement import (
    TOTAL_TIMEOUT_SECONDS,
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
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.02)

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
    ctx.copilot_total_timeout_exceeded = False
    with pytest.raises(CopilotTotalTimeoutError):
        await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
    assert ctx.copilot_total_timeout_exceeded is True


@pytest.mark.asyncio
async def test_runner_deadline_protects_context_overflow_recovery_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.02)

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
    ctx.copilot_total_timeout_exceeded = False
    with pytest.raises(CopilotTotalTimeoutError):
        await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
    assert call_count["n"] == 2, "recovery path should have triggered a second Runner call"
    assert ctx.copilot_total_timeout_exceeded is True


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
    ctx.copilot_total_timeout_exceeded = False
    returned = await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
    )
    assert returned is fake
    assert ctx.copilot_total_timeout_exceeded is False


def _cancellation_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.copilot_total_timeout_exceeded = False
    ctx.copilot_credential_pause_seconds = 0.0
    ctx.copilot_turn_cancelled_iteration = None
    return ctx


def _cancellation_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "copilot_turn_cancelled"]


def _deadline_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "copilot_turn_deadline_expired"]


class _CancellingClock:
    """``time.monotonic`` that jumps to ``elapsed`` only once the boundary is reached.

    The loop head raises its own deadline error before the model call when elapsed already
    exceeds the budget, so the jump has to land where the cancellation does.
    """

    def __init__(self, elapsed: float) -> None:
        self.elapsed = elapsed
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def reached_boundary(self) -> None:
        self.offset = self.elapsed


async def _cancel_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    boundary: str,
    elapsed: float,
    ctx: MagicMock,
) -> tuple[list[dict[str, Any]], int]:
    """Drive one real ``CancelledError`` through ``run_with_enforcement`` at one boundary."""
    clock = _CancellingClock(elapsed)
    cancellation = asyncio.CancelledError()
    stream_calls = {"n": 0}

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    async def stream_to_sse(result: Any, s: Any, c: Any) -> None:
        stream_calls["n"] += 1
        if stream_calls["n"] == 1 and boundary in ("overflow", "retry"):
            raise RuntimeError("context_length_exceeded: message too long")
        clock.reached_boundary()
        raise cancellation

    async def recover(session: Any, current_input: Any) -> Any:
        if boundary == "overflow":
            clock.reached_boundary()
            raise cancellation
        return current_input, False

    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.time", clock)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: _fake_result(),
    )
    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", stream_to_sse)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement._recover_from_context_overflow", recover)

    raised: BaseException | None = None
    with capture_logs() as logs:
        try:
            await run_with_enforcement(agent=MagicMock(), initial_input="hello", ctx=ctx, stream=stream)
        except BaseException as exc:  # noqa: BLE001 - the propagated object is the assertion
            raised = exc
    assert raised is cancellation, "the original cancellation must propagate unmasked"
    return logs, stream_calls["n"]


@pytest.mark.parametrize("boundary", ["first", "overflow", "retry"])
@pytest.mark.asyncio
async def test_sub_budget_cancellation_records_once_at_every_model_call_boundary(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    ctx = _cancellation_ctx()

    logs, stream_calls = await _cancel_at_boundary(monkeypatch, boundary=boundary, elapsed=588.0, ctx=ctx)

    events = _cancellation_events(logs)
    assert len(events) == 1
    assert 588.0 <= events[0]["elapsed_seconds"] < TOTAL_TIMEOUT_SECONDS
    assert events[0]["iteration"] == 0
    assert events[0]["deadline_exceeded"] is False
    assert _deadline_events(logs) == []
    assert ctx.copilot_total_timeout_exceeded is False
    assert ctx.copilot_turn_cancelled_iteration == 0
    if boundary == "retry":
        assert stream_calls == 2, "the retry boundary must run after an overflow recovery"


@pytest.mark.asyncio
async def test_over_budget_cancellation_records_the_deadline_beside_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _cancellation_ctx()

    logs, _ = await _cancel_at_boundary(monkeypatch, boundary="first", elapsed=950.0, ctx=ctx)

    events = _cancellation_events(logs)
    assert len(events) == 1
    assert events[0]["elapsed_seconds"] >= TOTAL_TIMEOUT_SECONDS
    assert events[0]["deadline_exceeded"] is True
    assert len(_deadline_events(logs)) == 1
    assert ctx.copilot_total_timeout_exceeded is True


@pytest.mark.asyncio
async def test_a_broken_recorder_neither_masks_nor_delays_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_mark(ctx: Any, start_time: float, iteration: int) -> None:
        raise RuntimeError("recorder is broken")

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement._mark_copilot_total_timeout_if_elapsed",
        exploding_mark,
    )
    ctx = _cancellation_ctx()

    logs, _ = await _cancel_at_boundary(monkeypatch, boundary="first", elapsed=588.0, ctx=ctx)

    assert _cancellation_events(logs) == []
    assert any(entry.get("event") == "Failed to record a copilot turn cancellation" for entry in logs)
