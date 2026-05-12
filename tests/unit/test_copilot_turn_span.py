"""Tests for the copilot.turn span wrapper around run_copilot_agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from opentelemetry import trace as otel_trace

from skyvern.forge.sdk.copilot import agent as copilot_agent
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.schemas.workflow_copilot import (
    WorkflowCopilotChatHistoryMessage,
    WorkflowCopilotChatRequest,
    WorkflowCopilotChatSender,
)


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
    """Open an OTel child span so the test can assert parentage, then short-circuit
    run_copilot_agent via the ask_clarification early-return path."""
    tracer = otel_trace.get_tracer("test.copilot.turn")
    with tracer.start_as_current_span("test.req_policy_child"):
        pass
    return RequestPolicy(
        user_response_policy="ask_clarification",
        clarification_question="What URL should I target?",
        clarification_reason="missing_target_url",
    )


async def _stub_build_request_policy_proceed(*_args: Any, **_kwargs: Any) -> RequestPolicy:
    return RequestPolicy(user_response_policy="ask_clarification", clarification_question="?")


@pytest.fixture
def patched_build_request_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        copilot_agent,
        "build_request_policy",
        _stub_build_request_policy_with_child_span,
    )


def _find_span(spans: list[Any], name: str) -> Any:
    matches = [s for s in spans if s.name == name]
    assert matches, f"expected span named {name!r}, got: {[s.name for s in spans]}"
    assert len(matches) == 1, f"expected exactly one span named {name!r}, got {len(matches)}"
    return matches[0]


@pytest.mark.asyncio
async def test_copilot_turn_span_parents_inner_spans(
    span_exporter: Any,
    patched_build_request_policy: None,
) -> None:
    chat_request = _make_chat_request(message="Hello, build me a workflow")
    chat_history = [_user_message("prior question")]

    result = await copilot_agent.run_copilot_agent(
        stream=object(),  # never used on the ask_clarification path
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=chat_history,
        global_llm_context=None,
        debug_run_info_text="",
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
    # one prior user msg in history + this turn = 2.
    assert attrs.get("copilot.turn_index") == 2
    preview = attrs.get("copilot.user_message_preview")
    assert isinstance(preview, str) and preview.startswith("Hello")


@pytest.mark.asyncio
async def test_copilot_turn_span_uses_explicit_turn_index(
    span_exporter: Any,
    patched_build_request_policy: None,
) -> None:
    chat_request = _make_chat_request()
    chat_history = [_user_message("a"), _user_message("b"), _user_message("c")]

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=chat_history,
        global_llm_context=None,
        debug_run_info_text="",
        llm_api_handler=None,
        turn_index=42,
    )

    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    assert dict(turn_span.attributes or {}).get("copilot.turn_index") == 42


@pytest.mark.asyncio
async def test_copilot_turn_span_omits_session_id_when_missing(
    span_exporter: Any,
    patched_build_request_policy: None,
) -> None:
    chat_request = _make_chat_request(workflow_copilot_chat_id=None)

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=[],
        global_llm_context=None,
        debug_run_info_text="",
        llm_api_handler=None,
    )

    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    attrs = dict(turn_span.attributes or {})
    assert "copilot.session_id" not in attrs


@pytest.mark.asyncio
async def test_copilot_turn_span_preview_is_single_line_and_truncated(
    span_exporter: Any,
    patched_build_request_policy: None,
) -> None:
    long_msg = "Line one\nLine two with extra content " + ("x" * 200)
    chat_request = _make_chat_request(message=long_msg)

    await copilot_agent.run_copilot_agent(
        stream=object(),
        organization_id="o_test",
        chat_request=chat_request,
        chat_history=[],
        global_llm_context=None,
        debug_run_info_text="",
        llm_api_handler=None,
    )

    turn_span = _find_span(span_exporter.get_finished_spans(), "copilot.turn")
    preview = dict(turn_span.attributes or {}).get("copilot.user_message_preview")
    assert isinstance(preview, str)
    assert "\n" not in preview
    assert "\r" not in preview
    assert len(preview) <= copilot_agent._USER_MESSAGE_PREVIEW_MAX_CHARS
    assert preview.endswith("…")


def test_build_user_message_preview_redacts_known_secret_patterns() -> None:
    preview = copilot_agent._build_user_message_preview("password: hunter2 please")
    assert "hunter2" not in preview
    assert "REDACTED_SECRET" in preview
