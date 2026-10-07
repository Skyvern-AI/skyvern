from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import litellm
import pytest
from agents import ItemHelpers, ModelSettings, RunContextWrapper, function_tool
from agents.extensions.models.litellm_model import LitellmModel
from agents.items import TResponseInputItem
from agents.mcp import MCPServer, MCPUtil
from agents.models.interface import ModelTracing
from litellm.integrations.anthropic_cache_control_hook import AnthropicCacheControlHook
from litellm.litellm_core_utils.fallback_utils import async_completion_with_fallbacks
from litellm.llms.vertex_ai.gemini.transformation import _gemini_convert_messages_with_history
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.utils import Delta
from litellm.types.utils import ModelResponse as LiteLLMModelResponse
from litellm.types.utils import ModelResponseStream, StreamingChoices, Usage
from mcp.types import Tool as MCPTool
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import ResponseFunctionToolCall
from structlog.testing import capture_logs

from skyvern.cli.mcp_tools.blocks import skyvern_block_schema
from skyvern.forge.sdk.copilot import agent as copilot_agent_module
from skyvern.forge.sdk.copilot import cache_envelope as cache_envelope_module
from skyvern.forge.sdk.copilot import model_telemetry as model_telemetry_module
from skyvern.forge.sdk.copilot.browser_ablation import CopilotEvalMode
from skyvern.forge.sdk.copilot.cache_envelope import CacheableSystemInstructions
from skyvern.forge.sdk.copilot.config import CopilotConfig
from skyvern.forge.sdk.copilot.enforcement import NUDGE_SENTINEL, SCREENSHOT_SENTINEL, _prune_input_list
from skyvern.forge.sdk.copilot.llm_errors import is_retriable_llm_error
from skyvern.forge.sdk.copilot.mcp_adapter import _copilot_to_call_tool_result
from skyvern.forge.sdk.copilot.model_telemetry import (
    CopilotLitellmModel,
    _model_call_telemetry_scope,
    current_model_attempt_telemetry,
    current_model_call_telemetry,
    model_attempt_telemetry_scope,
)
from skyvern.forge.sdk.copilot.pending_operation import (
    _turn_operations,
    install_pending_operation_slot,
    pending_operation,
    pending_operation_fields,
)
from skyvern.forge.sdk.copilot.recoverable_failure import build_recoverable_failure
from skyvern.forge.sdk.copilot.session_factory import copilot_session_input_callback
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest

pytestmark = pytest.mark.usefixtures("gpt56_litellm_models")


@pytest.fixture(autouse=True)
def _clear_pending_operation_slot() -> Iterator[None]:
    """Tests here install a turn slot directly; without this it leaks into every later test in the
    worker, and the suite runs under pytest-randomly."""
    token = _turn_operations.set(None)
    yield
    _turn_operations.reset(token)


@function_tool
def _lookup_number(name: str) -> int:
    return len(name)


_GPT_REASONING = {"reasoning_effort": "medium"}


class _ChunkStream:
    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> _ChunkStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        self.closed = True


def _completion() -> LiteLLMModelResponse:
    return LiteLLMModelResponse(
        id="chatcmpl-test",
        created=1,
        model="openai/gpt-5.6",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "42"},
                "finish_reason": "stop",
            }
        ],
        usage=Usage(
            prompt_tokens=100,
            completion_tokens=7,
            total_tokens=107,
            prompt_tokens_details={"cached_tokens": 31, "cache_write_tokens": 47},
        ),
    )


def _responses_completion() -> ResponsesAPIResponse:
    return ResponsesAPIResponse(
        id="resp-test",
        created_at=1,
        model="gpt-5.6-sol",
        object="response",
        output=[
            {
                "id": "msg-test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "42", "annotations": []}],
            }
        ],
        usage={
            "input_tokens": 100,
            "output_tokens": 7,
            "total_tokens": 107,
            "input_tokens_details": {"cached_tokens": 31, "cache_write_tokens": 47},
        },
    )


def _stream_chunks() -> list[ChatCompletionChunk]:
    content = ModelResponseStream(
        id="chatcmpl-test",
        created=1,
        model="openai/gpt-5.6",
        choices=[],
    )
    content.choices = [
        StreamingChoices(
            index=0,
            delta=Delta(role="assistant", content="42"),
            finish_reason=None,
            logprobs=None,
        )
    ]
    final = ModelResponseStream(
        id="chatcmpl-test",
        created=1,
        model="openai/gpt-5.6",
        choices=[],
        usage=Usage(
            prompt_tokens=100,
            completion_tokens=7,
            total_tokens=107,
            prompt_tokens_details={"cached_tokens": 31, "cache_write_tokens": 47},
        ),
    )
    return cast(list[ChatCompletionChunk], [content, final])


def _request_bytes(kwargs: dict[str, Any]) -> bytes:
    return json.dumps(kwargs, sort_keys=True, separators=(",", ":"), default=str).encode()


async def _get_response(
    model: LitellmModel,
    *,
    system_instructions: str = "You are concise.",
    model_settings: ModelSettings | None = None,
):
    return await model.get_response(
        system_instructions=system_instructions,
        input=[{"role": "user", "content": "Return 42"}],
        model_settings=model_settings or ModelSettings(temperature=0, include_usage=True),
        tools=[_lookup_number],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )


