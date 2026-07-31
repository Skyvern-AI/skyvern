from __future__ import annotations

import contextlib
import contextvars
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, cast, overload

import litellm
import structlog
from agents.agent_output import AgentOutputSchemaBase
from agents.extensions.models.litellm_model import LitellmModel
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models._retry_runtime import should_disable_provider_managed_retries
from agents.models.chatcmpl_converter import Converter
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.models.interface import ModelTracing
from agents.models.openai_responses import Converter as OpenAIResponsesConverter
from agents.tool import Tool
from agents.tracing.span_data import GenerationSpanData
from agents.tracing.spans import Span
from litellm.completion_extras import responses_api_bridge
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.utils import ModelResponse as LiteLLMModelResponse
from litellm.types.utils import Usage as LiteLLMUsage
from openai import AsyncStream, omit
from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import Response

from skyvern.forge.sdk.copilot.cache_envelope import (
    CacheableSystemInstructions,
    ExplicitCacheEnvelope,
    build_explicit_cache_envelope,
)

LOG = structlog.get_logger()
_DATED_MODEL_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _usage_field(value: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(value, dict):
            if key in value:
                return value[key]
        else:
            field = getattr(value, key, None)
            if field is not None:
                return field
    return None


@dataclass(slots=True)
class CopilotModelCallTelemetry:
    model_call_index: int
    response_model: str | None = None
    cache_mode: Literal["implicit", "explicit"] = "implicit"
    cache_breakpoint_count: int = 0
    cache_stable_prefix_chars: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def capture(self, usage: Any | None) -> None:
        if usage is None:
            return
        input_tokens = _usage_field(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_field(usage, "completion_tokens", "output_tokens")
        if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
            self.input_tokens = input_tokens
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            self.output_tokens = output_tokens

        details = _usage_field(usage, "prompt_tokens_details", "input_tokens_details")
        if details is not None:
            cache_read = _usage_field(details, "cached_tokens")
            if isinstance(cache_read, int) and not isinstance(cache_read, bool):
                self.cache_read_tokens = cache_read
            cache_write = _usage_field(details, "cache_write_tokens")
            if cache_write is None:
                cache_write = (getattr(details, "model_extra", None) or {}).get("cache_write_tokens")
            if isinstance(cache_write, int) and not isinstance(cache_write, bool):
                self.cache_write_tokens = cache_write


_current_model_call_telemetry: contextvars.ContextVar[CopilotModelCallTelemetry | None] = contextvars.ContextVar(
    "_current_model_call_telemetry",
    default=None,
)


def current_model_call_telemetry() -> CopilotModelCallTelemetry | None:
    return _current_model_call_telemetry.get()


def _model_call_cost(telemetry: CopilotModelCallTelemetry, model: str) -> float | None:
    if telemetry.input_tokens is None or telemetry.output_tokens is None:
        return None
    pricing_model = _DATED_MODEL_SUFFIX.sub("", model)
    try:
        input_cost, output_cost = litellm.cost_per_token(
            model=pricing_model,
            prompt_tokens=telemetry.input_tokens,
            completion_tokens=telemetry.output_tokens,
            cache_read_input_tokens=telemetry.cache_read_tokens,
            cache_creation_input_tokens=telemetry.cache_write_tokens,
            call_type="aresponses",
        )
    except Exception:
        return None
    return float(input_cost + output_cost)


def _otel_provider_name(model: str, base_url: str | None) -> str | None:
    normalized = model.lower()
    provider, separator, _ = normalized.partition("/")
    explicit_provider = {
        "azure": "azure.ai.openai",
        "openai": "openai",
        "anthropic": "anthropic",
        "bedrock": "aws.bedrock",
        "vertex_ai": "gcp.vertex_ai",
        "gemini": "gcp.gemini",
    }.get(provider)
    if separator and explicit_provider is not None:
        return explicit_provider
    if base_url and ".openai.azure.com" in base_url.lower():
        return "azure.ai.openai"
    if normalized.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return None


def _log_model_call_usage(
    telemetry: CopilotModelCallTelemetry,
    model: str,
    base_url: str | None,
) -> None:
    """Emit one Datadog-searchable usage event after provider usage arrives."""

    if telemetry.input_tokens is None and telemetry.output_tokens is None:
        return

    billing_model = telemetry.response_model or model
    fields: dict[str, Any] = {
        "log_code": "copilot_model_usage",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": model,
        "copilot.model_call_index": telemetry.model_call_index,
        "copilot.cache.mode": telemetry.cache_mode,
        "copilot.cache.breakpoint_count": telemetry.cache_breakpoint_count,
    }
    optional_fields = {
        "gen_ai.response.model": telemetry.response_model,
        "copilot.cache.stable_prefix_chars": telemetry.cache_stable_prefix_chars,
        "gen_ai.usage.input_tokens": telemetry.input_tokens,
        "gen_ai.usage.output_tokens": telemetry.output_tokens,
        "gen_ai.usage.cache_read.input_tokens": telemetry.cache_read_tokens,
        "gen_ai.usage.cache_creation.input_tokens": telemetry.cache_write_tokens,
        "operation.cost": _model_call_cost(telemetry, billing_model),
        "gen_ai.provider.name": _otel_provider_name(billing_model, base_url),
    }
    fields.update({key: value for key, value in optional_fields.items() if value is not None})
    LOG.info("Copilot model usage", **fields)


@contextlib.contextmanager
def model_call_telemetry_scope(
    model_call_index: int,
    *,
    model: str | None = None,
    base_url: str | None = None,
) -> Iterator[CopilotModelCallTelemetry]:
    telemetry = CopilotModelCallTelemetry(model_call_index=model_call_index)
    token = _current_model_call_telemetry.set(telemetry)
    try:
        yield telemetry
    finally:
        if model is not None:
            _log_model_call_usage(telemetry, model, base_url)
        _current_model_call_telemetry.reset(token)


class _UsageCapturingStream:
    def __init__(
        self,
        stream: AsyncStream[ChatCompletionChunk],
        telemetry: CopilotModelCallTelemetry,
    ) -> None:
        self._stream = stream
        self._iterator = stream.__aiter__()
        self._telemetry = telemetry

    def __aiter__(self) -> _UsageCapturingStream:
        return self

    async def __anext__(self) -> ChatCompletionChunk:
        chunk = await self._iterator.__anext__()
        if isinstance(chunk.model, str):
            self._telemetry.response_model = chunk.model
        # LiteLLM returns ModelResponseStream at runtime even though its public
        # annotation is ChatCompletionChunk. Content chunks omit ``usage``.
        usage = getattr(chunk, "usage", None)
        if isinstance(usage, (LiteLLMUsage, CompletionUsage)):
            _capture_usage(self._telemetry, usage)
        return chunk


class _ResponsesUsageCapturingStream:
    """Capture raw Responses usage before LiteLLM drops cache-write tokens."""

    def __init__(self, stream: AsyncIterator[Any], telemetry: CopilotModelCallTelemetry) -> None:
        self._iterator = stream.__aiter__()
        self._telemetry = telemetry

    def __aiter__(self) -> _ResponsesUsageCapturingStream:
        return self

    async def __anext__(self) -> Any:
        event = await self._iterator.__anext__()
        response = getattr(event, "response", None)
        response_model = getattr(response, "model", None)
        if isinstance(response_model, str):
            self._telemetry.response_model = response_model
        usage = getattr(response, "usage", None)
        if usage is not None:
            _capture_usage(self._telemetry, usage)
        return event


def _capture_usage(
    telemetry: CopilotModelCallTelemetry,
    usage: Any | None,
) -> None:
    try:
        telemetry.capture(usage)
    except Exception as exc:
        LOG.warning("Failed to capture Copilot model usage", error=repr(exc))


class CopilotLitellmModel(LitellmModel):
    def __init__(
        self,
        model: str,
        *,
        next_model_call_index: Callable[[], int],
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(model=model, base_url=base_url, api_key=api_key)
        self.next_model_call_index = next_model_call_index

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> ModelResponse:
        with model_call_telemetry_scope(
            self.next_model_call_index(),
            model=self.model,
            base_url=self.base_url,
        ):
            return await super().get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id,
                conversation_id,
                prompt,
            )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        with model_call_telemetry_scope(
            self.next_model_call_index(),
            model=self.model,
            base_url=self.base_url,
        ):
            async for event in super().stream_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id,
                conversation_id,
                prompt,
            ):
                yield event

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[True],
        prompt: Any | None = None,
    ) -> tuple[Response, AsyncStream[ChatCompletionChunk]]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[False],
        prompt: Any | None = None,
    ) -> LiteLLMModelResponse: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool = False,
        prompt: Any | None = None,
    ) -> LiteLLMModelResponse | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        explicit_cache_envelope = build_explicit_cache_envelope(
            model=self.model,
            base_url=self.base_url,
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            handoffs=handoffs,
            should_replay_reasoning_content=self.should_replay_reasoning_content,
        )
        if explicit_cache_envelope is not None:
            telemetry = current_model_call_telemetry()
            if telemetry is not None:
                telemetry.cache_mode = "explicit"
                telemetry.cache_breakpoint_count = 1
                if isinstance(system_instructions, CacheableSystemInstructions):
                    telemetry.cache_stable_prefix_chars = len(system_instructions.stable_prefix)
            result = await self._fetch_explicit_responses(
                explicit_cache_envelope,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span,
                tracing,
                stream,
            )
        else:
            result = await super()._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span,
                tracing,
                stream,
                prompt,
            )
        telemetry = current_model_call_telemetry()
        if isinstance(result, LiteLLMModelResponse):
            if telemetry is not None:
                if isinstance(result.model, str):
                    telemetry.response_model = result.model
                usage = result.get("usage")
                if isinstance(usage, LiteLLMUsage):
                    _capture_usage(telemetry, usage)
            return result

        response, response_stream = result
        if telemetry is None:
            return result
        capturing_stream = _UsageCapturingStream(response_stream, telemetry)
        return response, cast(AsyncStream[ChatCompletionChunk], capturing_stream)

    async def _fetch_explicit_responses(
        self,
        envelope: ExplicitCacheEnvelope,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool,
    ) -> LiteLLMModelResponse | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        """Issue one cacheable request at the Responses boundary LiteLLM uses for GPT-5.6."""

        if tracing.include_data():
            # Keep the logical trace input identical to the pre-envelope request.
            # The breakpoint and cache key are transport metadata, not agent input.
            span.span_data.input = envelope.logical_messages

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools
            else False
            if model_settings.parallel_tool_calls is False
            else None
        )
        reasoning_effort = self._get_reasoning_effort(model_settings)
        reasoning = (
            responses_api_bridge.transformation_handler._map_reasoning_effort(reasoning_effort)
            if reasoning_effort is not None
            else None
        )

        tool_choice = OpenAIResponsesConverter.convert_tool_choice(
            model_settings.tool_choice,
            tools=tools,
            handoffs=handoffs,
            model=self.model,
        )
        request_tool_choice = None if tool_choice is omit else tool_choice

        response_format = self._remove_not_given(Converter.convert_response_format(output_schema))
        text = (
            responses_api_bridge.transformation_handler._transform_response_format_to_text_format(response_format)
            if response_format is not None
            else None
        )

        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "input": envelope.responses_input,
            "tools": envelope.responses_tools or None,
            "temperature": model_settings.temperature,
            "top_p": model_settings.top_p,
            "max_output_tokens": model_settings.max_tokens,
            "tool_choice": request_tool_choice,
            "parallel_tool_calls": parallel_tool_calls,
            "reasoning": reasoning,
            "stream": stream,
            "extra_headers": self._merge_headers(model_settings),
            "api_key": self.api_key,
            "base_url": self.base_url,
        }
        if model_settings.extra_query:
            request_kwargs["extra_query"] = dict(model_settings.extra_query)
        if model_settings.metadata:
            request_kwargs["metadata"] = dict(model_settings.metadata)
        if text is not None:
            request_kwargs["text"] = text
        if model_settings.extra_args:
            request_kwargs.update(model_settings.extra_args)
        if should_disable_provider_managed_retries():
            request_kwargs["num_retries"] = 0
            request_kwargs["max_retries"] = 0

        # These four fields define the cache envelope and remain authoritative
        # even when callers provide generic LiteLLM escape-hatch arguments.
        request_kwargs["input"] = envelope.responses_input
        request_kwargs["tools"] = envelope.responses_tools or None
        request_kwargs["prompt_cache_key"] = envelope.prompt_cache_key
        request_kwargs["extra_body"] = envelope.extra_body
        request_kwargs.pop("reasoning_effort", None)

        raw_response = await litellm.aresponses(**request_kwargs)
        telemetry = current_model_call_telemetry()

        if stream:
            raw_stream = cast(AsyncIterator[Any], raw_response)
            if telemetry is not None:
                raw_stream = _ResponsesUsageCapturingStream(raw_stream, telemetry)
            response_stream = responses_api_bridge.transformation_handler.get_model_response_iterator(
                raw_stream,
                sync_stream=False,
            )
            response_tool_choice = request_tool_choice or "auto"
            response = Response(
                id=FAKE_RESPONSES_ID,
                created_at=time.time(),
                model=self.model,
                object="response",
                output=[],
                tool_choice=response_tool_choice,  # type: ignore[arg-type]
                top_p=model_settings.top_p,
                temperature=model_settings.temperature,
                tools=[],
                parallel_tool_calls=parallel_tool_calls or False,
                reasoning=model_settings.reasoning,
            )
            return response, cast(AsyncStream[ChatCompletionChunk], response_stream)

        if not isinstance(raw_response, ResponsesAPIResponse):
            raise TypeError(f"Unexpected LiteLLM Responses result: {type(raw_response)!r}")
        if telemetry is not None:
            if isinstance(raw_response.model, str):
                telemetry.response_model = raw_response.model
            _capture_usage(telemetry, raw_response.usage)
        model_response = responses_api_bridge.transformation_handler.transform_response(
            model=self.model,
            raw_response=raw_response,
            model_response=LiteLLMModelResponse(),
            logging_obj=SimpleNamespace(),  # type: ignore[arg-type]
            request_data=request_kwargs,
            messages=envelope.logical_messages,
            optional_params={},
            litellm_params={},
            encoding=None,
            api_key=self.api_key,
        )
        return cast(LiteLLMModelResponse, model_response)
