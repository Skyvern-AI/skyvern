"""Tests for the copilot.turn span wrapper around run_copilot_agent."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from opentelemetry import trace as otel_trace
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.copilot import agent as copilot_agent
from skyvern.forge.sdk.copilot import live_narrative
from skyvern.forge.sdk.copilot.context import CopilotContext, LiveTurnNarrative
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
)
from tests.unit.test_copilot_credential_pause import _FakeCache


def _make_chat_request(
    *,
    message: str = "Hello, build me a workflow",
    workflow_copilot_chat_id: str | None = "chat_abc",
    workflow_permanent_id: str = "wpid_xyz",
) -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id=workflow_permanent_id,
        workflow_id="w_001",
        workflow_copilot_chat_id=workflow_copilot_chat_id,
        message=message,
        workflow_yaml="",
    )


def _user_message(content: str) -> WorkflowCopilotChatHistoryMessage:
    return WorkflowCopilotChatHistoryMessage(
        sender=WorkflowCopilotChatSender.USER,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


async def _stub_build_request_policy_with_child_span(*_args: Any, **_kwargs: Any) -> RequestPolicy:
    """Open an OTel child span so the test can assert parentage. The request policy no longer ends the
    turn early, so patched_request_policy_trust_floor also stubs the agent loop."""
    tracer = otel_trace.get_tracer("test.copilot.turn")
    with tracer.start_as_current_span("test.req_policy_child"):
        pass
    return RequestPolicy(
        user_response_policy="ask_clarification",
        clarification_question="What URL should I target?",
        clarification_reason="missing_target_url",
    )


async def _stub_agent_loop_without_model_call(*_args: Any, **_kwargs: Any) -> None:
    """Stand in for the Agents SDK loop so the turn ends without sending a request to OpenAI."""
    raise RuntimeError("model call stubbed out in tests")


async def _raise_unhandled_turn_error(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("boom")


@pytest.fixture
def patched_request_policy_trust_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        copilot_agent,
        "build_request_policy_trust_floor",
        _stub_build_request_policy_with_child_span,
    )
    monkeypatch.setattr(copilot_agent, "_run_agent_loop_with_surface", _stub_agent_loop_without_model_call)


def _find_span(spans: list[Any], name: str) -> Any:
    matches = [s for s in spans if s.name == name]
    assert matches, f"expected span named {name!r}, got: {[s.name for s in spans]}"
    assert len(matches) == 1, f"expected exactly one span named {name!r}, got {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_copilot_turn_span_parents_inner_spans(
    span_exporter: Any,
    patched_request_policy_trust_floor: None,
) -> None:
    chat_request = _make_chat_request(message="Hello, build me a workflow")
    chat_history = [_user_message("prior question")]

    result = await copilot_agent.run_copilot_agent(
        stream=object(),  # never used: the stubbed agent loop raises before streaming
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=chat_history,
        global_llm_context=None,
        llm_api_handler=None,
    )

    assert result is not None
    spans = span_exporter.get_finished_spans()
    turn_span = _find_span(spans, "copilot.turn")
    child_span = _find_span(spans, "test.req_policy_child")

    assert child_span.parent is not None
    assert child_span.parent.span_id == turn_span.context.span_id

    attrs = dict(turn_span.attributes or {})
    assert attrs.get("skyvern.span.role") == "wrapper"
    assert attrs.get("copilot.session_id") == "chat_abc"
    assert attrs.get("workflow_permanent_id") == "wpid_xyz"
    # Zero-based: one prior user msg in history → this turn is index 1.
    assert attrs.get("copilot.turn_index") == 1
    assert "copilot.user_message_preview" not in attrs
    assert attrs.get("copilot.user_message_length") == len(chat_request.message)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raise", "hang"])
async def test_a_failing_live_narrative_cache_neither_fails_nor_delays_the_turn(
    span_exporter: Any,
    patched_request_policy_trust_floor: None,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    attempts: list[str] = []
    retried = asyncio.Event()

    class _FailingCache(_FakeCache):
        async def set(self, key: str, value: Any, ex: Any = None) -> None:
            attempts.append(key)
            if failure == "hang" or len(attempts) > 1:
                retried.set()
            if failure == "raise":
                raise RuntimeError("cache unavailable")
            await asyncio.Event().wait()

    async def hydrate_after_a_snapshot_retry(*_args: Any, **_kwargs: Any) -> None:
        await retried.wait()

    monkeypatch.setattr(live_narrative, "LIVE_TURN_NARRATIVE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(app, "CACHE", _FailingCache())
    monkeypatch.setattr(copilot_agent, "hydrate_work_plan", hydrate_after_a_snapshot_retry)
    ctx_sink: list[CopilotContext] = []

    with capture_logs() as logs:
        # Shorter than the write's own timeout, so a turn end that waited on the hanging write would miss it.
        result = await asyncio.wait_for(
            copilot_agent.run_copilot_agent(
                stream=object(),
                organization_id="o_test",
                chat_request=_make_chat_request(),
                chat_history=[],
                global_llm_context=None,
                llm_api_handler=None,
                ctx_sink=ctx_sink,
            ),
            timeout=3,
        )

    assert result is not None
    writer = ctx_sink[0].live_narrative_task
    assert writer is not None
    await asyncio.wait({writer}, timeout=1)
    assert writer.cancelled()
    assert attempts and set(attempts) == {f"ember:turn_narrative:o_test:chat_abc:{ctx_sink[0].turn_id}"}
    if failure == "raise":
        assert [log["event"] for log in logs].count("Live turn narrative write failed") == 1


@pytest.mark.asyncio
async def test_live_narrative_writer_rewrites_an_unchanged_copy_only_on_change_or_after_half_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    monkeypatch.setattr(app, "CACHE", cache)
    now = 0.0
    terminal = "running"
    builds = 0

    def build() -> LiveTurnNarrative:
        nonlocal builds
        builds += 1
        return cast(LiveTurnNarrative, {"terminal": terminal})

    async def settle(condition: Callable[[], bool]) -> None:
        # Below the 2 s poll, so only the wake can have started the pass.
        async with asyncio.timeout(1):
            while not condition():
                await asyncio.sleep(0)

    wake = asyncio.Event()
    writer = asyncio.create_task(live_narrative.write_live_turn_narrative("key", build, wake, clock=lambda: now))
    try:
        await settle(lambda: len(cache.set_calls) == 1)
        builds_before = builds
        wake.set()
        await settle(lambda: builds > builds_before)
        assert len(cache.set_calls) == 1

        terminal = "paused"
        wake.set()
        await settle(lambda: len(cache.set_calls) == 2)

        now = live_narrative.LIVE_TURN_NARRATIVE_TTL_SECONDS / 2
        wake.set()
        await settle(lambda: len(cache.set_calls) == 3)
        assert cache.set_calls[2][1] == cache.set_calls[1][1]
    finally:
        writer.cancel()


@pytest.mark.asyncio
async def test_copilot_turn_span_uses_explicit_turn_index(
    span_exporter: Any,
    patched_request_policy_trust_floor: None,
) -> None:
    chat_request = _make_chat_request()
    chat_history = [_user_message("a"), _user_message("b"), _user_message("c")]

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=chat_history,
        global_llm_context=None,
        llm_api_handler=None,
        turn_index=42,
    )

    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    assert dict(turn_span.attributes or {}).get("copilot.turn_index") == 42


@pytest.mark.asyncio
async def test_copilot_turn_span_omits_session_id_when_missing(
    span_exporter: Any,
    patched_request_policy_trust_floor: None,
) -> None:
    chat_request = _make_chat_request(workflow_copilot_chat_id=None)

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=None,
    )

    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    attrs = dict(turn_span.attributes or {})
    assert "copilot.session_id" not in attrs


@pytest.mark.asyncio
async def test_copilot_turn_span_ingress_telemetry_is_content_free(
    span_exporter: Any,
    patched_request_policy_trust_floor: None,
) -> None:
    long_msg = "Line one\nLine two with extra content " + ("x" * 200)
    chat_request = _make_chat_request(message=long_msg)

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=None,
    )

    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    attrs = dict(turn_span.attributes or {})
    assert "copilot.user_message_preview" not in attrs
    assert attrs.get("copilot.user_message_length") == len(long_msg)


@pytest.mark.asyncio
async def test_outer_turn_recovery_records_error_attrs_on_turn_span(
    span_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(copilot_agent, "_run_copilot_turn_impl", _raise_unhandled_turn_error)
    chat_request = _make_chat_request()

    result = await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=None,
    )

    assert result.updated_workflow is None
    assert "reference cpe_" in result.user_response
    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    attrs = dict(turn_span.attributes or {})
    assert attrs.get("copilot.error_recovered") is True
    assert attrs.get("copilot.error_failure_kind") == "unknown"
    assert attrs.get("copilot.error_exception_type") == "RuntimeError"
    assert attrs.get("copilot.error_reply_proposal_disposition") == result.proposal_disposition
    assert attrs.get("copilot.error_workflow_modified") is False


@pytest.mark.asyncio
async def test_outer_turn_recovery_preserves_browser_ablation_session_for_cleanup(
    span_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_after_session(*, ctx_sink: list[Any], stream: Any, **kwargs: Any) -> None:
        ctx_sink.append(
            copilot_agent.CopilotContext(
                organization_id=kwargs["organization_id"],
                workflow_id=kwargs["chat_request"].workflow_id,
                workflow_permanent_id=kwargs["chat_request"].workflow_permanent_id,
                workflow_yaml="",
                browser_session_id="pbs_cleanup",
                stream=stream,
                api_key=kwargs["api_key"],
                user_message=kwargs["chat_request"].message,
                workflow_copilot_chat_id=kwargs["chat_request"].workflow_copilot_chat_id,
                eval_mode=copilot_agent.CopilotEvalMode.BROWSER_ABLATION,
            )
        )
        raise RuntimeError("boom after session creation")

    monkeypatch.setattr(copilot_agent, "_run_copilot_turn_impl", fail_after_session)

    result = await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=_make_chat_request(),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=None,
        api_key="sk-test",
        eval_mode=copilot_agent.CopilotEvalMode.BROWSER_ABLATION,
    )

    assert result.browser_ablation_metadata is not None
    assert result.browser_ablation_metadata["browser_session_id"] == "pbs_cleanup"


@pytest.mark.asyncio
async def test_cancelled_browser_ablation_turn_closes_created_session(
    span_exporter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancel_after_session(*, ctx_sink: list[Any], stream: Any, **kwargs: Any) -> None:
        ctx_sink.append(
            copilot_agent.CopilotContext(
                organization_id=kwargs["organization_id"],
                workflow_id=kwargs["chat_request"].workflow_id,
                workflow_permanent_id=kwargs["chat_request"].workflow_permanent_id,
                workflow_yaml="",
                browser_session_id="pbs_cancelled",
                stream=stream,
                api_key=kwargs["api_key"],
                user_message=kwargs["chat_request"].message,
                workflow_copilot_chat_id=kwargs["chat_request"].workflow_copilot_chat_id,
                eval_mode=copilot_agent.CopilotEvalMode.BROWSER_ABLATION,
            )
        )
        raise asyncio.CancelledError

    close_session = AsyncMock()
    monkeypatch.setattr(copilot_agent, "_run_copilot_turn_impl", cancel_after_session)
    monkeypatch.setattr(copilot_agent, "close_browser_session_quietly", close_session)

    with pytest.raises(asyncio.CancelledError):
        await copilot_agent.run_copilot_agent(
            stream=object(),
            organization_id="o_test",
            chat_request=_make_chat_request(),
            chat_history=[],
            global_llm_context=None,
            llm_api_handler=None,
            api_key="sk-test",
            eval_mode=copilot_agent.CopilotEvalMode.BROWSER_ABLATION,
        )

    close_session.assert_awaited_once_with("o_test", "pbs_cancelled")