async def _stream_response(model: LitellmModel) -> tuple[list[Any], list[tuple[int, int | None]]]:
    events: list[Any] = []
    telemetry_seen: list[tuple[int, int | None]] = []
    async for event in model.stream_response(
        system_instructions="You are concise.",
        input=[{"role": "user", "content": "Return 42"}],
        model_settings=ModelSettings(temperature=0, include_usage=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    ):
        events.append(event)
        telemetry = current_model_call_telemetry()
        if telemetry is not None:
            telemetry_seen.append((telemetry.model_call_index, telemetry.cache_write_tokens))
    return events, telemetry_seen


@pytest.mark.asyncio
async def test_model_adapter_attaches_exact_tools_for_nonstream_and_stream_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attached: list[list[Any]] = []

    async def fake_get_response(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(output=[])

    async def fake_stream_response(*_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        yield "done"

    monkeypatch.setattr(model_telemetry_module, "attach_tool_surface_to_pending_capture", attached.append)
    monkeypatch.setattr(LitellmModel, "get_response", fake_get_response)
    monkeypatch.setattr(LitellmModel, "stream_response", fake_stream_response)
    model = CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 1)

    await model.get_response(
        system_instructions="system",
        input=[{"role": "user", "content": "hello"}],
        model_settings=ModelSettings(),
        tools=[_lookup_number],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    streamed = [
        event
        async for event in model.stream_response(
            system_instructions="system",
            input=[{"role": "user", "content": "hello"}],
            model_settings=ModelSettings(),
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
    ]

    assert attached == [[_lookup_number], [_lookup_number]]
    assert streamed == ["done"]


@pytest.mark.asyncio
async def test_nonstream_capture_preserves_request_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[bytes] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        requests.append(_request_bytes(kwargs))
        return _completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    base_result = await _get_response(LitellmModel(model="openai/gpt-5.6"))
    adapter_result = await _get_response(CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 4))

    assert requests[0] == requests[1]
    assert adapter_result == base_result
    assert current_model_call_telemetry() is None


@pytest.mark.asyncio
async def test_direct_gpt56_adds_one_stable_prefix_breakpoint_without_changing_prompt_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_requests: list[dict[str, Any]] = []
    responses_requests: list[dict[str, Any]] = []
    telemetry_modes: list[tuple[str, int, int | None]] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        chat_requests.append(kwargs)
        telemetry = current_model_call_telemetry()
        assert telemetry is not None
        telemetry_modes.append(
            (
                telemetry.cache_mode,
                telemetry.cache_breakpoint_count,
                telemetry.cache_stable_prefix_chars,
            )
        )
        return _completion()

    async def fake_aresponses(**kwargs: Any) -> ResponsesAPIResponse:
        responses_requests.append(kwargs)
        telemetry = current_model_call_telemetry()
        assert telemetry is not None
        telemetry_modes.append(
            (
                telemetry.cache_mode,
                telemetry.cache_breakpoint_count,
                telemetry.cache_stable_prefix_chars,
            )
        )
        return _responses_completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    prompt = CacheableSystemInstructions(
        "stable instructions",
        "\ndynamic timestamp and policy",
        cache_namespace="wcc_test",
    )
    model_settings = ModelSettings(temperature=0, include_usage=True, extra_args=_GPT_REASONING)
    await _get_response(
        CopilotLitellmModel(model="gpt-5.6-sol", next_model_call_index=lambda: 1),
        system_instructions=str(prompt),
        model_settings=model_settings,
    )
    await _get_response(
        CopilotLitellmModel(model="gpt-5.6-sol", next_model_call_index=lambda: 2),
        system_instructions=prompt,
        model_settings=model_settings,
    )

    assert len(chat_requests) == len(responses_requests) == 1
    control_request = chat_requests[0]
    request = responses_requests[0]
    assert control_request["messages"][0] == {"content": str(prompt), "role": "system"}
    content_parts = request["input"][0]["content"]
    assert "".join(part["text"] for part in content_parts) == str(prompt)
    assert content_parts == [
        {
            "type": "input_text",
            "text": "stable instructions",
            "prompt_cache_breakpoint": {"mode": "explicit"},
        },
        {
            "type": "input_text",
            "text": "\ndynamic timestamp and policy",
        },
    ]
    assert request["input"][1]["content"][0]["text"] == control_request["messages"][1]["content"]
    assert request["tools"][0]["name"] == control_request["tools"][0]["function"]["name"]
    assert request["extra_body"]["prompt_cache_options"] == {"mode": "explicit"}
    assert request["prompt_cache_key"].startswith("copilot:")
    assert "messages" not in request["extra_body"]
    assert telemetry_modes == [
        ("implicit", 0, None),
        ("explicit", 2, len("stable instructions")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", ["anthropic/claude-sonnet-5-5", "bedrock/global.anthropic.claude-sonnet-5-5"])
@pytest.mark.parametrize("stream", [False, True])
async def test_cacheable_system_instructions_survive_provider_message_copy(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    stream: bool,
) -> None:
    prompt = CacheableSystemInstructions("stable instructions", "\ndynamic context", cache_namespace="wcc_test")
    copied_messages: list[list[dict[str, Any]]] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse | AsyncStream[ChatCompletionChunk]:
        copied_messages.append(copy.deepcopy(kwargs["messages"]))
        if kwargs.get("stream"):
            return cast(AsyncStream[ChatCompletionChunk], _ChunkStream(_stream_chunks()))
        return _completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    model = CopilotLitellmModel(model=model_name, next_model_call_index=lambda: 1)
    if stream:
        events = [
            event
            async for event in model.stream_response(
                system_instructions=prompt,
                input=[{"role": "user", "content": "Return 42"}],
                model_settings=ModelSettings(include_usage=True),
                tools=[],
                output_schema=None,
                handoffs=[],
                tracing=ModelTracing.DISABLED,
            )
        ]
        response = events[-1].response
    else:
        response = await _get_response(model, system_instructions=prompt)

    assert copied_messages[0][0] == {
        "role": "system",
        "content": [
            {"type": "text", "text": "stable instructions", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\ndynamic context"},
        ],
    }
    assert ItemHelpers.extract_last_text(response.output[-1]) == "42"


@pytest.mark.asyncio
async def test_stream_capture_preserves_events_order_and_backpressure(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[bytes] = []
    streams: list[_ChunkStream] = []

    async def fake_acompletion(**kwargs: Any) -> AsyncStream[ChatCompletionChunk]:
        requests.append(_request_bytes(kwargs))
        stream = _ChunkStream(_stream_chunks())
        streams.append(stream)
        return cast(AsyncStream[ChatCompletionChunk], stream)

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    monkeypatch.setattr("agents.extensions.models.litellm_model.time.time", lambda: 1.0)
    base_events, _ = await _stream_response(LitellmModel(model="openai/gpt-5.6"))
    adapter_events, telemetry_seen = await _stream_response(
        CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 9)
    )

    assert requests[0] == requests[1]
    assert adapter_events == base_events
    assert [event.type for event in adapter_events] == [event.type for event in base_events]
    assert telemetry_seen[-1] == (9, 47)
    assert streams[1].closed == streams[0].closed is False
    assert current_model_call_telemetry() is None


@pytest.mark.asyncio
async def test_explicit_responses_stream_captures_raw_cache_write_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def raw_stream():
        yield SimpleNamespace(type="response.completed", response=_responses_completion())

    async def fake_aresponses(**kwargs: Any):
        requests.append(kwargs)
        return raw_stream()

    def fake_response_iterator(streaming_response: Any, sync_stream: bool):
        assert sync_stream is False

        async def converted_stream():
            async for _ in streaming_response:
                pass
            chunks = _stream_chunks()
            chunks[-1].usage = Usage(
                prompt_tokens=100,
                completion_tokens=7,
                total_tokens=107,
                prompt_tokens_details={"cached_tokens": 31},
            )
            for chunk in chunks:
                yield chunk

        return converted_stream()

    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.model_telemetry.responses_api_bridge.transformation_handler.get_model_response_iterator",
        fake_response_iterator,
    )

    telemetry_seen: list[tuple[str, int | None, int | None]] = []
    model = CopilotLitellmModel(model="gpt-5.6-sol", next_model_call_index=lambda: 8)
    async for _ in model.stream_response(
        system_instructions=CacheableSystemInstructions(
            "stable instructions",
            "\ndynamic timestamp",
            cache_namespace="wcc-test",
        ),
        input=[{"role": "user", "content": "Return 42"}],
        model_settings=ModelSettings(include_usage=True, extra_args=_GPT_REASONING),
        tools=[_lookup_number],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    ):
        telemetry = current_model_call_telemetry()
        assert telemetry is not None
        telemetry_seen.append(
            (
                telemetry.cache_mode,
                telemetry.cache_read_tokens,
                telemetry.cache_write_tokens,
            )
        )

    assert requests[0]["extra_body"] == {"prompt_cache_options": {"mode": "explicit"}}
    assert requests[0]["prompt_cache_key"].startswith("copilot:")
    assert telemetry_seen[-1] == ("explicit", 31, 47)
    assert current_model_call_telemetry() is None


@pytest.mark.asyncio
async def test_missing_usage_is_nonfatal_and_context_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _completion()
    response.usage = None

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        return response

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    result = await _get_response(CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 1))

    assert result.output
    assert current_model_call_telemetry() is None


@pytest.mark.asyncio
async def test_attempt_telemetry_captures_stop_metadata_and_missing_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _completion()
    response.usage = None

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        return response

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    with model_attempt_telemetry_scope() as attempt:
        await _get_response(CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 7))

    assert attempt.latest_stop_metadata.model_call_index == 7
    assert attempt.latest_stop_metadata.finish_reason == "stop"
    assert attempt.latest_stop_metadata.incomplete_details is None
    assert attempt.latest_stop_metadata.refusal is False
    assert attempt.latest_stop_metadata.content_filter is False
    assert attempt.latest_stop_metadata.usage_missing is True
    assert current_model_attempt_telemetry() is None


def test_responses_stop_metadata_captures_incomplete_filter_and_refusal() -> None:
    response = _responses_completion()
    response.status = "incomplete"
    response.incomplete_details = {"reason": "content_filter"}
    response.output = [
        {
            "id": "msg-refusal",
            "type": "message",
            "role": "assistant",
            "status": "incomplete",
            "content": [{"type": "refusal", "refusal": "not available"}],
        }
    ]
    telemetry = model_telemetry_module.CopilotModelCallTelemetry(model_call_index=8)

    model_telemetry_module._capture_responses_stop_metadata(telemetry, response)

    assert telemetry.incomplete_details == {"reason": "content_filter"}
    assert telemetry.content_filter is True
    assert telemetry.refusal is True


def test_chat_stop_metadata_captures_direct_refusal() -> None:
    telemetry = model_telemetry_module.CopilotModelCallTelemetry(model_call_index=9)

    model_telemetry_module._capture_chat_stop_metadata(
        telemetry,
        {"choices": [{"finish_reason": "stop", "message": {"refusal": "not available"}}]},
    )

    assert telemetry.finish_reason == "stop"
    assert telemetry.content_filter is False
    assert telemetry.refusal is True


def test_attempt_telemetry_keeps_only_the_latest_model_call() -> None:
    with model_attempt_telemetry_scope() as attempt:
        with _model_call_telemetry_scope(1) as first:
            first.finish_reason = "length"
        with _model_call_telemetry_scope(2) as second:
            second.finish_reason = "content_filter"
            second.content_filter = True

    assert attempt.latest_stop_metadata.model_call_index == 2
    assert attempt.latest_stop_metadata.finish_reason == "content_filter"
    assert attempt.latest_stop_metadata.content_filter is True


@pytest.mark.asyncio
async def test_concurrent_attempt_stop_metadata_is_isolated() -> None:
    release = asyncio.Event()

    async def observe(index: int, reason: str) -> tuple[int, str | None]:
        with model_attempt_telemetry_scope() as attempt, _model_call_telemetry_scope(index) as call:
            call.finish_reason = reason
            await release.wait()
        return attempt.latest_stop_metadata.model_call_index, attempt.latest_stop_metadata.finish_reason

    first = asyncio.create_task(observe(11, "stop"))
    second = asyncio.create_task(observe(22, "length"))
    release.set()

    assert sorted(await asyncio.gather(first, second)) == [(11, "stop"), (22, "length")]


@pytest.mark.asyncio
async def test_model_error_resets_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        raise RuntimeError("provider failed")

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    with pytest.raises(RuntimeError, match="provider failed"):
        await _get_response(CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 1))

    assert current_model_call_telemetry() is None


def test_nested_model_call_scopes_restore_outer_call() -> None:
    with _model_call_telemetry_scope(1) as outer:
        assert current_model_call_telemetry() is outer
        with _model_call_telemetry_scope(2) as inner:
            assert current_model_call_telemetry() is inner
        assert current_model_call_telemetry() is outer

    assert current_model_call_telemetry() is None


def test_model_call_cost_uses_runtime_litellm_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_cost_per_token(**kwargs: Any) -> tuple[float, float]:
        captured.update(kwargs)
        return 0.12, 0.03

    monkeypatch.setattr(model_telemetry_module.litellm, "cost_per_token", fake_cost_per_token)
    telemetry = model_telemetry_module.CopilotModelCallTelemetry(
        model_call_index=1,
        input_tokens=100,
        output_tokens=7,
        cache_read_tokens=31,
        cache_write_tokens=47,
    )

    assert model_telemetry_module._model_call_cost(telemetry, "gpt-5.6-sol") == pytest.approx(0.15)
    assert captured == {
        "model": "gpt-5.6-sol",
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "cache_read_input_tokens": 31,
        "cache_creation_input_tokens": 47,
        "call_type": "aresponses",
    }


def test_model_call_cost_normalizes_dated_gpt56_response_model(monkeypatch: pytest.MonkeyPatch) -> None:
    priced_models: list[str] = []

    def fake_cost_per_token(**kwargs: Any) -> tuple[float, float]:
        priced_models.append(kwargs["model"])
        return 0.25, 0.125

    monkeypatch.setattr(model_telemetry_module.litellm, "cost_per_token", fake_cost_per_token)
    telemetry = model_telemetry_module.CopilotModelCallTelemetry(
        model_call_index=1,
        input_tokens=40_000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=35_000,
    )

    dated_cost = model_telemetry_module._model_call_cost(
        telemetry,
        "gpt-5.6-sol-2026-07-09",
    )
    base_cost = model_telemetry_module._model_call_cost(telemetry, "gpt-5.6-sol")

    assert dated_cost is not None
    assert dated_cost == pytest.approx(base_cost)
    assert priced_models == ["gpt-5.6-sol", "gpt-5.6-sol"]


def test_completed_model_call_emits_datadog_usage_with_explicit_zeroes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        model_telemetry_module.LOG,
        "info",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(model_telemetry_module, "_model_call_cost", lambda telemetry, model: 0.125)

    with _model_call_telemetry_scope(3, model="gpt-5.6-sol") as telemetry:
        telemetry.cache_mode = "explicit"
        telemetry.cache_breakpoint_count = 1
        telemetry.cache_stable_prefix_chars = 118_024
        telemetry.input_tokens = 40_000
        telemetry.output_tokens = 500
        telemetry.cache_read_tokens = 0
        telemetry.cache_write_tokens = 35_000

    assert events == [
        (
            "Copilot model usage",
            {
                "log_code": "copilot_model_usage",
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-5.6-sol",
                "copilot.model_call_index": 3,
                "copilot.cache.mode": "explicit",
                "copilot.cache.breakpoint_count": 1,
                "copilot.cache.stable_prefix_chars": 118_024,
                "gen_ai.usage.input_tokens": 40_000,
                "gen_ai.usage.output_tokens": 500,
                "gen_ai.usage.cache_read.input_tokens": 0,
                "gen_ai.usage.cache_creation.input_tokens": 35_000,
                "operation.cost": 0.125,
                "gen_ai.provider.name": "openai",
            },
        )
    ]


def test_datadog_usage_preserves_missing_cache_write_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        model_telemetry_module.LOG,
        "info",
        lambda _event, **fields: events.append(fields),
    )
    monkeypatch.setattr(model_telemetry_module, "_model_call_cost", lambda telemetry, model: None)

    with _model_call_telemetry_scope(
        4,
        model="azure/gpt-5.6-sol",
        base_url="https://example.openai.azure.com",
    ) as telemetry:
        telemetry.input_tokens = 100
        telemetry.output_tokens = 5
        telemetry.cache_read_tokens = 0

    assert events[0]["gen_ai.provider.name"] == "azure.ai.openai"
    assert events[0]["gen_ai.usage.cache_read.input_tokens"] == 0
    assert "gen_ai.usage.cache_creation.input_tokens" not in events[0]
    assert "operation.cost" not in events[0]


def test_datadog_usage_attributes_fallback_spend_to_response_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    priced_models: list[str] = []
    monkeypatch.setattr(
        model_telemetry_module.LOG,
        "info",
        lambda _event, **fields: events.append(fields),
    )
    monkeypatch.setattr(
        model_telemetry_module,
        "_model_call_cost",
        lambda telemetry, model: priced_models.append(model) or 0.25,
    )

    with _model_call_telemetry_scope(
        5,
        model="azure/gpt-5.6-sol",
        base_url="https://example.openai.azure.com",
    ) as telemetry:
        telemetry.response_model = "anthropic/claude-sonnet-4-6"
        telemetry.input_tokens = 100
        telemetry.output_tokens = 5

    assert priced_models == ["anthropic/claude-sonnet-4-6"]
    assert events[0]["gen_ai.request.model"] == "azure/gpt-5.6-sol"
    assert events[0]["gen_ai.response.model"] == "anthropic/claude-sonnet-4-6"
    assert events[0]["gen_ai.provider.name"] == "anthropic"


@pytest.mark.asyncio
async def test_datadog_usage_names_the_provider_that_served_an_in_call_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(model_telemetry_module.LOG, "info", lambda _event, **fields: events.append(fields))
    monkeypatch.setattr(model_telemetry_module, "_model_call_cost", lambda telemetry, model: None)
    chunk = ModelResponseStream(model="gpt-5.6-terra")
    chunk._hidden_params = {"custom_llm_provider": "openai"}

    async def served_by_openai() -> AsyncIterator[ModelResponseStream]:
        yield chunk

    with _model_call_telemetry_scope(
        6,
        model="azure/gpt-5.6-terra",
        base_url="https://example.openai.azure.com",
    ) as telemetry:
        await anext(model_telemetry_module._UsageCapturingStream(served_by_openai(), telemetry))  # type: ignore[arg-type]
        telemetry.input_tokens = 100
        telemetry.output_tokens = 5

    assert events[0]["gen_ai.request.model"] == "azure/gpt-5.6-terra"
    assert events[0]["gen_ai.provider.name"] == "openai"


def test_model_call_without_provider_usage_does_not_emit_datadog_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        model_telemetry_module.LOG,
        "info",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    with _model_call_telemetry_scope(5, model="gpt-5.6-sol"):
        pass

    assert events == []


def test_datadog_logging_failure_does_not_escape_or_leak_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_log(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("logger unavailable")

    monkeypatch.setattr(model_telemetry_module.LOG, "info", fail_to_log)

    with _model_call_telemetry_scope(6, model="gpt-5.6-sol") as telemetry:
        telemetry.input_tokens = 100
        telemetry.output_tokens = 5

    assert current_model_call_telemetry() is None


@pytest.mark.asyncio
async def test_concurrent_calls_keep_usage_and_indices_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    release = {11: asyncio.Event(), 22: asyncio.Event()}
    observed: dict[int, tuple[int, int]] = {}

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        prompt = kwargs["messages"][-1]["content"]
        prompt_number = int(prompt)
        before = current_model_call_telemetry()
        assert before is not None
        await release[prompt_number].wait()
        after = current_model_call_telemetry()
        assert after is before
        observed[prompt_number] = (before.model_call_index, after.model_call_index)
        response = _completion()
        response.usage.prompt_tokens_details.cache_write_tokens = prompt_number
        return response

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    indices = iter([1, 2])
    model = CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: next(indices))

    async def run(prompt_number: int) -> tuple[int, int | None]:
        task = model.get_response(
            system_instructions=None,
            input=[{"role": "user", "content": str(prompt_number)}],
            model_settings=ModelSettings(include_usage=True),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )
        result = await task
        assert result.output
        telemetry = current_model_call_telemetry()
        assert telemetry is None
        return prompt_number, result.usage.input_tokens

    first = asyncio.create_task(run(11))
    second = asyncio.create_task(run(22))
    release[22].set()
    release[11].set()

    assert sorted(await asyncio.gather(first, second)) == [(11, 100), (22, 100)]
    assert observed == {11: (1, 1), 22: (2, 2)}


@pytest.mark.asyncio
async def test_stream_cancellation_resets_context_without_changing_stream_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _ChunkStream([_stream_chunks()[0], _stream_chunks()[0]])

    async def fake_acompletion(**kwargs: Any) -> AsyncStream[ChatCompletionChunk]:
        return cast(AsyncStream[ChatCompletionChunk], stream)

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    events = CopilotLitellmModel(
        model="openai/gpt-5.6",
        next_model_call_index=lambda: 3,
    ).stream_response(
        system_instructions=None,
        input="hello",
        model_settings=ModelSettings(include_usage=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )

    await anext(events)
    await events.aclose()

    assert current_model_call_telemetry() is None
    assert stream.closed is False


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.openai.azure.com",
        "https://example.openai.azure.com/openai/deployments/x",
        "https://OPENAI.AZURE.COM",
    ],
)
def test_otel_provider_name_detects_azure_hosts(base_url: str) -> None:
    assert model_telemetry_module._otel_provider_name("gpt-5.6-sol", base_url) == "azure.ai.openai"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.test/?redirect=.openai.azure.com",
        "https://openai.azure.com.evil.test",
        "https://evil.test/.openai.azure.com",
        "https://notopenai.azure.com.attacker.test/v1",
    ],
)
def test_otel_provider_name_rejects_lookalike_azure_urls(base_url: str) -> None:
    # A bare substring check labelled all of these as Azure; the host check must not.
    assert model_telemetry_module._otel_provider_name("some-model", base_url) is None


