"""Milestone 1 — LLM handler tracing enrichment.

These tests verify the LLM chokepoint span + SKY-8414
`llm.request.completed` event behavior implemented in
`skyvern/forge/sdk/api/llm/api_handler_factory.py`. They serve as regression
coverage for the instrumentation.

Note: OTEL's global TracerProvider can only be set once per process. This
module installs a shared TracerProvider + InMemorySpanExporter on first use
via `_ensure_provider()`. Other test files that also call
`otel_trace.set_tracer_provider(...)` will clobber or be clobbered depending
on import order. If more test files need span capture, move the provider
setup to a session-scoped fixture in conftest.py.

The tests use OTEL's `InMemorySpanExporter` — no OTEL backend, collector, or
network required. Fast and deterministic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest  # type: ignore[import-not-found]

# opentelemetry-sdk is only installed in the cloud dependency group. OSS CI
# runs `uv sync --group dev`, so this module is absent there — skip the file
# rather than error on collection.
pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace as otel_trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    EXTRACT_ACTION_PROMPT_NAME,
    LLMAPIHandlerFactory,
)
from skyvern.forge.sdk.api.llm.models import LLMConfig
from tests.unit.helpers import FakeLLMResponse

LLM_SPAN_NAME = "skyvern.llm.request"
LLM_EVENT_NAME = "llm.request.completed"


_SHARED_EXPORTER: InMemorySpanExporter | None = None


def _ensure_provider() -> InMemorySpanExporter:
    """OTEL's global TracerProvider can only be set once per process. Install
    a shared TracerProvider + InMemorySpanExporter on first use; subsequent
    tests reuse it and just clear the buffer between runs."""
    global _SHARED_EXPORTER
    if _SHARED_EXPORTER is None:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        otel_trace.set_tracer_provider(provider)
        _SHARED_EXPORTER = exporter
    return _SHARED_EXPORTER


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    exporter = _ensure_provider()
    exporter.clear()
    yield exporter
    exporter.clear()


def _span_by_name(spans: list, name: str):
    return next((s for s in spans if s.name == name), None)


async def _invoke_handler(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    prompt_name: str,
    prompt_tokens: int = 1234,
    completion_tokens: int = 567,
) -> None:
    """Call the non-router LLM handler with a stubbed litellm completion."""
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = False
    context.cached_static_prompt = None
    context.hashed_href_map = {}

    llm_config = LLMConfig(
        model_name=model_name,
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config",
        lambda _: llm_config,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config",
        lambda _: False,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current",
        lambda: context,
    )
    monkeypatch.setattr(
        api_handler_factory,
        "llm_messages_builder",
        AsyncMock(return_value=[{"role": "user", "content": "test"}]),
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    response = FakeLLMResponse(model_name)
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    monkeypatch.setattr(
        api_handler_factory.litellm,
        "acompletion",
        AsyncMock(return_value=response),
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler(model_name)
    await handler(prompt="test prompt", prompt_name=prompt_name)


@pytest.mark.asyncio
async def test_llm_handler_emits_span_with_canonical_name(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    """The chokepoint must emit a span named `skyvern.llm.request` (not the Python qualname)."""
    await _invoke_handler(monkeypatch, "gpt-4", EXTRACT_ACTION_PROMPT_NAME)
    spans = span_exporter.get_finished_spans()
    span = _span_by_name(spans, LLM_SPAN_NAME)
    assert span is not None, f"Expected span {LLM_SPAN_NAME!r}, got {[s.name for s in spans]}"


@pytest.mark.asyncio
async def test_llm_handler_span_has_enriched_attributes(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    """Span attributes must be queryable in SigNoz for Milestone 2 aggregations."""
    await _invoke_handler(
        monkeypatch,
        model_name="gpt-4",
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        prompt_tokens=1234,
        completion_tokens=567,
    )
    span = _span_by_name(span_exporter.get_finished_spans(), LLM_SPAN_NAME)
    assert span is not None

    attrs = span.attributes or {}
    assert attrs.get("llm_model") == "gpt-4"
    assert attrs.get("prompt_name") == EXTRACT_ACTION_PROMPT_NAME
    assert attrs.get("prompt_tokens") == 1234
    assert attrs.get("completion_tokens") == 567
    assert "latency_ms" in attrs
    assert attrs.get("status") == "ok"


@pytest.mark.asyncio
async def test_llm_handler_emits_request_completed_event(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    """SKY-8414: emit `llm.request.completed` event on the span."""
    await _invoke_handler(monkeypatch, "gpt-4", EXTRACT_ACTION_PROMPT_NAME)
    span = _span_by_name(span_exporter.get_finished_spans(), LLM_SPAN_NAME)
    assert span is not None

    event = next((e for e in span.events if e.name == LLM_EVENT_NAME), None)
    assert event is not None, f"Expected event {LLM_EVENT_NAME!r}, got {[e.name for e in span.events]}"
    assert event.attributes.get("model") == "gpt-4"
    assert event.attributes.get("prompt_tokens") == 1234
    assert event.attributes.get("completion_tokens") == 567


@pytest.mark.asyncio
async def test_llm_handler_span_records_error_status(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    """On LLM provider error, span.status must be ERROR and attribute `status=error`."""
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = False
    context.cached_static_prompt = None
    context.hashed_href_map = {}

    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)
    monkeypatch.setattr(
        api_handler_factory,
        "llm_messages_builder",
        AsyncMock(return_value=[{"role": "user", "content": "test"}]),
    )
    monkeypatch.setattr(
        api_handler_factory.litellm,
        "acompletion",
        AsyncMock(side_effect=RuntimeError("provider 500")),
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    with pytest.raises(Exception):
        await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    span = _span_by_name(span_exporter.get_finished_spans(), LLM_SPAN_NAME)
    assert span is not None
    assert span.status.status_code.name == "ERROR"
    assert (span.attributes or {}).get("status") == "error"


@pytest.mark.asyncio
async def test_llm_handler_span_has_no_prompt_content(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    """Privacy: never attach raw prompt content, completion text, or screenshots as attributes."""
    await _invoke_handler(monkeypatch, "gpt-4", EXTRACT_ACTION_PROMPT_NAME)
    span = _span_by_name(span_exporter.get_finished_spans(), LLM_SPAN_NAME)
    assert span is not None

    attrs = span.attributes or {}
    forbidden = {"prompt", "completion", "messages", "response_content", "screenshot", "screenshots"}
    leaked = forbidden & set(attrs.keys())
    assert not leaked, f"Privacy violation: span attributes must not include {leaked}"
