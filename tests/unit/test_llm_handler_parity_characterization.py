"""SKY-11712 — characterization parity suite for the router / non-router LLM handlers.

Drives the same scenario matrix through `get_llm_api_handler` and
`get_llm_api_handler_with_router` against one stubbed litellm layer and pins the
observable outputs; `# drift:` markers pin cells where the variants currently disagree.
"""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import litellm  # type: ignore[import-not-found]
import pytest  # type: ignore[import-not-found]
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.config import settings
from skyvern.exceptions import SkyvernContextWindowExceededError
from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    EXTRACT_ACTION_PROMPT_NAME,
    LLM_REQUEST_COMPLETED_EVENT,
    LLM_REQUEST_SPAN_NAME,
    LLMAPIHandlerFactory,
)
from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry
from skyvern.forge.sdk.api.llm.exceptions import (
    InvalidLLMResponseFormat,
    LLMOutputTruncatedError,
    LLMProviderError,
    LLMProviderErrorRetryableTask,
)
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.schemas.llm import LLMConfig, LLMRouterConfig, LLMRouterModelConfig
from tests.unit.helpers import FakeLLMResponse

ROUTER_LLM_KEY = "PARITY_TEST_ROUTER"
LLM_COST = 0.01
PROMPT = "parity prompt"
DEFAULT_PROMPT_NAME = "check-user-goal"
SCREENSHOTS = [b"img-a", b"img-b"]

# Span attributes that must be identical between the two variants for the same
# scenario. Excludes handler_type / llm_key (intentionally per-variant) and
# latency_ms (wall-clock).
PARITY_SPAN_ATTR_KEYS = (
    "llm_model",
    "prompt_name",
    "screenshots_included",
    "screenshot_count",
    "speculative",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "status",
    "cache_hit",
    "llm_cost",
    "image_tokens",
    "image_cost",
    "image_count",
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.reasoning_tokens",
    "gen_ai.usage.cached_tokens",
    "gen_ai.usage.cost",
)


class ParityResponse(FakeLLMResponse):
    def __init__(
        self,
        model: str,
        content: str | None = '{"actions": []}',
        finish_reason: str | None = None,
        prompt_tokens: int = 1200,
        completion_tokens: int = 345,
        reasoning_tokens: int = 67,
        cached_tokens: int = 89,
    ) -> None:
        super().__init__(model)
        self._content = content
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
        self.usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
            cache_read_input_tokens=0,
        )


@dataclass
class HandlerOutcome:
    parsed: Any = None
    error: BaseException | None = None
    completion_calls: list[dict[str, Any]] = field(default_factory=list)
    router_calls: list[dict[str, Any]] = field(default_factory=list)
    builder_screenshots: list[list[bytes] | None] = field(default_factory=list)
    update_step_calls: list[dict[str, Any]] = field(default_factory=list)
    update_thought_calls: list[dict[str, Any]] = field(default_factory=list)
    block_cost_calls: list[dict[str, Any]] = field(default_factory=list)
    span: ReadableSpan | None = None

    @property
    def span_attrs(self) -> dict[str, Any]:
        assert self.span is not None, f"no {LLM_REQUEST_SPAN_NAME!r} span captured"
        return dict(self.span.attributes or {})

    @property
    def event_attrs(self) -> dict[str, Any]:
        assert self.span is not None
        event = next((e for e in self.span.events if e.name == LLM_REQUEST_COMPLETED_EVENT), None)
        assert event is not None, f"no {LLM_REQUEST_COMPLETED_EVENT!r} event on span"
        return dict(event.attributes or {})


def _parity_span_attrs(outcome: HandlerOutcome) -> dict[str, Any]:
    attrs = outcome.span_attrs
    return {key: attrs.get(key) for key in PARITY_SPAN_ATTR_KEYS}


def _event_attrs_without_latency(outcome: HandlerOutcome) -> dict[str, Any]:
    return {key: value for key, value in outcome.event_attrs.items() if key != "latency_ms"}


def _response_feeder(responses: list[Any]) -> Any:
    queue = list(responses)

    def _next() -> Any:
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return _next


def _make_step() -> Step:
    now = datetime.now()
    return Step(
        created_at=now,
        modified_at=now,
        task_id="tsk_parity",
        step_id="stp_parity",
        status=StepStatus.running,
        order=0,
        is_last=False,
        retry_index=0,
        organization_id="org_parity",
    )