def test_model_call_scope_names_the_open_operation_and_retires_it_on_exit() -> None:
    install_pending_operation_slot()

    with _model_call_telemetry_scope(0, model="gpt-5.6-sol"):
        while_open = pending_operation_fields()

    after_exit = pending_operation_fields()

    assert while_open["pending_operation"] == "model.call:gpt-5.6-sol"
    assert isinstance(while_open["pending_operation_started_monotonic"], float)
    assert while_open["pending_operation_state"] == "open"
    assert while_open["pending_operation_open_count"] == 1
    assert after_exit["pending_operation"] == "model.call:gpt-5.6-sol"
    assert after_exit["pending_operation_state"] == "returned"
    assert after_exit["pending_operation_open_count"] == 0


def test_the_innermost_scope_wins_over_an_outer_one_that_is_still_open() -> None:
    install_pending_operation_slot()

    outer = pending_operation("turn.stream", span=True)
    inner = pending_operation("mcp.call_tool:run_block")
    outer.__enter__()
    inner.__enter__()
    while_both_open = pending_operation_fields()
    inner.__exit__(None, None, None)
    after_inner_returned = pending_operation_fields()
    outer.__exit__(None, None, None)

    assert while_both_open["pending_operation"] == "mcp.call_tool:run_block"
    assert after_inner_returned["pending_operation"] == "mcp.call_tool:run_block"
    assert after_inner_returned["pending_operation_state"] == "returned"
    assert after_inner_returned["pending_operation_open_count"] == 1


