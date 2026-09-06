"""Tests for the per-iteration Runner deadline (SKY-9243)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.build_test_outcome import record_build_test_outcome
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    TOTAL_TIMEOUT_SECONDS,
    CopilotTotalTimeoutError,
    _mark_copilot_total_timeout,
    run_with_enforcement,
)
from skyvern.forge.sdk.copilot.pending_operation import (
    _turn_operations,
    pending_operation,
    pending_operation_fields,
)
from skyvern.forge.sdk.copilot.tools import run_execution
from skyvern.forge.sdk.copilot.tools.run_execution import _run_blocks_and_collect_debug
from tests.unit.copilot_test_helpers import (
    count_record_and_send,
    failed_second_factor_run,
    handback_ctx,
    make_copilot_ctx,
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
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
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

    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    with pytest.raises(CopilotTotalTimeoutError):
        await run_with_enforcement(
            agent=MagicMock(),
            initial_input="hello",
            ctx=ctx,
            stream=stream,
        )
    assert ctx.copilot_total_timeout_exceeded is True
    assert ctx.budget_expiry_state.hard_backstop_reached is True
    assert ctx.budget_expiry_state.source == "deadline"
    assert ctx.budget_expiry_state.drain_attempted is False


@pytest.mark.asyncio
async def test_runner_deadline_protects_context_overflow_recovery_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
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

    ctx = make_copilot_ctx()
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

    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    returned = await run_with_enforcement(
        agent=MagicMock(),
        initial_input="hello",
        ctx=ctx,
        stream=stream,
    )
    assert returned is fake
    assert ctx.copilot_total_timeout_exceeded is False


def _cancellation_ctx() -> CopilotContext:
    ctx = make_copilot_ctx()
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


@pytest.mark.asyncio
async def test_deadline_event_names_the_operation_still_open_when_the_budget_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.02)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: _fake_result(),
    )

    async def hanging_stream(result: Any, s: Any, c: Any) -> None:
        with pending_operation("mcp.call_tool:run_block"):
            await asyncio.sleep(5.0)

    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", hanging_stream)

    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    with capture_logs() as logs:
        with pytest.raises(CopilotTotalTimeoutError):
            await run_with_enforcement(agent=MagicMock(), initial_input="hello", ctx=ctx, stream=stream)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["pending_operation"] == "mcp.call_tool:run_block"
    assert isinstance(events[0]["pending_operation_started_monotonic"], float)
    assert events[0]["pending_operation_state"] == "unwound_by_cancellation"
    assert events[0]["iteration"] == 0
    assert events[0]["elapsed_seconds"] >= 0.05


@pytest.mark.asyncio
async def test_a_broken_fingerprint_reader_neither_masks_nor_delays_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_fields() -> dict[str, str | float | int]:
        raise RuntimeError("fingerprint reader is broken")

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.pending_operation_fields",
        exploding_fields,
    )
    ctx = _cancellation_ctx()

    logs, _ = await _cancel_at_boundary(monkeypatch, boundary="first", elapsed=588.0, ctx=ctx)

    assert _cancellation_events(logs) == []
    assert any(entry.get("event") == "Failed to record a copilot turn cancellation" for entry in logs)


@pytest.mark.asyncio
async def test_the_deadline_names_the_inner_operation_that_returned_over_the_outer_one_still_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.02)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: _fake_result(),
    )

    async def stream_stalling_after_a_tool_returned(result: Any, s: Any, c: Any) -> None:
        with pending_operation("mcp.call_tool:run_block"):
            await asyncio.sleep(0)
        await asyncio.sleep(5.0)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
        stream_stalling_after_a_tool_returned,
    )

    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    with capture_logs() as logs:
        with pytest.raises(CopilotTotalTimeoutError):
            await run_with_enforcement(agent=MagicMock(), initial_input="hello", ctx=ctx, stream=stream)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["pending_operation"] == "mcp.call_tool:run_block"
    assert events[0]["pending_operation_state"] == "returned"
    assert events[0]["pending_operation_open_count"] == 1, "the outer turn.stream scope is still open"


@pytest.mark.asyncio
async def test_the_deadline_names_an_operation_that_exited_by_exception_and_never_calls_it_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.02)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: _fake_result(),
    )

    async def stream_stalling_after_a_tool_failed(result: Any, s: Any, c: Any) -> None:
        with contextlib.suppress(RuntimeError):
            with pending_operation("mcp.call_tool:run_block"):
                raise RuntimeError("tool blew up")
        await asyncio.sleep(5.0)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse",
        stream_stalling_after_a_tool_failed,
    )

    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    with capture_logs() as logs:
        with pytest.raises(CopilotTotalTimeoutError):
            await run_with_enforcement(agent=MagicMock(), initial_input="hello", ctx=ctx, stream=stream)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert events[0]["pending_operation"] == "mcp.call_tool:run_block"
    assert events[0]["pending_operation_state"] == "unwound_by_error"
    assert events[0]["pending_operation_open_count"] == 1, "the outer turn.stream scope is still open"


def test_the_fingerprint_reader_reports_nothing_rather_than_raising_on_an_unreadable_slot() -> None:
    token = _turn_operations.set(cast(Any, object()))
    try:
        assert pending_operation_fields() == {}
    finally:
        _turn_operations.reset(token)


@pytest.mark.asyncio
async def test_the_deadline_path_is_unchanged_when_the_fingerprint_contributes_no_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.TOTAL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.HARD_BACKSTOP_ALLOWANCE_SECONDS", 0)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.MIN_DEADLINE_REMAINING_SECONDS", 0.02)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.pending_operation_fields", dict)

    stream = MagicMock()
    stream.is_disconnected = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.Runner.run_streamed",
        lambda *a, **kw: _fake_result(),
    )

    async def hanging_stream(result: Any, s: Any, c: Any) -> None:
        await asyncio.sleep(5.0)

    monkeypatch.setattr("skyvern.forge.sdk.copilot.streaming_adapter.stream_to_sse", hanging_stream)

    ctx = make_copilot_ctx()
    ctx.copilot_total_timeout_exceeded = False
    with capture_logs() as logs:
        with pytest.raises(CopilotTotalTimeoutError):
            await run_with_enforcement(agent=MagicMock(), initial_input="hello", ctx=ctx, stream=stream)

    events = _deadline_events(logs)
    assert len(events) == 1
    assert "pending_operation" not in events[0]
    assert events[0]["iteration"] == 0
    assert events[0]["elapsed_seconds"] >= 0.05
    assert ctx.copilot_total_timeout_exceeded is True

    with capture_logs() as already_marked:
        _mark_copilot_total_timeout(ctx, elapsed_seconds=99.0, iteration=1)
    assert _deadline_events(already_marked) == []


async def _run_with_failing_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    enrichment: Callable[..., Awaitable[tuple[str, dict[str, object] | None]]],
) -> tuple[CopilotContext, dict[str, int]]:
    ctx = await handback_ctx(monkeypatch, polled_status="completed", block_status="completed")
    record_build_test_outcome(ctx, failed_second_factor_run("wr_prior_failure"))
    counts = count_record_and_send(monkeypatch)
    monkeypatch.setattr(run_execution, "_attach_post_run_browser_enrichment", enrichment)
    return ctx, counts


def _assert_later_run_survived(ctx: CopilotContext, counts: dict[str, int]) -> None:
    outcome = ctx.latest_recorded_build_test_outcome
    assert outcome is not None
    assert outcome.workflow_run_id == "wr_paused"
    assert outcome.verdict != "repairable_failure"
    assert counts["record"] == 1
    assert [entry["workflow_run_id"] for entry in ctx.recorded_build_test_outcome_history] == [
        "wr_prior_failure",
        "wr_paused",
    ]


@pytest.mark.asyncio
async def test_completed_run_survives_an_enrichment_probe_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raising(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object] | None]:
        raise RuntimeError("post-run browser probe failed")

    ctx, counts = await _run_with_failing_enrichment(monkeypatch, _raising)

    with pytest.raises(RuntimeError, match="post-run browser probe failed"):
        await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    _assert_later_run_survived(ctx, counts)


@pytest.mark.asyncio
async def test_completed_run_survives_an_enrichment_probe_cancelled_by_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[str] = []

    async def _hanging(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object] | None]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("cancelled")
            raise
        return "", None

    ctx, counts = await _run_with_failing_enrichment(monkeypatch, _hanging)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx),
            timeout=0.05,
        )

    assert cancelled == ["cancelled"]
    _assert_later_run_survived(ctx, counts)


@pytest.mark.asyncio
async def test_bot_wall_seen_only_by_the_post_run_page_still_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every block reports completed and the run output alone looks clean; the challenge is visible
    only in the page evidence that arrives after the outcome is committed."""
    ctx = await handback_ctx(monkeypatch, polled_status="completed", block_status="completed")
    challenge_evidence = {
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_paused",
        "source_browser_session_id": "pbs_run",
        "current_url": "https://example.com/verify",
        "inspected_url": "https://example.com/verify",
        "anti_bot_indicators": ["captcha"],
        "challenge_state": {
            "detected": True,
            "kind": "captcha",
            "requires_human_verification": True,
        },
        "message": "Please complete the CAPTCHA to continue",
    }

    async def _enrichment(
        enrich_ctx: CopilotContext, result_data: dict[str, object], **_kwargs: object
    ) -> tuple[str, dict[str, object]]:
        enrich_ctx.composition_page_evidence = challenge_evidence
        result_data["post_run_page_evidence"] = challenge_evidence
        return "https://example.com/verify", challenge_evidence

    monkeypatch.setattr(run_execution, "_attach_post_run_browser_enrichment", _enrichment)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert result["ok"] is False, "a run that ended on a bot wall was reported as passing"
    assert ctx.last_test_anti_bot, ctx.last_test_anti_bot
    assert ctx.last_full_workflow_test_ok is False
    # The emitted envelope must agree with the failure the caller returns.
    assert ctx.last_run_outcome is not None
    assert ctx.last_run_outcome.verdict == "not_demonstrated", ctx.last_run_outcome
    assert ctx.last_run_outcome.run_completed is False
    # The success-branch latches must not survive a run the settle turned into a failure.
    assert ctx.verified_terminal_proposal_ready is False


