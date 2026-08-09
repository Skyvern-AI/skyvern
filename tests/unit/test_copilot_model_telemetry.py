from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents import ModelSettings, function_tool
from agents.extensions.models.litellm_model import LitellmModel
from agents.models.interface import ModelTracing
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.utils import Delta
from litellm.types.utils import ModelResponse as LiteLLMModelResponse
from litellm.types.utils import ModelResponseStream, StreamingChoices, Usage
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk

from skyvern.forge.sdk.copilot import model_telemetry as model_telemetry_module
from skyvern.forge.sdk.copilot.cache_envelope import CacheableSystemInstructions
from skyvern.forge.sdk.copilot.model_telemetry import (
    CopilotLitellmModel,
    current_model_call_telemetry,
    model_call_telemetry_scope,
)


@function_tool
def _lookup_number(name: str) -> int:
    return len(name)


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
):
    return await model.get_response(
        system_instructions=system_instructions,
        input=[{"role": "user", "content": "Return 42"}],
        model_settings=ModelSettings(temperature=0, include_usage=True),
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
    await _get_response(
        CopilotLitellmModel(model="gpt-5.6-sol", next_model_call_index=lambda: 1),
        system_instructions=str(prompt),
    )
    await _get_response(
        CopilotLitellmModel(model="gpt-5.6-sol", next_model_call_index=lambda: 2),
        system_instructions=prompt,
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
        ("explicit", 1, len("stable instructions")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "base_url"),
    [
        ("azure/gpt-5.6-sol", None),
        ("gpt-5.6-sol", "https://example.openai.azure.com"),
        ("gpt-5.5", None),
    ],
)
async def test_explicit_cache_envelope_leaves_other_routes_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    base_url: str | None,
) -> None:
    requests: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        requests.append(kwargs)
        telemetry = current_model_call_telemetry()
        assert telemetry is not None
        assert telemetry.cache_mode == "implicit"
        return _completion()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    prompt = CacheableSystemInstructions("stable", "dynamic", cache_namespace="wcc_test")
    await _get_response(
        CopilotLitellmModel(
            model=model_name,
            base_url=base_url,
            next_model_call_index=lambda: 1,
        ),
        system_instructions=prompt,
    )

    assert "extra_body" not in requests[0]


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
        model_settings=ModelSettings(include_usage=True),
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
async def test_model_error_resets_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs: Any) -> LiteLLMModelResponse:
        raise RuntimeError("provider failed")

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)

    with pytest.raises(RuntimeError, match="provider failed"):
        await _get_response(CopilotLitellmModel(model="openai/gpt-5.6", next_model_call_index=lambda: 1))

    assert current_model_call_telemetry() is None


def test_nested_model_call_scopes_restore_outer_call() -> None:
    with model_call_telemetry_scope(1) as outer:
        assert current_model_call_telemetry() is outer
        with model_call_telemetry_scope(2) as inner:
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

    with model_call_telemetry_scope(3, model="gpt-5.6-sol") as telemetry:
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

    with model_call_telemetry_scope(
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

    with model_call_telemetry_scope(
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


def test_model_call_without_provider_usage_does_not_emit_datadog_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        model_telemetry_module.LOG,
        "info",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    with model_call_telemetry_scope(5, model="gpt-5.6-sol"):
        pass

    assert events == []


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