def test_a_scope_that_exits_by_exception_is_never_reported_as_still_open() -> None:
    install_pending_operation_slot()

    with pytest.raises(RuntimeError):
        with pending_operation("mcp.call_tool:run_block"):
            raise RuntimeError("tool blew up")

    fields = pending_operation_fields()
    assert fields["pending_operation"] == "mcp.call_tool:run_block"
    assert fields["pending_operation_state"] == "unwound_by_error"
    assert fields["pending_operation_open_count"] == 0


def test_a_scope_slower_than_the_threshold_logs_itself_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.pending_operation.PENDING_OPERATION_LOG_THRESHOLD_SECONDS",
        0.0,
    )
    install_pending_operation_slot()

    with capture_logs() as logs:
        with pending_operation("mcp.call_tool:run_block"):
            pass

    slow = [entry for entry in logs if entry.get("event") == "copilot_pending_operation_slow"]
    assert len(slow) == 1
    assert slow[0]["pending_operation"] == "mcp.call_tool:run_block"
    assert isinstance(slow[0]["pending_operation_started_monotonic"], float)


def test_a_fingerprint_carries_the_turn_identifiers_that_join_it_to_its_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.pending_operation.PENDING_OPERATION_LOG_THRESHOLD_SECONDS",
        0.0,
    )
    install_pending_operation_slot(
        SimpleNamespace(
            workflow_permanent_id="wpid_1",
            turn_id="turn_1",
            workflow_copilot_chat_id="wcc_1",
        )
    )

    with capture_logs() as logs:
        with pending_operation("mcp.call_tool:run_block"):
            fields = pending_operation_fields()

    slow = [entry for entry in logs if entry.get("event") == "copilot_pending_operation_slow"]
    assert len(slow) == 1
    for key, value in (
        ("workflow_permanent_id", "wpid_1"),
        ("turn_id", "turn_1"),
        ("workflow_copilot_chat_id", "wcc_1"),
    ):
        assert slow[0][key] == value
        assert fields[key] == value


def test_a_context_carrying_no_real_identifiers_adds_no_correlation_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.pending_operation.PENDING_OPERATION_LOG_THRESHOLD_SECONDS",
        0.0,
    )
    install_pending_operation_slot(cast(Any, MagicMock()))

    with pending_operation("mcp.call_tool:run_block"):
        fields = pending_operation_fields()

    assert not {"workflow_permanent_id", "turn_id", "workflow_copilot_chat_id"} & set(fields)


def test_an_early_tool_error_does_not_pin_the_fingerprint_for_the_rest_of_the_turn() -> None:
    install_pending_operation_slot()

    # mcp_adapter raises for code-block control flow, so an ordinary turn retires an errored scope
    # early. It must not outrank everything that runs afterwards.
    with contextlib.suppress(RuntimeError):
        with pending_operation("mcp.call_tool:codeblock_control_flow"):
            raise RuntimeError("control flow")
    with pending_operation("mcp.call_tool:later"):
        pass

    fields = pending_operation_fields()
    assert fields["pending_operation"] == "mcp.call_tool:later"
    assert fields["pending_operation_state"] == "returned"


def test_a_sibling_still_hanging_outranks_a_later_one_that_already_returned() -> None:
    install_pending_operation_slot()
    turn = pending_operation("turn.stream", span=True)
    turn.__enter__()

    hung = pending_operation("mcp.call_tool:hung")
    hung.__enter__()
    # The SDK cancels concurrent tool tasks without awaiting cleanup, so a later-started sibling can
    # retire while the one that hung is still open.
    with pending_operation("mcp.call_tool:fast"):
        pass

    fields = pending_operation_fields()
    hung.__exit__(None, None, None)
    turn.__exit__(None, None, None)

    assert fields["pending_operation"] == "mcp.call_tool:hung"
    assert fields["pending_operation_state"] == "open"


@pytest.mark.asyncio
async def test_a_scope_abandoned_by_an_unfinalised_generator_does_not_own_the_next_iteration() -> None:
    install_pending_operation_slot()

    async def abandoned() -> AsyncIterator[int]:
        with pending_operation("model.call:terra"):
            for value in range(100):
                yield value

    iteration_one = pending_operation("turn.stream", span=True)
    iteration_one.__enter__()
    generator = abandoned()
    async for _ in generator:
        break
    del generator
    iteration_one.__exit__(None, None, None)

    with pending_operation("turn.stream", span=True):
        with pending_operation("mcp.call_tool:later"):
            pass
        fields = pending_operation_fields()

    assert fields["pending_operation"] == "mcp.call_tool:later"
    assert fields["pending_operation_state"] == "returned"


def test_a_finished_scope_reports_its_own_duration_not_the_time_since_it_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.pending_operation.time.monotonic",
        lambda: clock["now"],
    )
    install_pending_operation_slot()

    with pending_operation("model.call:terra"):
        clock["now"] = 1002.0
    clock["now"] = 1900.0  # the turn then stalls for ~15 min somewhere uninstrumented

    fields = pending_operation_fields()
    assert fields["pending_operation"] == "model.call:terra"
    assert fields["pending_operation_state"] == "returned"
    assert fields["pending_operation_seconds"] == 2.0, "a 2s call must not read as though it hung for 900s"


def test_a_scope_spanning_the_whole_iteration_does_not_emit_the_slow_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.pending_operation.PENDING_OPERATION_LOG_THRESHOLD_SECONDS",
        0.0,
    )
    install_pending_operation_slot()

    with capture_logs() as logs:
        with pending_operation("turn.stream", span=True):
            pass

    assert [entry for entry in logs if entry.get("event") == "copilot_pending_operation_slow"] == []


@pytest.mark.asyncio
async def test_the_result_names_the_fallback_model_that_actually_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRateLimitError(Exception):
        pass

    FakeRateLimitError.__module__ = "openai"

    class FakeMCPServerManager:
        def __init__(self, servers: object) -> None:
            self.active_servers = servers

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def fake_resolve_model_config(
        _handler: object, *, copilot_config: object = None, llm_key_override: str | None = None
    ) -> tuple[str, object, str, bool]:
        del copilot_config
        key = llm_key_override or "PRIMARY"
        return f"model-{key}", object(), key, True

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("agents.mcp.MCPServerManager", FakeMCPServerManager)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.model_resolver.resolve_model_config", fake_resolve_model_config)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.run_with_enforcement",
        AsyncMock(
            side_effect=[
                FakeRateLimitError("rate limit"),
                SimpleNamespace(final_output=json.dumps({"type": "REPLY", "user_response": "ok"}), new_items=[]),
            ]
        ),
    )

    result = await copilot_agent_module.run_copilot_agent(
        stream=MagicMock(),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="build it",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_run_id=None,
            workflow_yaml="",
            browser_session_id=None,
            product_action=None,
            selected_connected_account_id=None,
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(fallback_llm_key="SECONDARY"),
    )

    assert result.resolved_model == "model-SECONDARY"
    assert result.resolved_model != "model-PRIMARY"