@pytest.mark.asyncio
async def test_prior_run_page_evidence_does_not_fail_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A challenge page left on ctx by an earlier run must not grade this one: the capture that drops
    another run's page runs after the record, and the anti-bot read has no run-id check."""
    ctx = await handback_ctx(monkeypatch, polled_status="completed", block_status="completed")
    ctx.composition_page_evidence = {
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_some_older_run",
        "source_browser_session_id": "pbs_run",
        "current_url": "https://example.com/verify",
        "anti_bot_indicators": ["captcha"],
        "challenge_state": {"detected": True, "kind": "captcha", "requires_human_verification": True},
        "message": "Please complete the CAPTCHA to continue",
    }

    async def _enrichment(
        _enrich_ctx: CopilotContext, _result_data: dict[str, object], **_kwargs: object
    ) -> tuple[str, dict[str, object] | None]:
        return "https://example.com/done", None

    monkeypatch.setattr(run_execution, "_attach_post_run_browser_enrichment", _enrichment)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert result["ok"] is True, "another run's page evidence failed this run"
    assert ctx.last_test_anti_bot is None, ctx.last_test_anti_bot


@pytest.mark.asyncio
async def test_enrichment_supplied_page_reaches_the_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the real _attach_post_run_browser_enrichment. Every other test stubs the helper and
    plants the page on ctx first, which cannot tell a page enrichment supplied from one already
    there -- the distinction this ordering is entirely about."""
    ctx = await handback_ctx(monkeypatch, polled_status="completed", block_status="completed")
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    assert ctx.composition_page_evidence is None, "the page must arrive from enrichment, not the fixture"

    challenge_page = {
        "source_tool": "inspect_page_for_composition",
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_paused",
        "current_url": "https://example.com/verify",
        "inspected_url": "https://example.com/verify",
        "anti_bot_indicators": ["captcha"],
        "challenge_state": {"detected": True, "kind": "captcha", "requires_human_verification": True},
        "message": "Please complete the CAPTCHA to continue",
    }

    async def _page_info(*_args: object, **_kwargs: object) -> tuple[str, str | None, str | None]:
        return "https://example.com/verify", "Verify you are human", None

    async def _read_evidence(*_args: object, **_kwargs: object) -> tuple[dict[str, object], str, None, None]:
        return challenge_page, "pbs_run", None, None

    monkeypatch.setattr(run_execution, "_resolve_post_run_page_info", _page_info)
    monkeypatch.setattr(run_execution, "_read_run_session_page_evidence", _read_evidence)

    result = await _run_blocks_and_collect_debug({"block_labels": ["extract_heading"], "parameters": {}}, ctx)

    assert ctx.composition_page_evidence is not None, "the real enrichment never stored the page"
    assert result["ok"] is False, "a page supplied by enrichment did not reach the grade"
    assert ctx.last_test_anti_bot, ctx.last_test_anti_bot