def _make_thought() -> MagicMock:
    thought = MagicMock()
    thought.observer_thought_id = "ot_parity"
    thought.organization_id = "org_parity"
    return thought


def _stub_common(mp: pytest.MonkeyPatch, outcome: HandlerOutcome, context: SkyvernContext | None) -> None:
    mp.setattr(api_handler_factory.skyvern_context, "current", lambda: context)

    async def fake_llm_messages_builder(prompt: str, screenshots: Any, add_assistant_prefix: bool) -> list[dict]:
        outcome.builder_screenshots.append(screenshots)
        return [{"role": "user", "content": prompt}]

    mp.setattr(api_handler_factory, "llm_messages_builder", fake_llm_messages_builder)
    mp.setattr(api_handler_factory.litellm, "completion_cost", lambda completion_response: LLM_COST)

    artifact_manager = MagicMock()
    artifact_manager.prepare_llm_artifact = AsyncMock(return_value=None)
    artifact_manager.bulk_create_artifacts = AsyncMock()
    mp.setattr(api_handler_factory.app, "ARTIFACT_MANAGER", artifact_manager)

    async def fake_update_step(**kwargs: Any) -> MagicMock:
        outcome.update_step_calls.append(kwargs)
        return MagicMock()

    mp.setattr(api_handler_factory.app.DATABASE.tasks, "update_step", fake_update_step)

    async def fake_update_thought(**kwargs: Any) -> MagicMock:
        outcome.update_thought_calls.append(kwargs)
        return MagicMock()

    mp.setattr(api_handler_factory.app.DATABASE.observer, "update_thought", fake_update_thought)

    async def fake_increment_block_cost(**kwargs: Any) -> None:
        outcome.block_cost_calls.append(kwargs)

    mp.setattr(
        api_handler_factory.app.DATABASE.observer, "increment_workflow_run_block_llm_cost", fake_increment_block_cost
    )


def _request_span(span_exporter: InMemorySpanExporter) -> ReadableSpan | None:
    return next((s for s in span_exporter.get_finished_spans() if s.name == LLM_REQUEST_SPAN_NAME), None)


def _handler_call_kwargs(
    prompt: str,
    prompt_name: str,
    step: Step | None,
    thought: Any,
    workflow_run_block_id: str | None,
    organization_id: str | None,
    screenshots: list[bytes] | None,
    parameters: dict[str, Any] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "prompt_name": prompt_name,
        "step": step,
        "thought": thought,
        "workflow_run_block_id": workflow_run_block_id,
        "organization_id": organization_id,
        "screenshots": screenshots,
    }
    if parameters is not None:
        kwargs["parameters"] = dict(parameters)
    return kwargs


async def _run_direct(
    span_exporter: InMemorySpanExporter,
    *,
    responses: list[Any],
    model_name: str = "gpt-4",
    supports_vision: bool = True,
    context: SkyvernContext | None = None,
    prompt: str = PROMPT,
    prompt_name: str = DEFAULT_PROMPT_NAME,
    step: Step | None = None,
    thought: Any = None,
    workflow_run_block_id: str | None = None,
    organization_id: str | None = None,
    screenshots: list[bytes] | None = None,
    parameters: dict[str, Any] | None = None,
) -> HandlerOutcome:
    outcome = HandlerOutcome()
    next_response = _response_feeder(responses)
    llm_config = LLMConfig(
        model_name=model_name,
        required_env_vars=[],
        supports_vision=supports_vision,
        add_assistant_prefix=False,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config)
        mp.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False)

        async def fake_acompletion(*args: Any, **kwargs: Any) -> Any:
            outcome.completion_calls.append(dict(kwargs))
            return next_response()

        mp.setattr(api_handler_factory.litellm, "acompletion", fake_acompletion)
        _stub_common(mp, outcome, context)

        span_exporter.clear()
        handler = LLMAPIHandlerFactory.get_llm_api_handler(model_name)
        try:
            outcome.parsed = await handler(
                **_handler_call_kwargs(
                    prompt, prompt_name, step, thought, workflow_run_block_id, organization_id, screenshots, parameters
                )
            )
        except BaseException as exc:
            outcome.error = exc
    outcome.span = _request_span(span_exporter)
    return outcome