@pytest.mark.asyncio
async def test_model_setup_failure_does_not_claim_the_unstarted_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingMCPServerManager:
        def __init__(self, _servers: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            raise RuntimeError("setup failed")

        async def __aexit__(self, *args: object) -> None:
            return None

    def fake_resolve_model_config(
        _handler: object, *, copilot_config: object = None, llm_key_override: str | None = None
    ) -> tuple[str, object, str, bool]:
        del copilot_config, llm_key_override
        return "model-PRIMARY", object(), "PRIMARY", True

    enforcement = AsyncMock()
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("agents.mcp.MCPServerManager", FailingMCPServerManager)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.model_resolver.resolve_model_config", fake_resolve_model_config)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.enforcement.run_with_enforcement", enforcement)

    result = await copilot_agent_module.run_copilot_agent(
        stream=MagicMock(send=AsyncMock(return_value=True)),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="build it",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_run_id=None,
            workflow_yaml="",
            browser_session_id=None,
            product_action=None,
            selected_connected_account_id=None,
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(),
    )

    assert result.resolved_model is None
    enforcement.assert_not_awaited()


@pytest.mark.parametrize(
    ("fallback_enabled", "expected_model"),
    [
        pytest.param(False, "model-PRIMARY", id="primary"),
        pytest.param(True, "model-SECONDARY", id="fallback"),
    ],
)
@pytest.mark.asyncio
async def test_browser_ablation_timeout_reports_active_model_and_browser_work(
    monkeypatch: pytest.MonkeyPatch,
    fallback_enabled: bool,
    expected_model: str,
) -> None:
    class FakeRateLimitError(Exception):
        pass

    FakeRateLimitError.__module__ = "openai"

    class FakeMCPServerManager:
        def __init__(self, servers: object) -> None:
            self.active_servers = servers

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def fake_resolve_model_config(
        _handler: object, *, copilot_config: object = None, llm_key_override: str | None = None
    ) -> tuple[str, object, str, bool]:
        del copilot_config
        key = llm_key_override or "PRIMARY"
        return f"model-{key}", object(), key, True

    attempt_count = 0

    async def timeout_after_browser_activity(**kwargs: object) -> None:
        nonlocal attempt_count
        attempt_count += 1
        if fallback_enabled and attempt_count == 1:
            raise FakeRateLimitError("rate limit")
        ctx = cast(copilot_agent_module.CopilotContext, kwargs["ctx"])
        ctx.copilot_total_timeout_exceeded = True
        ctx.eval_tool_activity = [{"tool_name": "navigate_browser", "success": True}]
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.agent._resolve_live_browser_session_id", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("agents.mcp.MCPServerManager", FakeMCPServerManager)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.model_resolver.resolve_model_config", fake_resolve_model_config)
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.mcp_adapter.SkyvernOverlayMCPServer.list_tools",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.copilot.enforcement.run_with_enforcement",
        AsyncMock(side_effect=timeout_after_browser_activity),
    )

    result = await copilot_agent_module.run_copilot_agent(
        stream=MagicMock(send=AsyncMock(return_value=True)),
        organization_id="org-1",
        chat_request=WorkflowCopilotChatRequest(
            message="research it",
            workflow_id="wf-1",
            workflow_permanent_id="wfp-1",
            workflow_copilot_chat_id="chat-1",
            workflow_run_id=None,
            workflow_yaml="",
            browser_session_id=None,
            product_action=None,
            selected_connected_account_id=None,
        ),
        chat_history=[],
        global_llm_context=None,
        llm_api_handler=SimpleNamespace(llm_key="PRIMARY"),
        raw_secret_safety_handler=AsyncMock(
            return_value={"version": "1", "state": "clean", "handling": "none", "citations": []}
        ),
        api_key="sk-test",
        config=CopilotConfig(fallback_llm_key="SECONDARY" if fallback_enabled else None),
        eval_mode=CopilotEvalMode.BROWSER_ABLATION,
    )

    assert result.resolved_model == expected_model
    assert result.user_response == ""
    assert result.turn_outcome is not None
    assert result.turn_outcome.budget_expired is True
    assert result.turn_outcome.budget_expiry_report_produced is False
    assert result.narrative_payload is not None
    assert result.narrative_payload["turnFacts"]["terminalCause"] == "deadline_expired"
    assert result.browser_ablation_metadata is not None
    assert result.browser_ablation_metadata["eval_mode"] == "browser_ablation"
    assert result.browser_ablation_metadata["tool_activity"] == [{"tool_name": "navigate_browser", "success": True}]


_GEMINI = "vertex_ai/gemini-2.5-flash"
_SCHEMA_CALL = ResponseFunctionToolCall(
    type="function_call",
    call_id="call_schema",
    name="get_block_schema",
    arguments='{"block_type": "code"}',
)
_REF_RESULT = json.dumps({"ok": True, "data": {"schema": {"$ref": "#/$defs/Step", "$defs": {"Step": {}}}}})
_ESCAPED_REF_KEY_RESULT = '{"ok": true, "data": {"schema": {"\\u0024ref": "#/$defs/Step", "$defs": {"Step": {}}}}}'


def _tool_turn(output: object) -> list[TResponseInputItem]:
    return [
        {"role": "user", "content": "Add a block"},
        cast(TResponseInputItem, _SCHEMA_CALL.model_dump()),
        ItemHelpers.tool_call_output_item(_SCHEMA_CALL, output),
    ]


async def _block_schema_tool_output(block_type: str) -> tuple[object, str]:
    call_result = _copilot_to_call_tool_result(await skyvern_block_schema(block_type), "get_block_schema")
    server = SimpleNamespace(
        name="skyvern", use_structured_content=False, call_tool=AsyncMock(return_value=call_result)
    )
    output = await MCPUtil.invoke_mcp_tool(
        cast(MCPServer, server),
        MCPTool(name="get_block_schema", inputSchema={"type": "object"}),
        RunContextWrapper(None),
        _SCHEMA_CALL.arguments,
    )
    return output, cast(Any, call_result.content[0]).text


async def _sent_request(
    monkeypatch: pytest.MonkeyPatch,
    model: LitellmModel,
    turn: list[TResponseInputItem],
    extra_args: dict[str, Any] | None,
    stream: bool = False,
    system_instructions: str = "You are concise.",
    model_settings: ModelSettings | None = None,
) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse | AsyncStream[ChatCompletionChunk]:
        requests.append(kwargs)
        return cast(AsyncStream[ChatCompletionChunk], _ChunkStream(_stream_chunks())) if stream else _completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    call = {
        "system_instructions": system_instructions,
        "input": turn,
        "model_settings": model_settings or ModelSettings(extra_args=extra_args),
        "tools": [],
        "output_schema": None,
        "handoffs": [],
        "tracing": ModelTracing.DISABLED,
    }
    if stream:
        async for _ in model.stream_response(**call):
            pass
    else:
        await model.get_response(**call)
    return requests[0]


async def _sent_messages(
    monkeypatch: pytest.MonkeyPatch,
    model: LitellmModel,
    turn: list[TResponseInputItem],
    extra_args: dict[str, Any] | None,
    stream: bool = False,
) -> list[dict[str, Any]]:
    return (await _sent_request(monkeypatch, model, turn, extra_args, stream))["messages"]


_SONNET_ROUTES = ["anthropic/claude-sonnet-5-5", "bedrock/global.anthropic.claude-sonnet-5-5"]
_CACHED_PROMPT = CacheableSystemInstructions("stable instructions", "\ndynamic timestamp", cache_namespace="wcc_test")
_FRAME: TResponseInputItem = {
    "role": "user",
    "content": [
        {"type": "input_text", "text": SCREENSHOT_SENTINEL + "Frame"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "low"},
    ],
}
_NUDGE: TResponseInputItem = {"role": "user", "content": NUDGE_SENTINEL + "Keep going."}


def _provider_messages(model_name: str, request: dict[str, Any]) -> list[dict[str, Any]]:
    _, messages, _ = AnthropicCacheControlHook().get_chat_completion_prompt(
        model=model_name,
        messages=request["messages"],
        non_default_params={"cache_control_injection_points": request.get("cache_control_injection_points")},
        prompt_id=None,
        prompt_variables=None,
        dynamic_callback_params={},
    )
    return messages


def _marked_indices(messages: list[dict[str, Any]]) -> list[int]:
    return [index for index, message in enumerate(messages) if '"cache_control"' in json.dumps(message)]


def _cache_usage(logs: list[dict[str, Any]]) -> tuple[str, int, int | None]:
    (usage,) = (entry for entry in logs if entry.get("log_code") == "copilot_model_usage")
    return (
        usage["copilot.cache.mode"],
        usage["copilot.cache.breakpoint_count"],
        usage.get("copilot.cache.stable_prefix_chars"),
    )


def _tool_round(call_id: str) -> list[TResponseInputItem]:
    return [
        {"type": "function_call", "call_id": call_id, "name": "evaluate", "arguments": json.dumps({"code": "x" * 400})},
        {"type": "function_call_output", "call_id": call_id, "output": json.dumps({"ok": True, "text": "y" * 400})},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", _SONNET_ROUTES)
@pytest.mark.parametrize(
    ("turn", "marked", "breakpoints"),
    [
        (_tool_turn("tool result"), [0, 1], 2),
        (_tool_turn("tool result") + [_FRAME], [0, 1], 2),
        (_tool_turn("tool result") + [_FRAME, _NUDGE], [0, 1], 2),
        ([_FRAME], [0], 1),
    ],
)
async def test_anthropic_route_marks_stable_system_prefix_and_the_stable_anchor(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    turn: list[TResponseInputItem],
    marked: list[int],
    breakpoints: int,
) -> None:
    model_settings = ModelSettings(extra_args={"fallbacks": ["bedrock/global.anthropic.claude-sonnet-5-5"]})
    with capture_logs() as logs:
        request = await _sent_request(
            monkeypatch,
            CopilotLitellmModel(model=model_name, next_model_call_index=lambda: 1),
            turn,
            None,
            system_instructions=_CACHED_PROMPT,
            model_settings=model_settings,
        )

    stable_block, dynamic_block = request["messages"][0]["content"]
    assert stable_block["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in dynamic_block
    assert stable_block["text"] + dynamic_block["text"] == str(_CACHED_PROMPT)
    assert _marked_indices(_provider_messages(model_name, request)) == marked
    assert _cache_usage(logs) == ("explicit", breakpoints, len("stable instructions"))
    assert model_settings.extra_args == {"fallbacks": ["bedrock/global.anthropic.claude-sonnet-5-5"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", _SONNET_ROUTES)
@pytest.mark.parametrize(
    "next_call_input",
    [copilot_session_input_callback, lambda history, new_items: _prune_input_list([*history, *new_items])],
    ids=["session", "no_session"],
)
@pytest.mark.parametrize(
    ("mid_history", "anchor_call_id"),
    [([_FRAME, _NUDGE], "call_1"), ([_NUDGE], "call_2")],
    ids=["frame_then_nudge", "nudge"],
)
async def test_anthropic_rolling_marker_prefix_is_resent_unchanged_on_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    next_call_input: Callable[[list[TResponseInputItem], list[TResponseInputItem]], list[TResponseInputItem]],
    mid_history: list[TResponseInputItem],
    anchor_call_id: str,
) -> None:
    (call_0a, output_0a), (call_0b, output_0b) = _tool_round("call_0a"), _tool_round("call_0b")
    history: list[TResponseInputItem] = [
        {"role": "user", "content": "Build the workflow"},
        {
            "type": "message",
            "role": "assistant",
            "id": "msg_0",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Checking the page.", "annotations": []}],
        },
        call_0a,
        call_0b,
        output_0a,
        output_0b,
        *_tool_round("call_1"),
        *mid_history,
        *_tool_round("call_2"),
        *_tool_round("call_3"),
        *_tool_round("call_4"),
        *_tool_round("call_5"),
    ]
    requests = []
    for turn in (
        next_call_input(history, []),
        next_call_input([*history, *_tool_round("call_6")], [_FRAME, _NUDGE]),
    ):
        requests.append(
            await _sent_request(
                monkeypatch,
                CopilotLitellmModel(model=model_name, next_model_call_index=lambda: 1),
                turn,
                None,
                system_instructions=_CACHED_PROMPT,
            )
        )

    provider_messages = _provider_messages(model_name, requests[0])
    rolling = _marked_indices(provider_messages)[-1]
    assert requests[0]["messages"][: rolling + 1] == requests[1]["messages"][: rolling + 1]
    assert provider_messages[rolling]["tool_call_id"] == anchor_call_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "extra_args"),
    [
        ("openai/gpt-5.5", None),
        ("anthropic/claude-sonnet-5-5", {"fallbacks": ["openai/gpt-5.5"]}),
        ("anthropic/claude-sonnet-5-5", {"cache_control_injection_points": [{"location": "message", "role": "user"}]}),
    ],
)
async def test_anthropic_cache_breakpoints_opt_out_off_an_all_anthropic_chain_or_with_caller_points(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    extra_args: dict[str, Any] | None,
) -> None:
    with capture_logs() as logs:
        request = await _sent_request(
            monkeypatch,
            CopilotLitellmModel(model=model_name, next_model_call_index=lambda: 1),
            _tool_turn("tool result"),
            extra_args,
            system_instructions=_CACHED_PROMPT,
        )

    assert request["messages"][0] == {"role": "system", "content": str(_CACHED_PROMPT)}
    assert request.get("cache_control_injection_points") == (extra_args or {}).get("cache_control_injection_points")
    assert _cache_usage(logs)[:2] == ("implicit", 0)


def _tool_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return next(message for message in messages if message["role"] == "tool")


def _tool_text(messages: list[dict[str, Any]]) -> str:
    content = _tool_message(messages)["content"]
    return content if isinstance(content, str) else "".join(part["text"] for part in content)


def _gemini_function_responses(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contents = _gemini_convert_messages_with_history(
        messages=cast(Any, [message for message in messages if message["role"] != "system"]),
        model="gemini-2.5-flash",
        custom_llm_provider="vertex_ai",
    )
    return [
        part["function_response"] for content in contents for part in content["parts"] if "function_response" in part
    ]


def _json_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_json_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_json_keys(child) for child in value))
    return set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "model_name", "extra_args", "stream"),
    [
        ("code", _GEMINI, None, False),
        ("code", "openai/gpt-5.6", {"fallbacks": [_GEMINI]}, True),
        ("for_loop", _GEMINI, None, True),
        ("for_loop", "azure/gpt-5.6-sol", {"fallbacks": [{"model": _GEMINI, "api_key": None}]}, False),
        ("escaped_ref_key", _GEMINI, None, False),
    ],
)
async def test_gemini_on_the_model_chain_receives_ref_tool_results_as_literal_text(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    model_name: str,
    extra_args: dict[str, Any] | None,
    stream: bool,
) -> None:
    if payload == "escaped_ref_key":
        output, text = _ESCAPED_REF_KEY_RESULT, _ESCAPED_REF_KEY_RESULT
    else:
        output, text = await _block_schema_tool_output(payload)
    turn = _tool_turn(output)
    original_turn = copy.deepcopy(turn)

    raw = await _sent_messages(monkeypatch, LitellmModel(model=model_name), turn, extra_args)
    usage_events: list[dict[str, Any]] = []
    monkeypatch.setattr(model_telemetry_module.LOG, "info", lambda event, **fields: usage_events.append(fields))
    sent = await _sent_messages(
        monkeypatch, CopilotLitellmModel(model=model_name, next_model_call_index=lambda: 1), turn, extra_args, stream
    )

    assert [event["copilot.ref_tool_outputs_escaped"] for event in usage_events] == [1]
    assert _tool_text(raw) == text
    assert "$ref" in _json_keys([response["response"] for response in _gemini_function_responses(raw)])
    [function_response] = _gemini_function_responses(sent)
    assert function_response["name"] == "get_block_schema"
    assert function_response["response"] == {"content": text}
    assert json.loads(_tool_text(sent)) == {"content": text}
    assert _tool_message(sent)["tool_call_id"] == "call_schema"
    assert turn == original_turn


@pytest.mark.asyncio
async def test_a_tool_result_too_deep_to_walk_is_still_sent_to_a_gemini_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    deep = "{" + '"a": {' * 600 + "}" * 600 + "}"

    sent = await _sent_messages(
        monkeypatch,
        CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 1),
        _tool_turn(deep),
        {"fallbacks": [_GEMINI]},
    )

    assert json.loads(_tool_text(sent)) == {"content": deep}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "output"),
    [
        ("azure/gpt-5.6-sol", [{"type": "text", "text": _REF_RESULT}]),
        ("openai/gpt-5.6", _REF_RESULT),
        ("anthropic/claude-sonnet-4-5", [{"type": "text", "text": _REF_RESULT}]),
        ("foo/bar", _REF_RESULT),
        (_GEMINI, '{"ok": true, "data": {"count": 3}}'),
        (_GEMINI, [{"type": "text", "text": '{"ok": false, "error": "Timed out waiting for the page"}'}]),
        (_GEMINI, "An error occurred while running the tool."),
        (_GEMINI, json.dumps([{"$ref": "#/$defs/Step"}])),
    ],
)
async def test_tool_results_off_the_gemini_escape_reach_the_provider_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    output: object,
) -> None:
    turn = _tool_turn(output)
    original_turn = copy.deepcopy(turn)

    raw = await _sent_messages(monkeypatch, LitellmModel(model=model_name), turn, None)
    sent = await _sent_messages(
        monkeypatch, CopilotLitellmModel(model=model_name, next_model_call_index=lambda: 1), turn, None
    )

    assert sent == raw
    assert turn == original_turn