async def _run_router(
    span_exporter: InMemorySpanExporter,
    *,
    responses: list[Any],
    main_model_group: str = "gpt-4",
    litellm_model: str = "openai/gpt-4",
    fallback_groups: tuple[str, ...] = (),
    supports_vision: bool = True,
    context: SkyvernContext | None = None,
    prompt: str = PROMPT,
    prompt_name: str = DEFAULT_PROMPT_NAME,
    step: Step | None = None,
    thought: Any = None,
    workflow_run_block_id: str | None = None,
    organization_id: str | None = None,
    screenshots: list[bytes] | None = None,
    parameters: dict[str, Any] | None = None,
) -> HandlerOutcome:
    outcome = HandlerOutcome()
    next_response = _response_feeder(responses)
    deployments = [LLMRouterModelConfig(model_name=main_model_group, litellm_params={"model": litellm_model})] + [
        LLMRouterModelConfig(model_name=group, litellm_params={"model": f"openai/{group}"}) for group in fallback_groups
    ]
    router_config = LLMRouterConfig(
        model_name="parity-router",
        required_env_vars=[],
        supports_vision=supports_vision,
        add_assistant_prefix=False,
        model_list=deployments,
        redis_host="localhost",
        redis_port=6379,
        redis_password="",
        main_model_group=main_model_group,
        fallback_model_group=list(fallback_groups) or None,
        routing_strategy="simple-shuffle",
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )

    class FakeRouter:
        # model_list attribute satisfies the RouterWithModelList protocol used by
        # the vertex-cache primary path.
        def __init__(self, **kwargs: Any) -> None:
            self.model_list = kwargs.get("model_list", [])

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> Any:
            outcome.router_calls.append({"model": model, "messages": messages, **kwargs})
            return next_response()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LLMConfigRegistry, "validate_config", classmethod(lambda cls, key, cfg: None))
        LLMConfigRegistry._configs.pop(ROUTER_LLM_KEY, None)  # type: ignore[attr-defined]
        LLMConfigRegistry.register_config(ROUTER_LLM_KEY, router_config)
        LLMAPIHandlerFactory._router_handler_cache.pop(ROUTER_LLM_KEY, None)
        mp.setattr(api_handler_factory.litellm, "Router", FakeRouter)

        async def fake_acompletion(*args: Any, **kwargs: Any) -> Any:
            outcome.completion_calls.append(dict(kwargs))
            return next_response()

        mp.setattr(api_handler_factory.litellm, "acompletion", fake_acompletion)
        _stub_common(mp, outcome, context)

        span_exporter.clear()
        try:
            handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router(ROUTER_LLM_KEY)
            outcome.parsed = await handler(
                **_handler_call_kwargs(
                    prompt, prompt_name, step, thought, workflow_run_block_id, organization_id, screenshots, parameters
                )
            )
        except BaseException as exc:
            outcome.error = exc
        finally:
            LLMConfigRegistry._configs.pop(ROUTER_LLM_KEY, None)  # type: ignore[attr-defined]
            LLMAPIHandlerFactory._router_handler_cache.pop(ROUTER_LLM_KEY, None)
    outcome.span = _request_span(span_exporter)
    return outcome


@pytest.mark.asyncio
async def test_happy_path_parses_and_records_identically(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(span_exporter, responses=[ParityResponse("gpt-4")], step=_make_step(), parameters={})
    router = await _run_router(span_exporter, responses=[ParityResponse("gpt-4")], step=_make_step(), parameters={})

    assert direct.error is None and router.error is None
    assert direct.parsed == router.parsed == {"actions": []}

    assert len(direct.completion_calls) == 1 and not direct.router_calls
    assert len(router.router_calls) == 1 and not router.completion_calls
    direct_call = direct.completion_calls[0]
    router_call = router.router_calls[0]
    assert direct_call["model"] == router_call["model"] == "gpt-4"
    assert direct_call["messages"] == router_call["messages"] == [{"role": "user", "content": PROMPT}]
    assert direct_call["drop_params"] is True and router_call["drop_params"] is True
    # SKY-10200 intentional split: direct passes a per-call timeout; the router variant
    # must not (Router-level default + per-deployment timeouts take precedence).
    assert direct_call["timeout"] == settings.LLM_CONFIG_TIMEOUT
    assert "timeout" not in router_call
    # Compare shared completion args by VALUE (not just key set) so the gate catches
    # value drift (temperature, token limits, retry settings) during the SKY-11686 dedup.
    assert {k: v for k, v in direct_call.items() if k != "timeout"} == router_call

    expected_step_update = {
        "task_id": "tsk_parity",
        "step_id": "stp_parity",
        "organization_id": "org_parity",
        "incremental_cost": LLM_COST,
        "incremental_input_tokens": 1200,
        "incremental_output_tokens": 345,
        "incremental_reasoning_tokens": 67,
        "incremental_cached_tokens": 89,
        "last_llm_model": "gpt-4",
    }
    assert direct.update_step_calls == router.update_step_calls == [expected_step_update]

    assert direct.span_attrs["handler_type"] == "single_handler"
    assert router.span_attrs["handler_type"] == "router_with_fallback"
    assert _parity_span_attrs(direct) == _parity_span_attrs(router)
    assert direct.span_attrs["status"] == "ok"
    assert direct.span_attrs["cache_hit"] is True
    assert (
        _event_attrs_without_latency(direct)
        == _event_attrs_without_latency(router)
        == {
            "model": "gpt-4",
            "prompt_tokens": 1200,
            "completion_tokens": 345,
            "reasoning_tokens": 67,
            "cached_tokens": 89,
            "llm_cost": LLM_COST,
            "image_tokens": 0,
            "image_cost": 0.0,
            "image_count": 0,
            "prompt_name": DEFAULT_PROMPT_NAME,
        }
    )


@pytest.mark.asyncio
async def test_zero_usage_records_none_token_fields_on_both(span_exporter: InMemorySpanExporter) -> None:
    def zero_response() -> ParityResponse:
        return ParityResponse("gpt-4", prompt_tokens=0, completion_tokens=0, reasoning_tokens=0, cached_tokens=0)

    direct = await _run_direct(span_exporter, responses=[zero_response()], step=_make_step(), parameters={})
    router = await _run_router(span_exporter, responses=[zero_response()], step=_make_step(), parameters={})

    expected_step_update = {
        "task_id": "tsk_parity",
        "step_id": "stp_parity",
        "organization_id": "org_parity",
        "incremental_cost": LLM_COST,
        "incremental_input_tokens": None,
        "incremental_output_tokens": None,
        "incremental_reasoning_tokens": None,
        "incremental_cached_tokens": None,
        "last_llm_model": "gpt-4",
    }
    assert direct.update_step_calls == router.update_step_calls == [expected_step_update]
    assert direct.span_attrs["cache_hit"] is False
    assert _parity_span_attrs(direct) == _parity_span_attrs(router)


@pytest.mark.asyncio
async def test_provider_prefixed_response_model(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(
        span_exporter,
        responses=[ParityResponse("vertex_ai/gemini-2.5-flash")],
        model_name="gemini-2.5-flash",
        step=_make_step(),
        parameters={},
    )
    router = await _run_router(
        span_exporter,
        responses=[ParityResponse("vertex_ai/gemini-2.5-flash")],
        main_model_group="vertex-gemini-2.5-flash",
        litellm_model="vertex_ai/gemini-2.5-flash",
        step=_make_step(),
        parameters={},
    )

    assert direct.error is None and router.error is None
    # Both variants normalize the persisted model to the bare name.
    assert direct.update_step_calls[0]["last_llm_model"] == "gemini-2.5-flash"
    assert router.update_step_calls[0]["last_llm_model"] == "gemini-2.5-flash"

    # drift: the router span/event record the raw provider-prefixed response.model while
    # the direct handler records the normalized bare name.
    assert direct.span_attrs["llm_model"] == "gemini-2.5-flash"
    assert direct.event_attrs["model"] == "gemini-2.5-flash"
    assert router.span_attrs["llm_model"] == "vertex_ai/gemini-2.5-flash"
    assert router.event_attrs["model"] == "vertex_ai/gemini-2.5-flash"


@pytest.mark.parametrize(
    "make_error, expected_type, expected_status",
    [
        pytest.param(
            lambda: litellm.exceptions.APIError(500, "boom", "openai", "gpt-4"),
            LLMProviderErrorRetryableTask,
            "error",
            id="api-error",
        ),
        pytest.param(
            lambda: litellm.exceptions.RateLimitError("rl", "openai", "gpt-4"),
            LLMProviderError,
            "rate_limited",
            id="rate-limit",
        ),
        pytest.param(
            lambda: litellm.exceptions.ContextWindowExceededError("ctx", "gpt-4", "openai"),
            SkyvernContextWindowExceededError,
            "context_exceeded",
            id="context-window",
        ),
        pytest.param(lambda: RuntimeError("kaput"), LLMProviderError, "error", id="unexpected"),
        pytest.param(lambda: CancelledError(), CancelledError, "cancelled", id="cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_exception_mapping_parity(
    span_exporter: InMemorySpanExporter,
    make_error: Any,
    expected_type: type[BaseException],
    expected_status: str,
) -> None:
    direct = await _run_direct(span_exporter, responses=[make_error()], parameters={})
    router = await _run_router(span_exporter, responses=[make_error()], parameters={})

    assert type(direct.error) is expected_type
    assert type(router.error) is expected_type
    assert direct.span_attrs.get("status") == expected_status
    assert router.span_attrs.get("status") == expected_status


@pytest.mark.asyncio
async def test_value_error_mapping_drift(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(span_exporter, responses=[ValueError("token limit")], parameters={})
    router = await _run_router(span_exporter, responses=[ValueError("token limit")], parameters={})

    # drift: ValueError from the LLM call maps to LLMProviderErrorRetryableTask on the
    # router variant (dedicated except clause) but plain LLMProviderError on the direct one.
    assert type(direct.error) is LLMProviderError
    assert type(router.error) is LLMProviderErrorRetryableTask
    assert direct.span_attrs.get("status") == "error"
    assert router.span_attrs.get("status") == "error"


@pytest.mark.asyncio
async def test_empty_content_raises_invalid_response_format_on_both(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(span_exporter, responses=[ParityResponse("gpt-4", content=None)], parameters={})
    router = await _run_router(span_exporter, responses=[ParityResponse("gpt-4", content=None)], parameters={})

    assert type(direct.error) is InvalidLLMResponseFormat
    assert type(router.error) is InvalidLLMResponseFormat


@pytest.mark.asyncio
async def test_screenshots_forwarded_when_vision_supported(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(
        span_exporter, responses=[ParityResponse("gpt-4")], screenshots=list(SCREENSHOTS), parameters={}
    )
    router = await _run_router(
        span_exporter, responses=[ParityResponse("gpt-4")], screenshots=list(SCREENSHOTS), parameters={}
    )

    assert direct.builder_screenshots == router.builder_screenshots == [SCREENSHOTS]
    for outcome in (direct, router):
        assert outcome.span_attrs["screenshots_included"] is True
        assert outcome.span_attrs["screenshot_count"] == 2
    assert _parity_span_attrs(direct) == _parity_span_attrs(router)


@pytest.mark.asyncio
async def test_screenshots_dropped_for_non_vision_config(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(
        span_exporter,
        responses=[ParityResponse("gpt-4")],
        supports_vision=False,
        screenshots=list(SCREENSHOTS),
        parameters={},
    )
    router = await _run_router(
        span_exporter,
        responses=[ParityResponse("gpt-4")],
        supports_vision=False,
        screenshots=list(SCREENSHOTS),
        parameters={},
    )

    assert direct.builder_screenshots == router.builder_screenshots == [None]
    for outcome in (direct, router):
        assert outcome.span_attrs["screenshots_included"] is False
        assert outcome.span_attrs["screenshot_count"] == 0
    assert _parity_span_attrs(direct) == _parity_span_attrs(router)


@pytest.mark.asyncio
async def test_vertex_cache_attached_for_gemini_extract_actions(span_exporter: InMemorySpanExporter) -> None:
    cache_name = "projects/123/locations/us-central1/cachedContents/456"

    def cached_context() -> SkyvernContext:
        return SkyvernContext(vertex_cache_name=cache_name, use_prompt_caching=True)

    direct = await _run_direct(
        span_exporter,
        responses=[ParityResponse("vertex_ai/gemini-2.5-flash")],
        model_name="gemini-2.5-flash",
        context=cached_context(),
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        parameters={},
    )
    router = await _run_router(
        span_exporter,
        responses=[ParityResponse("vertex_ai/gemini-2.5-flash")],
        main_model_group="vertex-gemini-2.5-flash",
        litellm_model="vertex_ai/gemini-2.5-flash",
        context=cached_context(),
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        parameters={},
    )

    assert direct.error is None and router.error is None
    # Both variants attach the cache and call litellm.acompletion directly — the router
    # variant bypasses its Router client for the cached primary call.
    assert len(direct.completion_calls) == 1
    assert len(router.completion_calls) == 1 and not router.router_calls
    assert direct.completion_calls[0]["cached_content"] == cache_name
    assert router.completion_calls[0]["cached_content"] == cache_name
    assert direct.completion_calls[0]["model"] == "gemini-2.5-flash"
    assert router.completion_calls[0]["model"] == "vertex_ai/gemini-2.5-flash"
    assert direct.completion_calls[0]["timeout"] == settings.LLM_CONFIG_TIMEOUT
    assert router.completion_calls[0]["timeout"] == settings.LLM_CONFIG_TIMEOUT


@pytest.mark.asyncio
async def test_no_vertex_cache_when_prompt_caching_disabled(span_exporter: InMemorySpanExporter) -> None:
    cache_name = "projects/123/locations/us-central1/cachedContents/456"

    def uncached_context() -> SkyvernContext:
        return SkyvernContext(vertex_cache_name=cache_name, use_prompt_caching=False)

    direct = await _run_direct(
        span_exporter,
        responses=[ParityResponse("vertex_ai/gemini-2.5-flash")],
        model_name="gemini-2.5-flash",
        context=uncached_context(),
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        parameters={},
    )
    router = await _run_router(
        span_exporter,
        responses=[ParityResponse("vertex_ai/gemini-2.5-flash")],
        main_model_group="vertex-gemini-2.5-flash",
        litellm_model="vertex_ai/gemini-2.5-flash",
        context=uncached_context(),
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        parameters={},
    )

    assert direct.error is None and router.error is None
    assert len(direct.completion_calls) == 1
    assert "cached_content" not in direct.completion_calls[0]
    assert len(router.router_calls) == 1 and not router.completion_calls
    assert "cached_content" not in router.router_calls[0]


@pytest.mark.asyncio
async def test_openai_static_prompt_injection_drift(span_exporter: InMemorySpanExporter) -> None:
    static_prompt = "This is the extract-action-static prompt content"

    def caching_context() -> SkyvernContext:
        return SkyvernContext(cached_static_prompt=static_prompt, use_prompt_caching=True)

    direct = await _run_direct(
        span_exporter,
        responses=[ParityResponse("gpt-4")],
        context=caching_context(),
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        parameters={},
    )
    router = await _run_router(
        span_exporter,
        responses=[ParityResponse("gpt-4")],
        context=caching_context(),
        prompt_name=EXTRACT_ACTION_PROMPT_NAME,
        parameters={},
    )

    assert direct.error is None and router.error is None
    # drift: the OpenAI cached_static_prompt system-message injection is unreachable on
    # the router variant (isinstance(llm_config, LLMConfig) gate); only the direct
    # variant prepends the cached system message.
    direct_messages = direct.completion_calls[0]["messages"]
    assert direct_messages[0]["role"] == "system"
    assert direct_messages[0]["content"][0]["text"] == static_prompt
    assert router.router_calls[0]["messages"] == [{"role": "user", "content": PROMPT}]


@pytest.mark.asyncio
async def test_truncated_response_direct_raises_output_truncated(span_exporter: InMemorySpanExporter) -> None:
    truncated = ParityResponse("gpt-4", content=None, finish_reason="length")
    direct = await _run_direct(span_exporter, responses=[truncated], parameters={})

    assert type(direct.error) is LLMOutputTruncatedError
    assert direct.error.model == "gpt-4"
    assert direct.error.prompt_tokens == 1200
    assert direct.error.completion_tokens == 345
    assert direct.error.reasoning_tokens == 67
    assert len(direct.completion_calls) == 1


@pytest.mark.asyncio
async def test_truncated_response_router_retries_on_fallback(span_exporter: InMemorySpanExporter) -> None:
    truncated = ParityResponse("gpt-4", content=None, finish_reason="length")
    router = await _run_router(
        span_exporter,
        responses=[truncated, ParityResponse("gpt-4-fallback")],
        fallback_groups=("gpt-4-fallback",),
        parameters={"max_completion_tokens": 4096, "temperature": 0.0},
    )

    assert router.error is None
    assert router.parsed == {"actions": []}
    assert len(router.router_calls) == 2
    assert router.router_calls[0]["model"] == "gpt-4"
    assert router.router_calls[0]["max_completion_tokens"] == 4096
    assert router.router_calls[1]["model"] == "gpt-4-fallback"
    # The truncation retry strips output-size caps but keeps the other parameters.
    assert "max_completion_tokens" not in router.router_calls[1]
    assert "max_tokens" not in router.router_calls[1]
    assert router.router_calls[1]["temperature"] == 0.0
    assert router.span_attrs["llm_model"] == "gpt-4-fallback"


@pytest.mark.asyncio
async def test_truncated_fallback_exhausted_wraps_error_on_router(span_exporter: InMemorySpanExporter) -> None:
    def truncated() -> ParityResponse:
        return ParityResponse("gpt-4", content=None, finish_reason="length")

    router = await _run_router(
        span_exporter,
        responses=[truncated(), truncated()],
        fallback_groups=("gpt-4-fallback",),
        parameters={},
    )

    # drift: when the router's truncation fallback is itself truncated, the
    # LLMOutputTruncatedError is swallowed by the generic except clause and re-raised as
    # LLMProviderError; the direct variant raises LLMOutputTruncatedError bare.
    assert type(router.error) is LLMProviderError
    assert type(router.error.__cause__) is LLMOutputTruncatedError
    assert router.span_attrs.get("status") == "error"
    assert len(router.router_calls) == 2


@pytest.mark.asyncio
async def test_prompt_breakdown_consumed_once_on_both(span_exporter: InMemorySpanExporter) -> None:
    def breakdown_context() -> SkyvernContext:
        return SkyvernContext(
            last_prompt_breakdown={
                "html_token_count": 12345,
                "total_tokens_local": 50000,
                "html_pct": 0.2469,
                "template_name": DEFAULT_PROMPT_NAME,
            }
        )

    direct_context = breakdown_context()
    router_context = breakdown_context()
    direct = await _run_direct(
        span_exporter, responses=[ParityResponse("gpt-4")], context=direct_context, parameters={}
    )
    router = await _run_router(
        span_exporter, responses=[ParityResponse("gpt-4")], context=router_context, parameters={}
    )

    assert direct.error is None and router.error is None
    assert direct_context.last_prompt_breakdown is None
    assert router_context.last_prompt_breakdown is None


@pytest.mark.asyncio
async def test_block_cost_persisted_on_both(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(
        span_exporter,
        responses=[ParityResponse("gpt-4")],
        workflow_run_block_id="wrb_parity",
        organization_id="org_parity",
        parameters={},
    )
    router = await _run_router(
        span_exporter,
        responses=[ParityResponse("gpt-4")],
        workflow_run_block_id="wrb_parity",
        organization_id="org_parity",
        parameters={},
    )

    expected_block_cost = {
        "workflow_run_block_id": "wrb_parity",
        "organization_id": "org_parity",
        "amount": LLM_COST,
    }
    assert direct.block_cost_calls == router.block_cost_calls == [expected_block_cost]


@pytest.mark.asyncio
async def test_thought_stats_recorded_identically(span_exporter: InMemorySpanExporter) -> None:
    direct = await _run_direct(
        span_exporter, responses=[ParityResponse("gpt-4")], thought=_make_thought(), parameters={}
    )
    router = await _run_router(
        span_exporter, responses=[ParityResponse("gpt-4")], thought=_make_thought(), parameters={}
    )

    expected_thought_update = {
        "thought_id": "ot_parity",
        "organization_id": "org_parity",
        "input_token_count": 1200,
        "output_token_count": 345,
        "reasoning_token_count": 67,
        "cached_token_count": 89,
        "thought_cost": LLM_COST,
        "last_llm_model": "gpt-4",
    }
    assert direct.update_thought_calls == router.update_thought_calls == [expected_thought_update]