@pytest.mark.asyncio
async def test_gemini_route_leaves_an_unresolved_media_reference_for_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_reference = {"screenshot": {"$ref": "page.png"}}
    turn = _tool_turn(
        [
            {"type": "text", "text": json.dumps(media_reference)},
            {"type": "image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
        ]
    )

    raw = await _sent_messages(monkeypatch, LitellmModel(model=_GEMINI), turn, None)
    sent = await _sent_messages(
        monkeypatch, CopilotLitellmModel(model=_GEMINI, next_model_call_index=lambda: 1), turn, None
    )

    assert sent == raw
    [function_response] = _gemini_function_responses(sent)
    assert function_response["response"] == media_reference
    assert [set(part) for part in function_response["parts"]] == [{"inline_data"}]


_AZURE_BASE = "https://example.openai.azure.com"


def _router_hop(model: str, base_url: str | None = None) -> dict[str, Any]:
    return {
        "api_key": "sk-hop",
        "api_version": None,
        "model_info": {},
        "vertex_credentials": None,
        "vertex_location": None,
        "thinking": None,
        "service_tier": None,
        "model": model,
        "base_url": base_url,
        "timeout": 30,
    }


def _gpt_route_settings(model_name: str, fallbacks: list[str | dict[str, Any]]) -> ModelSettings:
    extra_args: dict[str, Any] = {**_GPT_REASONING, "api_version": "2025-04-01-preview"}
    if "/responses/" in model_name:
        extra_args["allowed_openai_params"] = ["reasoning_effort", "service_tier"]
    if fallbacks:
        extra_args["fallbacks"] = fallbacks
    return ModelSettings(include_usage=True, extra_args=extra_args)


def _keyed_parts(items: list[dict[str, Any]]) -> list[tuple[int, int]]:
    return [
        (item_index, part_index)
        for item_index, item in enumerate(items)
        for part_index, part in enumerate(item.get(cache_envelope_module.item_parts_key(item)) or [])
        if "prompt_cache_breakpoint" in part
    ]


_LIST_OUTPUT_ROUND: list[TResponseInputItem] = [
    {"type": "function_call", "call_id": "call_1", "name": "evaluate", "arguments": "{}"},
    {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "input_text", "text": "listed"}]},
]
_USER_BUILD: TResponseInputItem = {"role": "user", "content": "Build the workflow"}
_GPT_TOOL_HISTORY: list[TResponseInputItem] = [
    _USER_BUILD,
    *_tool_round("call_0"),
    *_LIST_OUTPUT_ROUND,
    *_tool_round("call_2"),
    *_tool_round("call_3"),
    *_tool_round("call_4"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "base_url", "fallbacks"),
    [
        ("azure/gpt-5.6-terra", _AZURE_BASE, [_router_hop("gpt-5.6-terra")]),
        ("azure/responses/gpt-6.1-sol", _AZURE_BASE, [_router_hop("openai/responses/gpt-6.1-sol")]),
        ("azure/responses/gpt-6-sol", _AZURE_BASE, [_router_hop("openai/responses/gpt-6-sol")]),
        ("openai/responses/gpt-6.1-sol", None, []),
    ],
)
@pytest.mark.parametrize(
    ("turn", "anchor"),
    [
        (_GPT_TOOL_HISTORY, {"type": "function_call_output", "call_id": "call_1"}),
        ([_USER_BUILD, _FRAME], {"role": "user"}),
    ],
    ids=["list_tool_output_anchor", "first_user_anchor"],
)
async def test_gpt56_plus_chain_marks_the_stable_prefix_and_anchor_and_keeps_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    base_url: str | None,
    fallbacks: list[str | dict[str, Any]],
    turn: list[TResponseInputItem],
    anchor: dict[str, str],
) -> None:
    chat_requests: list[dict[str, Any]] = []
    responses_requests: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        chat_requests.append(kwargs)
        return _completion()

    async def fake_aresponses(**kwargs: Any) -> ResponsesAPIResponse:
        responses_requests.append(kwargs)
        return _responses_completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    model_settings = _gpt_route_settings(model_name, copy.deepcopy(fallbacks))
    with capture_logs() as logs:
        await CopilotLitellmModel(model=model_name, base_url=base_url, next_model_call_index=lambda: 1).get_response(
            system_instructions=_CACHED_PROMPT,
            input=turn,
            model_settings=model_settings,
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert chat_requests == []
    assert len(responses_requests) == 1
    request = responses_requests[0]
    stable_part, dynamic_part = request["input"][0]["content"]
    assert stable_part["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in dynamic_part
    assert stable_part["text"] + dynamic_part["text"] == str(_CACHED_PROMPT)
    keyed = _keyed_parts(request["input"])
    (*_, (anchor_index, _)) = keyed
    assert anchor.items() <= request["input"][anchor_index].items()
    assert request["extra_body"]["prompt_cache_options"] == {"mode": "explicit"}
    assert request["prompt_cache_key"].startswith("copilot:")
    assert _cache_usage(logs) == ("explicit", len(keyed), len("stable instructions"))
    assert model_settings.extra_args is not None
    assert model_settings.extra_args.get("fallbacks", []) == fallbacks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "fallback"),
    [("azure/gpt-5.6-terra", "gpt-5.6-terra"), ("azure/responses/gpt-6.1-sol", "openai/responses/gpt-6.1-sol")],
)
async def test_gpt_route_keeps_every_earlier_breakpoint_so_the_next_call_reads_what_this_one_wrote(
    monkeypatch: pytest.MonkeyPatch, model_name: str, fallback: str
) -> None:
    responses_requests: list[dict[str, Any]] = []

    async def fake_aresponses(**kwargs: Any) -> ResponsesAPIResponse:
        responses_requests.append(kwargs)
        return _responses_completion()

    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    turn = [_USER_BUILD, *(item for call in range(4) for item in _tool_round(f"call_{call}"))]
    for model_input in (turn, [*turn, *_tool_round("call_4")]):
        await CopilotLitellmModel(model=model_name, base_url=_AZURE_BASE, next_model_call_index=lambda: 1).get_response(
            system_instructions=_CACHED_PROMPT,
            input=model_input,
            model_settings=_gpt_route_settings(model_name, [_router_hop(fallback)]),
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    first, second = (
        {(request["input"][index].get("role"), request["input"][index].get("call_id")) for index, _ in keyed}
        for request in responses_requests
        for keyed in [_keyed_parts(request["input"])]
    )
    assert ("system", None) in first
    assert len(first) >= 2
    assert first < second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "extra_args"),
    [
        ("azure/gpt-5.6-terra", {**_GPT_REASONING, "fallbacks": [_router_hop("gpt-5.5")]}),
        ("azure/gpt-5.6-terra", {**_GPT_REASONING, "fallbacks": ["anthropic/claude-sonnet-5-5"]}),
        ("azure/gpt-5.6-terra", {**_GPT_REASONING, "fallbacks": [_GEMINI]}),
        ("gpt-5.5", _GPT_REASONING),
        ("azure/gpt-5.6-terra", None),
        ("openai/gpt-5.6-terra", _GPT_REASONING),
        ("azure/gpt-5.6-terra", {**_GPT_REASONING, "fallbacks": [_router_hop("gpt-5.6-terra", "https://gw.test/v1")]}),
        (
            "azure/gpt-5.6-terra",
            {**_GPT_REASONING, "fallbacks": [{**_router_hop("gpt-5.6-terra"), "reasoning_effort": None}]},
        ),
        (
            "azure/gpt-5.6-terra",
            {**_GPT_REASONING, "fallbacks": [{**_router_hop("gpt-5.6-terra"), "extra_body": {"service_tier": "flex"}}]},
        ),
        ("azure/gpt-5.6-terra", {**_GPT_REASONING, "fallbacks": [_router_hop("azure/gpt-5-mini")]}),
    ],
    ids=[
        "gpt55_hop",
        "claude_hop",
        "gemini_hop",
        "gpt55_primary",
        "chat_transport",
        "openai_primary_on_custom_base",
        "openai_hop_on_custom_base",
        "dict_hop_overrides_reasoning",
        "dict_hop_overrides_extra_body",
        "gpt5_mini_hop",
    ],
)
async def test_chain_with_an_ineligible_hop_or_chat_transport_sends_no_cache_markers(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    extra_args: dict[str, Any] | None,
) -> None:
    chat_requests: list[dict[str, Any]] = []
    responses_requests: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        chat_requests.append(kwargs)
        return _completion()

    async def fake_aresponses(**kwargs: Any) -> ResponsesAPIResponse:
        responses_requests.append(kwargs)
        return _responses_completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    with capture_logs() as logs:
        await CopilotLitellmModel(model=model_name, base_url=_AZURE_BASE, next_model_call_index=lambda: 1).get_response(
            system_instructions=_CACHED_PROMPT,
            input=_GPT_TOOL_HISTORY,
            model_settings=ModelSettings(extra_args=copy.deepcopy(extra_args)),
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert responses_requests == []
    (request,) = chat_requests
    sent = json.dumps(request, default=str)
    assert "prompt_cache_breakpoint" not in sent
    assert "prompt_cache_options" not in sent
    assert "prompt_cache_key" not in request
    assert request.get("fallbacks") == (extra_args or {}).get("fallbacks")
    assert _cache_usage(logs)[:2] == ("implicit", 0)


@dataclass
class _ResponsesStub:
    base: str
    requests: list[tuple[str, str, dict[str, Any]]]


@pytest.fixture
def responses_stub() -> Iterator[_ResponsesStub]:
    stub = _ResponsesStub(base="", requests=[])
    reply = _responses_completion().model_dump_json().encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            stub.requests.append((self.path, self.headers.get("Authorization") or self.headers["api-key"], body))
            failed = self.path.startswith("/fail/")
            self.send_response(500 if failed else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "boom"}}' if failed else reply)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    stub.base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("primary", "hop_model", "wire_model"),
    [
        ("azure/gpt-5.6-terra", "gpt-5.6-terra", "gpt-5.6-terra"),
        ("azure/responses/gpt-6.1-sol", "openai/responses/gpt-6.1-sol", "gpt-6.1-sol"),
    ],
)
async def test_a_failed_azure_primary_reaches_the_openai_hop_with_its_credentials_and_cache_markers(
    monkeypatch: pytest.MonkeyPatch,
    responses_stub: _ResponsesStub,
    primary: str,
    hop_model: str,
    wire_model: str,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", f"{responses_stub.base}/openai/v1")
    model_settings = _gpt_route_settings(primary, [_router_hop(hop_model)])
    with capture_logs() as logs:
        await CopilotLitellmModel(
            model=primary,
            base_url=f"{responses_stub.base}/fail",
            api_key="azure-key",
            next_model_call_index=lambda: 1,
        ).get_response(
            system_instructions=_CACHED_PROMPT,
            input=_GPT_TOOL_HISTORY,
            model_settings=model_settings,
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    (azure_path, azure_auth, azure_body), (hop_path, hop_auth, hop_body) = responses_stub.requests
    assert azure_path.startswith("/fail/openai/responses?api-version=")
    assert azure_auth == "azure-key"
    assert (hop_path, hop_auth, hop_body["model"]) == ("/openai/v1/responses", "Bearer sk-hop", wire_model)
    for body in (azure_body, hop_body):
        assert body.get("prompt_cache_options") == {"mode": "explicit"}
        assert body["prompt_cache_key"].startswith("copilot:")
        assert len(_keyed_parts(body["input"])) >= 2
    assert azure_body["input"] == hop_body["input"]
    (usage,) = (entry for entry in logs if entry.get("log_code") == "copilot_model_usage")
    assert usage["gen_ai.provider.name"] == "openai"


_RESPONSE_ITERATOR = (
    "skyvern.forge.sdk.copilot.model_telemetry.responses_api_bridge.transformation_handler.get_model_response_iterator"
)


def _drained_then_chunks(
    streaming_response: AsyncIterator[SimpleNamespace], sync_stream: bool
) -> AsyncIterator[ChatCompletionChunk]:
    async def converted_stream() -> AsyncIterator[ChatCompletionChunk]:
        async for _ in streaming_response:
            pass
        for chunk in _stream_chunks():
            yield chunk

    return converted_stream()


@pytest.mark.asyncio
async def test_a_streamed_fallback_is_attributed_to_the_hop_that_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[str] = []

    async def raw_stream() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(type="response.completed", response=_responses_completion())

    async def fake_aresponses(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        attempted.append(kwargs["model"])
        if kwargs["model"].startswith("azure/"):
            raise litellm.exceptions.InternalServerError(message="boom", llm_provider="azure", model=kwargs["model"])
        return raw_stream()

    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    monkeypatch.setattr(_RESPONSE_ITERATOR, _drained_then_chunks)
    with capture_logs() as logs:
        async for _ in CopilotLitellmModel(
            model="azure/gpt-5.6-terra",
            base_url=_AZURE_BASE,
            api_key="azure-key",
            next_model_call_index=lambda: 1,
        ).stream_response(
            system_instructions=_CACHED_PROMPT,
            input=_GPT_TOOL_HISTORY,
            model_settings=_gpt_route_settings("azure/gpt-5.6-terra", [_router_hop("gpt-5.6-terra")]),
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        ):
            pass

    assert attempted == ["azure/gpt-5.6-terra", "gpt-5.6-terra"]
    (usage,) = (entry for entry in logs if entry.get("log_code") == "copilot_model_usage")
    assert usage["gen_ai.provider.name"] == "openai"


@pytest.mark.asyncio
async def test_a_stream_that_drops_midway_raises_the_typed_error_copilot_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dropped_stream() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(type="response.created", response=None)
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    async def fake_aresponses(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        return dropped_stream()

    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    monkeypatch.setattr(_RESPONSE_ITERATOR, _drained_then_chunks)
    with pytest.raises(litellm.APIConnectionError) as raised:
        async for _ in CopilotLitellmModel(
            model="azure/gpt-5.6-terra",
            base_url=_AZURE_BASE,
            api_key="azure-key",
            next_model_call_index=lambda: 1,
        ).stream_response(
            system_instructions=_CACHED_PROMPT,
            input=_GPT_TOOL_HISTORY,
            model_settings=_gpt_route_settings("azure/gpt-5.6-terra", [_router_hop("gpt-5.6-terra")]),
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        ):
            pass

    assert is_retriable_llm_error(raised.value) is True


@pytest.mark.asyncio
async def test_every_failed_hop_is_logged_and_an_exhausted_chain_raises_litellms_fallback_error(
    monkeypatch: pytest.MonkeyPatch,
    responses_stub: _ResponsesStub,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", f"{responses_stub.base}/fail/openai/v1")
    with capture_logs() as logs, pytest.raises(Exception) as raised:
        await CopilotLitellmModel(
            model="azure/gpt-5.6-terra",
            base_url=f"{responses_stub.base}/fail",
            api_key="azure-key",
            next_model_call_index=lambda: 1,
        ).get_response(
            system_instructions=_CACHED_PROMPT,
            input=_GPT_TOOL_HISTORY,
            model_settings=_gpt_route_settings("azure/gpt-5.6-terra", [_router_hop("gpt-5.6-terra")]),
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert len(responses_stub.requests) == 2
    assert type(raised.value) is Exception
    assert "All fallback attempts failed" in str(raised.value)
    assert raised.value.__cause__ is None
    failures = [entry for entry in logs if entry["event"] == "Copilot Responses hop failed"]
    assert [(entry["hop_index"], entry["model"], entry["status_code"]) for entry in failures] == [
        (0, "azure/gpt-5.6-terra", 500),
        (1, "gpt-5.6-terra", 500),
    ]
    assert failures[-1]["error_type"] == "InternalServerError"
    assert all(
        set(entry) == {"event", "log_level", "hop_index", "model", "error_type", "status_code", "error"}
        for entry in failures
    )
    assert all("boom" in entry["error"] and len(entry["error"]) <= 300 for entry in failures)

    async def last_hop_fails(**kwargs: Any) -> LiteLLMModelResponse:
        raise litellm.exceptions.InternalServerError(message="boom", llm_provider="openai", model=kwargs["model"])

    monkeypatch.setattr("litellm.acompletion", last_hop_fails)
    with pytest.raises(Exception) as litellm_raised:
        await async_completion_with_fallbacks(
            model="azure/gpt-5.6-terra", messages=[], kwargs={"fallbacks": ["gpt-5.6-terra"]}
        )
    assert is_retriable_llm_error(raised.value) is is_retriable_llm_error(litellm_raised.value) is False
    assert (
        build_recoverable_failure(raised.value, workflow_modified=False).failure_kind
        == build_recoverable_failure(litellm_raised.value, workflow_modified=False).failure_kind
    )


_RESPONSES_KWARG_NAMES = {
    "messages": "input",
    "max_tokens": "max_output_tokens",
    "reasoning_effort": "reasoning",
    "response_format": "text",
    "base_url": "api_base",
}
_REWRITTEN_FOR_RESPONSES = {"input", "tools", "reasoning", "text", "extra_body"}


@pytest.mark.asyncio
async def test_explicit_responses_request_forwards_every_setting_the_sdk_sends_to_acompletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, dict[str, Any]] = {}

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        sent["chat"] = kwargs
        return _completion()

    async def fake_aresponses(**kwargs: Any) -> ResponsesAPIResponse:
        sent["responses"] = kwargs
        return _responses_completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    monkeypatch.setattr("litellm.aresponses", fake_aresponses)
    model_settings = ModelSettings(
        temperature=0.2,
        top_p=0.9,
        max_tokens=512,
        tool_choice="required",
        parallel_tool_calls=True,
        extra_headers={"x-test": "1"},
        extra_query={"q": "1"},
        metadata={"m": "1"},
        extra_body={"custom_body": 1},
        extra_args={**_GPT_REASONING, "api_version": "2025-04-01-preview", "custom_arg": 1},
    )
    for system_instructions in (str(_CACHED_PROMPT), _CACHED_PROMPT):
        await CopilotLitellmModel(
            model="azure/gpt-5.6-terra",
            base_url=_AZURE_BASE,
            api_key="azure-key",
            next_model_call_index=lambda: 1,
        ).get_response(
            system_instructions=system_instructions,
            input=[_USER_BUILD],
            model_settings=model_settings,
            tools=[_lookup_number],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

    assert set(sent) == {"chat", "responses"}
    chat = {key: value for key, value in sent["chat"].items() if value is not None}
    responses = sent["responses"]
    for key, value in chat.items():
        name = _RESPONSES_KWARG_NAMES.get(key, key)
        assert name in responses, key
        if name not in _REWRITTEN_FOR_RESPONSES:
            assert responses[name] == value, key
    assert chat["extra_body"].items() <= responses["extra_body"].items()
