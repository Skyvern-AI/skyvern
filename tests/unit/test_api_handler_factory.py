from __future__ import annotations

import asyncio
import json
from asyncio import CancelledError
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import litellm  # type: ignore[import-not-found]
import openai
import pytest  # type: ignore[import-not-found]

from skyvern.forge.sdk.api.llm import api_handler_factory
from skyvern.forge.sdk.api.llm.api_handler_factory import (
    EXTRACT_ACTION_PROMPT_NAME,
    GEMINI_SAFETY_SETTINGS,
    LLMAPIHandlerFactory,
    LLMCaller,
    get_org_aware_secondary_llm_api_handler,
)
from skyvern.forge.sdk.api.llm.exceptions import LLMProviderErrorRetryableTask
from skyvern.forge.sdk.api.llm.models import LLMConfig
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.schemas.llm import LLMRouterConfig, LLMRouterModelConfig
from tests.unit.helpers import DummyLogger, FakeLLMResponse


def _custom_llm_config(model_name: str, api_base: str = "https://llm.example.test/v1") -> LLMConfig:
    return LLMConfig(model_name, [], False, False, {"api_key": "test-key", "api_base": api_base})


def test_render_hashed_href_map_blocks_lipsum_globals_command(tmp_path: Path) -> None:
    marker = tmp_path / "ssti_canary"
    payload = "{{ lipsum.__globals__['os'].popen('touch " + str(marker) + "').read() }}"

    result = api_handler_factory._render_hashed_href_map(payload, {})

    assert not marker.exists()
    assert result == payload


def test_render_hashed_href_map_blocks_string_class_subclasses_gadget(tmp_path: Path) -> None:
    marker = tmp_path / "ssti_canary"
    payload = (
        "{% for cls in ''.__class__.__mro__[1].__subclasses__() %}"
        "{% if cls.__name__ == 'catch_warnings' %}"
        f"{{{{ cls()._module.__builtins__['__import__']('os').system('touch {marker}') }}}}"
        "{% endif %}{% endfor %}"
    )

    result = api_handler_factory._render_hashed_href_map(payload, {})

    assert not marker.exists()
    assert result == payload


def test_render_hashed_href_map_blocks_cycler_globals_gadget(tmp_path: Path) -> None:
    marker = tmp_path / "ssti_canary"
    payload = f"{{{{ cycler.__init__.__globals__['os'].popen('touch {marker}').read() }}}}"

    result = api_handler_factory._render_hashed_href_map(payload, {})

    assert not marker.exists()
    assert result == payload


def test_render_hashed_href_map_replaces_only_hashed_url_placeholders_with_optional_spaces() -> None:
    first_key = f"_{'a' * 64}"
    second_key = f"_{'b' * 64}"
    first_url = "https://example.test/a/very/long/path?with=query&and=more-query-parameters"
    second_url = "https://example.test/another/path"
    hashed_href_map = {first_key: first_url, second_key: second_url}
    payload = (
        '{"message":"Use the links without changing ordinary prose.",'
        f'"links":["{{{{{first_key}}}}}","{{{{ {second_key} }}}}","{{{{{first_key}}}}}"],'
        '"template":"{{ unknown_key }}"}'
    )
    expected = (
        '{"message":"Use the links without changing ordinary prose.",'
        f'"links":["{first_url}","{second_url}","{first_url}"],'
        '"template":"{{ unknown_key }}"}'
    )

    assert api_handler_factory._render_hashed_href_map(f"{{{{{first_key}}}}}", hashed_href_map) == first_url
    assert api_handler_factory._render_hashed_href_map(payload, hashed_href_map) == expected


def test_render_hashed_href_map_removes_unknown_hash_and_preserves_other_placeholder() -> None:
    unknown_hash = f"_{'c' * 64}"
    other_placeholder = "ordinary prose with {{ unknown_key }} left intact"

    assert api_handler_factory._render_hashed_href_map(f"{{{{{unknown_hash}}}}}", {}) == ""
    assert api_handler_factory._render_hashed_href_map(other_placeholder, {}) == other_placeholder


def test_render_hashed_href_map_returns_malformed_template_unchanged() -> None:
    payload = "response with stray {{"

    result = api_handler_factory._render_hashed_href_map(payload, {})

    assert result == payload


def test_render_hashed_href_map_returns_self_calling_macro_unchanged() -> None:
    payload = "{% macro m() %}{{ m() }}{% endmacro %}{{ m() }}"

    result = api_handler_factory._render_hashed_href_map(payload, {})

    assert result == payload


def test_render_hashed_href_map_returns_expensive_templates_unchanged() -> None:
    range_payload = "{% for i in range(10000000) %}x{% endfor %}"
    multiplication_payload = "{{ 'a' * 1000000000 }}"

    assert api_handler_factory._render_hashed_href_map(range_payload, {}) == range_payload
    assert api_handler_factory._render_hashed_href_map(multiplication_payload, {}) == multiplication_payload


@pytest.mark.parametrize(
    ("model_name", "http_client_attribute"),
    [("openai/example-model", "_client"), ("ollama_chat/example-model", "client")],
)
@pytest.mark.asyncio
async def test_custom_llm_http_clients_do_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str,
    http_client_attribute: str,
) -> None:
    llm_config = _custom_llm_config(model_name)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_router_config", lambda _: False)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        api_handler_factory.SettingsManager.get_settings(), "ALLOW_CUSTOM_LLM_LOCAL_API_BASES", False, raising=False
    )
    validate_api_base = MagicMock(return_value="https://llm.example.test/v1")
    monkeypatch.setattr(api_handler_factory, "validate_fetch_url", validate_api_base)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda **_: 0.0)

    completion = AsyncMock(return_value=FakeLLMResponse(model_name))
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", completion)

    handler = LLMAPIHandlerFactory.get_llm_api_handler("CUSTOM_LLM_redirect_test")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    client = completion.await_args.kwargs["client"]
    http_client = getattr(client, http_client_attribute)
    assert http_client.follow_redirects is False
    assert http_client.is_closed is True
    validate_api_base.assert_called_once_with("https://llm.example.test/v1")
    if http_client_attribute == "client":
        retry_client = client.create_client(timeout=None, event_hooks=None)
        assert retry_client.follow_redirects is False
        await retry_client.aclose()


@pytest.mark.parametrize("request_error", [None, RuntimeError("provider failed")], ids=["success", "failure"])
@pytest.mark.asyncio
async def test_custom_openrouter_client_is_scoped_to_request(
    monkeypatch: pytest.MonkeyPatch, request_error: Exception | None
) -> None:
    llm_config = _custom_llm_config("openrouter/example-model", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    completion = MagicMock()
    completion.model_dump.return_value = {}
    client = MagicMock()
    client.max_retries = 0
    client.chat.completions.create = AsyncMock(return_value=completion, side_effect=request_error)
    client.close = AsyncMock()
    openai_client = MagicMock(return_value=client)
    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", openai_client)
    monkeypatch.setattr(api_handler_factory.litellm, "ModelResponse", MagicMock())

    caller = LLMCaller("CUSTOM_LLM_openrouter_redirect_test")
    assert caller.openai_client is None
    if request_error:
        with pytest.raises(RuntimeError, match="provider failed"):
            await caller._dispatch_llm_call(messages=[])
    else:
        await caller._dispatch_llm_call(messages=[])

    assert openai_client.call_args.kwargs["http_client"].follow_redirects is False
    assert openai_client.call_args.kwargs["max_retries"] == 0
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_openrouter_extra_body_reaches_async_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_routing = {
        "order": ["cloudflare", "parasail"],
        "allow_fallbacks": False,
        "quantizations": ["fp8"],
    }
    llm_config = LLMConfig(
        "openrouter/deepseek/deepseek-v4-flash-0731",
        [],
        supports_vision=False,
        add_assistant_prefix=False,
        max_completion_tokens=4096,
        temperature=0.2,
        reasoning_effort="medium",
        litellm_params={
            "extra_body": {
                "provider": provider_routing,
                "reasoning_effort": "high",
                "temperature": 0.1,
            }
        },
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)

    completion = MagicMock()
    completion.model_dump.return_value = {"provider": "Cloudflare", "service_tier": "standard"}
    client = MagicMock()
    client.max_retries = 0
    client.chat.completions.create = AsyncMock(return_value=completion)
    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", MagicMock(return_value=client))
    monkeypatch.setattr(api_handler_factory.litellm, "ModelResponse", MagicMock())

    caller = LLMCaller("OPENROUTER_DEEPSEEK_V4_FLASH_0731")
    active_parameters = LLMAPIHandlerFactory.get_api_parameters(llm_config)
    active_parameters.update(llm_config.litellm_params or {})
    await caller._dispatch_llm_call(messages=[{"role": "user", "content": "test"}], **active_parameters)

    request_kwargs = client.chat.completions.create.await_args.kwargs
    assert request_kwargs["extra_body"] == {"provider": provider_routing}
    assert request_kwargs["temperature"] == 0.2
    assert request_kwargs["reasoning_effort"] == "medium"


def _openrouter_caller(
    monkeypatch: pytest.MonkeyPatch,
    create: Any,
    *,
    llm_key: str = "OPENROUTER_EXAMPLE",
    max_retries: Any = 0,
) -> tuple[LLMCaller, MagicMock]:
    llm_config = _custom_llm_config("openrouter/example-model", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    client = MagicMock()
    client.max_retries = max_retries
    client.chat.completions.create = create
    client.close = AsyncMock()
    monkeypatch.setattr(api_handler_factory, "AsyncOpenAI", MagicMock(return_value=client))
    monkeypatch.setattr(api_handler_factory.litellm, "ModelResponse", MagicMock())
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        api_handler_factory,
        "llm_messages_builder_with_history",
        AsyncMock(return_value=[{"role": "user", "content": "test"}]),
    )
    return LLMCaller(llm_key), client


def _set_hard_deadline_settings(monkeypatch: pytest.MonkeyPatch, *, enforce: bool, grace: float) -> None:
    monkeypatch.setattr(api_handler_factory.settings, "ENFORCE_LLM_HARD_DEADLINE", enforce)
    monkeypatch.setattr(api_handler_factory.settings, "LLM_HARD_DEADLINE_GRACE_SECONDS", grace)
    monkeypatch.setattr(api_handler_factory, "_hard_deadline_disabled_reasons", set())


@pytest.mark.parametrize(
    ("enforce", "timeout", "attempts", "expected"),
    [
        (True, 300, 1, 310.0),
        (True, 300, 3, 910.0),
        (True, 300.5, 1, 310.5),
        (False, 300, 1, None),
        (True, 300, None, None),
        (True, None, 1, None),
        (True, 0, 1, None),
        (True, -1, 1, None),
        (True, "300", 1, None),
        (True, True, 1, None),
    ],
)
def test_llm_hard_deadline_seconds_only_extends_a_usable_timeout(
    monkeypatch: pytest.MonkeyPatch, enforce: bool, timeout: Any, attempts: int | None, expected: float | None
) -> None:
    _set_hard_deadline_settings(monkeypatch, enforce=enforce, grace=10.0)

    assert api_handler_factory._llm_hard_deadline_seconds(timeout, attempts) == expected


@pytest.mark.parametrize(
    ("max_retries", "expected"),
    [(0, 1), (2, 3), (None, None), (-1, None), (True, None), ("2", None)],
)
def test_openai_client_attempts_reads_the_sdk_retry_budget(
    monkeypatch: pytest.MonkeyPatch, max_retries: Any, expected: int | None
) -> None:
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)
    client = SimpleNamespace(max_retries=max_retries)

    assert api_handler_factory._openai_client_attempts(client) == expected
    if expected is None:
        assert "client_max_retries" in api_handler_factory._hard_deadline_disabled_reasons


@pytest.mark.asyncio
async def test_hard_deadline_surfaces_as_retryable_from_the_public_call(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_call_cancelled = asyncio.Event()

    async def _drip_forever(**kwargs: Any) -> Any:
        try:
            while True:
                await asyncio.sleep(0.01)
        except CancelledError:
            provider_call_cancelled.set()
            raise

    caller, _ = _openrouter_caller(monkeypatch, _drip_forever)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=0.05)

    with pytest.raises(LLMProviderErrorRetryableTask):
        await caller.call(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME, timeout=0.05)

    assert provider_call_cancelled.is_set()


@pytest.mark.asyncio
async def test_hard_deadline_on_a_custom_openrouter_client_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang(**kwargs: Any) -> Any:
        await asyncio.sleep(60)

    caller, client = _openrouter_caller(monkeypatch, _hang, llm_key="CUSTOM_LLM_openrouter_deadline_test")
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=0.05)

    with pytest.raises(LLMProviderErrorRetryableTask):
        await caller.call(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME, timeout=0.05)

    client.close.assert_awaited_once()
    assert api_handler_factory.AsyncOpenAI.call_args.kwargs["max_retries"] == 0


@pytest.mark.asyncio
async def test_hard_deadline_scales_with_the_client_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that retries twice may legitimately spend three timeouts, so 0.2s must survive a
    0.1s timeout: the deadline covers 3 attempts, not one."""

    async def _slower_than_one_attempt(**kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return SimpleNamespace(model_dump=lambda: {})

    caller, _ = _openrouter_caller(monkeypatch, _slower_than_one_attempt, max_retries=2)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=0.0)

    await caller._dispatch_llm_call(messages=[], timeout=0.1)


@pytest.mark.asyncio
async def test_a_timeout_raised_inside_the_request_is_not_relabelled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _proxy_timeout(**kwargs: Any) -> Any:
        raise TimeoutError("connection to proxy timed out")

    caller, _ = _openrouter_caller(monkeypatch, _proxy_timeout)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)

    with pytest.raises(TimeoutError, match="connection to proxy timed out") as raised:
        await caller._dispatch_llm_call(messages=[], timeout=300)

    assert not isinstance(raised.value, api_handler_factory.LLMProviderError)


@pytest.mark.asyncio
async def test_cancellation_landing_with_the_response_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run-stop that arrives in the same tick the response resolves must still stop the run.
    Releasing the response and cancelling before the loop resumes the task pins that race."""
    provider_call_started = asyncio.Event()
    release_response = asyncio.Event()

    async def _responds_when_released(**kwargs: Any) -> Any:
        provider_call_started.set()
        await release_response.wait()
        return SimpleNamespace(model_dump=lambda: {})

    caller, _ = _openrouter_caller(monkeypatch, _responds_when_released)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)

    dispatch = asyncio.create_task(caller._dispatch_llm_call(messages=[], timeout=300))
    await provider_call_started.wait()
    release_response.set()
    dispatch.cancel()

    with pytest.raises(CancelledError):
        await dispatch


@pytest.mark.asyncio
async def test_hard_deadline_does_not_convert_an_external_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    provider_call_started = asyncio.Event()

    async def _hang(**kwargs: Any) -> Any:
        provider_call_started.set()
        await asyncio.sleep(60)

    caller, _ = _openrouter_caller(monkeypatch, _hang)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)

    dispatch = asyncio.create_task(caller._dispatch_llm_call(messages=[], timeout=60))
    await provider_call_started.wait()
    dispatch.cancel()

    with pytest.raises(CancelledError):
        await dispatch


@pytest.mark.asyncio
async def test_hard_deadline_leaves_a_fast_call_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    completion = MagicMock()
    completion.model_dump.return_value = {"id": "chatcmpl-test"}
    create = AsyncMock(return_value=completion)
    caller, _ = _openrouter_caller(monkeypatch, create)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)

    await caller._dispatch_llm_call(messages=[{"role": "user", "content": "test"}], timeout=30)

    assert create.await_args.kwargs["timeout"] == 30
    api_handler_factory.litellm.ModelResponse.assert_called_once_with(id="chatcmpl-test")


@pytest.mark.asyncio
async def test_hard_deadline_disabled_lets_a_slow_call_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    completion = MagicMock()
    completion.model_dump.return_value = {}

    async def _slower_than_the_declared_timeout(**kwargs: Any) -> Any:
        await asyncio.sleep(0.05)
        return completion

    caller, _ = _openrouter_caller(monkeypatch, _slower_than_the_declared_timeout)
    _set_hard_deadline_settings(monkeypatch, enforce=False, grace=0.0)

    await caller._dispatch_llm_call(messages=[], timeout=0.001)

    completion.model_dump.assert_called_once()


@pytest.mark.asyncio
async def test_hard_deadline_disabled_skips_reading_the_client_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill switch should fully silence the deadline machinery: it must not even read (and
    potentially warn about) the client's retry budget when the flag is off."""
    completion = MagicMock()
    completion.model_dump.return_value = {}
    create = AsyncMock(return_value=completion)
    caller, client = _openrouter_caller(monkeypatch, create)
    client.max_retries = -1  # malformed; would log if _openai_client_attempts ever inspected it
    _set_hard_deadline_settings(monkeypatch, enforce=False, grace=10.0)

    await caller._dispatch_llm_call(messages=[], timeout=30)

    assert api_handler_factory._hard_deadline_disabled_reasons == set()


@pytest.mark.asyncio
async def test_unparseable_response_body_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _whitespace_only_200(**kwargs: Any) -> Any:
        raise json.JSONDecodeError("Expecting value", "           ", 0)

    caller, _ = _openrouter_caller(monkeypatch, _whitespace_only_200)
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)

    with pytest.raises(LLMProviderErrorRetryableTask):
        await caller.call(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)


def _openai_rate_limit_error() -> Exception:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.RateLimitError("Too Many Requests", response=httpx.Response(429, request=request), body=None)


def _litellm_rate_limit_error() -> Exception:
    return litellm.exceptions.RateLimitError("Too Many Requests", llm_provider="openrouter", model="example-model")


@pytest.mark.parametrize(
    "make_rate_limit_error", [_openai_rate_limit_error, _litellm_rate_limit_error], ids=["openai", "litellm"]
)
@pytest.mark.asyncio
async def test_a_rate_limited_custom_client_call_is_retryable(
    monkeypatch: pytest.MonkeyPatch, make_rate_limit_error: Any
) -> None:
    """max_retries=0 took away the SDK's silent retries on this path, so a 429 has to reach the
    step-level retry rather than failing the step outright."""

    async def _rate_limited(**kwargs: Any) -> Any:
        raise make_rate_limit_error()

    caller, _ = _openrouter_caller(monkeypatch, _rate_limited, llm_key="CUSTOM_LLM_openrouter_rate_limit_test")
    _set_hard_deadline_settings(monkeypatch, enforce=True, grace=10.0)

    with pytest.raises(LLMProviderErrorRetryableTask):
        await caller.call(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)


def test_json_error_body_length_tolerates_a_doc_less_error() -> None:
    assert api_handler_factory._json_error_body_length(json.JSONDecodeError("Expecting value", "  ", 0)) == 2
    assert api_handler_factory._json_error_body_length(json.JSONDecodeError.__new__(json.JSONDecodeError)) is None


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (SimpleNamespace(provider="Cloudflare"), "Cloudflare"),
        (SimpleNamespace(_hidden_params={"custom_llm_provider": "openrouter"}), "openrouter"),
        (SimpleNamespace(_hidden_params={}), None),
        (SimpleNamespace(provider=None), None),
        (SimpleNamespace(provider=123), None),
        (SimpleNamespace(), None),
    ],
)
def test_response_provider_is_string_or_none(response: object, expected: str | None) -> None:
    assert api_handler_factory._response_provider(response) == expected


def test_copilot_model_usage_extraction_preserves_response_model_for_malformed_usage() -> None:
    class BrokenResponse:
        model = "gpt-4.1"

        @property
        def usage(self) -> None:
            raise RuntimeError("malformed usage")

    event = api_handler_factory._copilot_model_usage_event(
        BrokenResponse(),
        request_model="gpt-4",
        provider_name="openai",
        cost=None,
        prompt_name="workflow-copilot-narration",
    )

    assert event.log_fields() == {
        "log_code": "copilot_model_usage",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4",
        "gen_ai.response.model": "gpt-4.1",
        "gen_ai.provider.name": "openai",
        "copilot.prompt_name": "workflow-copilot-narration",
    }


def test_copilot_model_usage_logging_failure_cannot_fail_a_completed_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = MagicMock()
    logger.info.side_effect = RuntimeError("logger unavailable")
    logger.warning.side_effect = RuntimeError("logger unavailable")
    monkeypatch.setattr(api_handler_factory, "LOG", logger)

    api_handler_factory._emit_copilot_model_usage_for_response(
        FakeLLMResponse("gpt-4.1"),
        request_model="gpt-4",
        prompt_name="workflow-copilot-narration",
        cost=None,
    )


def test_copilot_model_usage_metadata_failure_cannot_fail_a_completed_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenResponse:
        @property
        def provider(self) -> str:
            raise RuntimeError("bad provider metadata")

    logger = DummyLogger()
    monkeypatch.setattr(api_handler_factory, "LOG", logger)

    api_handler_factory._emit_copilot_model_usage_for_response(
        BrokenResponse(),
        request_model="gpt-4",
        prompt_name="workflow-copilot-narration",
        cost=None,
    )

    assert any(event == "Failed to emit Copilot model usage" for event, _ in logger.warnings)


def _stub_successful_llm_caller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parse_error: Exception | None = None,
) -> tuple[LLMCaller, DummyLogger]:
    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        api_handler_factory,
        "llm_messages_builder_with_history",
        AsyncMock(return_value=[{"role": "user", "content": "test"}]),
    )
    response = FakeLLMResponse("openai/gpt-4.1")
    response.provider = "openai"
    response.usage.prompt_tokens = 7
    response.usage.completion_tokens = 3
    response.usage.prompt_tokens_details.cached_tokens = 0
    response.usage.prompt_tokens_details.cache_write_tokens = 2
    caller = LLMCaller("TEST_LLM_CALLER_USAGE")
    monkeypatch.setattr(caller, "_dispatch_llm_call", AsyncMock(return_value=response))
    monkeypatch.setattr(
        caller,
        "get_call_stats",
        AsyncMock(
            return_value=api_handler_factory.LLMCallStats(
                input_tokens=7,
                output_tokens=3,
                cached_tokens=0,
                reasoning_tokens=0,
                llm_cost=0.25,
                llm_cost_available=True,
            )
        ),
    )
    artifact_manager = MagicMock()
    artifact_manager.bulk_create_artifacts = AsyncMock()
    monkeypatch.setattr(api_handler_factory.app, "ARTIFACT_MANAGER", artifact_manager)
    if parse_error is None:
        monkeypatch.setattr(api_handler_factory, "parse_api_response", lambda *args: {"actions": []})
    else:
        monkeypatch.setattr(api_handler_factory, "parse_api_response", MagicMock(side_effect=parse_error))
    logger = DummyLogger()
    monkeypatch.setattr(api_handler_factory, "LOG", logger)
    return caller, logger


@pytest.mark.asyncio
async def test_llm_caller_emits_one_copilot_model_usage_event(monkeypatch: pytest.MonkeyPatch) -> None:
    caller, logger = _stub_successful_llm_caller(monkeypatch)

    result = await caller.call(prompt="test", prompt_name="workflow-copilot-page-evidence-vision")

    usage_events = [fields for _, fields in logger.events if fields.get("log_code") == "copilot_model_usage"]
    assert result == {"actions": []}
    assert usage_events == [
        {
            "log_code": "copilot_model_usage",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4",
            "gen_ai.response.model": "openai/gpt-4.1",
            "gen_ai.provider.name": "openai",
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.usage.output_tokens": 3,
            "gen_ai.usage.cache_read.input_tokens": 0,
            "gen_ai.usage.cache_creation.input_tokens": 2,
            "operation.cost": 0.25,
            "copilot.prompt_name": "workflow-copilot-page-evidence-vision",
        }
    ]


@pytest.mark.asyncio
async def test_llm_caller_emits_usage_when_final_response_parsing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller, logger = _stub_successful_llm_caller(monkeypatch, parse_error=ValueError("invalid response"))

    with pytest.raises(ValueError, match="invalid response"):
        await caller.call(prompt="test", prompt_name="workflow-copilot-page-evidence-vision")

    usage_events = [fields for _, fields in logger.events if fields.get("log_code") == "copilot_model_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["operation.cost"] == 0.25
    assert usage_events[0]["copilot.prompt_name"] == "workflow-copilot-page-evidence-vision"


@pytest.mark.asyncio
async def test_llm_caller_emits_usage_before_call_stats_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    caller, logger = _stub_successful_llm_caller(monkeypatch)
    monkeypatch.setattr(caller, "get_call_stats", AsyncMock(side_effect=RuntimeError("bad usage metadata")))

    with pytest.raises(RuntimeError, match="bad usage metadata"):
        await caller.call(prompt="test", prompt_name="workflow-copilot-page-evidence-vision")

    usage_events = [fields for _, fields in logger.events if fields.get("log_code") == "copilot_model_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["gen_ai.usage.input_tokens"] == 7
    assert "operation.cost" not in usage_events[0]


@pytest.mark.asyncio
async def test_anthropic_call_stats_treats_missing_cache_read_tokens_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_config = LLMConfig(
        model_name="claude-sonnet-4-6",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    caller = LLMCaller("ANTHROPIC_TEST")
    response = api_handler_factory.AnthropicMessage.model_validate(
        {
            "id": "msg_test",
            "content": [],
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "type": "message",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_input_tokens": None,
            },
        }
    )

    stats = await caller.get_call_stats(response)

    assert stats.cached_tokens == 0
    assert stats.llm_cost == pytest.approx((3.0 * 100 + 15.0 * 10) / 1_000_000)


@pytest.mark.asyncio
async def test_cached_content_not_added_for_non_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that cached_content is NOT added to non-Gemini models like GPT-4."""
    # Setup context with caching enabled
    context = MagicMock()
    context.vertex_cache_name = "projects/123/locations/us-central1/cachedContents/456"
    context.use_prompt_caching = True
    context.cached_static_prompt = "some static prompt"
    context.hashed_href_map = {}

    # Setup non-Gemini config
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
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    # Mock litellm.acompletion to capture the parameters
    completion_params = {}

    async def mock_acompletion(*args, **kwargs):
        completion_params.update(kwargs)
        return FakeLLMResponse("gpt-4")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    # Verify cached_content was NOT passed
    assert "cached_content" not in completion_params
    assert completion_params["model"] == "gpt-4"


@pytest.mark.asyncio
async def test_cached_content_added_for_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that cached_content IS added for Gemini models."""
    # Setup context with caching enabled
    context = MagicMock()
    context.vertex_cache_name = "projects/123/locations/us-central1/cachedContents/456"
    context.use_prompt_caching = True
    context.cached_static_prompt = "some static prompt"
    context.hashed_href_map = {}

    # Setup Gemini config
    llm_config = LLMConfig(
        model_name="gemini-1.5-pro",
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
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    # Mock litellm.acompletion to capture the parameters
    completion_params = {}

    async def mock_acompletion(*args, **kwargs):
        completion_params.update(kwargs)
        return FakeLLMResponse("gemini-1.5-pro")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gemini-1.5-pro")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    # Verify cached_content WAS passed
    assert "cached_content" in completion_params
    assert completion_params["cached_content"] == "projects/123/locations/us-central1/cachedContents/456"
    assert completion_params["model"] == "gemini-1.5-pro"


@pytest.mark.asyncio
async def test_openai_caching_not_injected_for_check_user_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OpenAI context caching system message is NOT injected for check-user-goal prompts.

    This is a regression test for a bug where the extract-action-static.j2 prompt was being
    injected as a system message for ALL prompts on OpenAI models, causing the LLM to return
    CLICK actions when running check-user-goal (which should only return COMPLETE/TERMINATE).
    """
    # Setup context with caching enabled (simulating state after extract-action ran)
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = True
    context.cached_static_prompt = "This is the extract-action-static prompt content"
    context.hashed_href_map = {}

    # Setup OpenAI config (GPT-4)
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

    # Capture messages passed to LLM
    captured_messages: list = []

    async def mock_llm_messages_builder(prompt, screenshots, add_assistant_prefix):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", mock_llm_messages_builder)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    async def mock_acompletion(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return FakeLLMResponse("gpt-4")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it with check-user-goal prompt (NOT extract-actions)
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    await handler(prompt="check-user-goal prompt content", prompt_name="check-user-goal")

    # Verify the cached_static_prompt was NOT injected as a system message
    # There should only be the user message, no system message with the cached content
    system_messages = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_messages) == 0, (
        f"Expected no system messages with cached content for check-user-goal, but found: {system_messages}"
    )


@pytest.mark.asyncio
async def test_openai_caching_injected_for_extract_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that OpenAI context caching system message IS injected for extract-actions prompts."""
    # Setup context with caching enabled
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = True
    context.cached_static_prompt = "This is the extract-action-static prompt content"
    context.hashed_href_map = {}

    # Setup OpenAI config (GPT-4)
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

    # Capture messages passed to LLM
    captured_messages: list = []

    async def mock_llm_messages_builder(prompt, screenshots, add_assistant_prefix):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", mock_llm_messages_builder)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    async def mock_acompletion(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return FakeLLMResponse("gpt-4")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Get handler and call it with extract-actions prompt
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    await handler(prompt="extract-actions prompt content", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    # Verify the cached_static_prompt WAS injected as a system message
    system_messages = [m for m in captured_messages if m.get("role") == "system"]
    assert len(system_messages) == 1, (
        f"Expected 1 system message with cached content for extract-actions, "
        f"but found {len(system_messages)}: {system_messages}"
    )
    # Check the system message contains the cached content
    system_content = system_messages[0].get("content", [])
    assert any(part.get("text") == "This is the extract-action-static prompt content" for part in system_content), (
        f"System message should contain cached_static_prompt, got: {system_content}"
    )


def test_normalize_llm_model_strips_provider_prefix() -> None:
    """LiteLLM returns model names with provider prefixes; dbt expects the bare name."""
    assert api_handler_factory._normalize_llm_model("vertex_ai/gemini-2.5-flash") == "gemini-2.5-flash"
    assert api_handler_factory._normalize_llm_model("openai/gpt-4.1-mini") == "gpt-4.1-mini"
    assert api_handler_factory._normalize_llm_model("gpt-4") == "gpt-4"
    assert api_handler_factory._normalize_llm_model(None) is None


@pytest.mark.parametrize(
    "model_name",
    [
        "anthropic/claude-opus-4-7",
        "anthropic/claude-opus-4-8",
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-5",
        "anthropic-claude-opus-4-8",
        "anthropic-claude-fable-5",
        "anthropic-claude-opus-5",
    ],
)
def test_requires_adaptive_thinking_for_direct_anthropic_models(model_name: str) -> None:
    assert LLMAPIHandlerFactory.requires_adaptive_thinking(model_name) is True


@pytest.mark.parametrize(
    "model_name",
    [
        "bedrock/us.anthropic.claude-opus-4-8",
        "bedrock/us.anthropic.claude-fable-5",
        "bedrock/us.anthropic.claude-opus-5",
        "anthropic/claude-sonnet-4-6",
        None,
    ],
)
def test_requires_adaptive_thinking_does_not_rewrite_other_providers(model_name: str | None) -> None:
    assert LLMAPIHandlerFactory.requires_adaptive_thinking(model_name) is False


@pytest.mark.parametrize(
    "model_name",
    [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-5",
    ],
)
def test_apply_anthropic_thinking_optimization_uses_adaptive_shape(model_name: str) -> None:
    llm_config = LLMConfig(
        model_name=model_name,
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    params: dict[str, Any] = {}

    LLMAPIHandlerFactory._apply_anthropic_thinking_optimization(
        params,
        new_budget=2048,
        llm_config=llm_config,
        prompt_name="workflow-copilot-request-policy",
    )

    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": LLMAPIHandlerFactory.ADAPTIVE_THINKING_EFFORT}


def test_assert_step_thought_block_exclusive_rejects_both_set() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        api_handler_factory._assert_step_thought_block_exclusive(MagicMock(), MagicMock(), None)


def test_assert_step_thought_block_exclusive_rejects_step_and_block() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        api_handler_factory._assert_step_thought_block_exclusive(MagicMock(), None, "wfb_123")


def test_assert_step_thought_block_exclusive_allows_single_or_neither() -> None:
    api_handler_factory._assert_step_thought_block_exclusive(None, None, None)
    api_handler_factory._assert_step_thought_block_exclusive(MagicMock(), None, None)
    api_handler_factory._assert_step_thought_block_exclusive(None, MagicMock(), None)
    api_handler_factory._assert_step_thought_block_exclusive(None, None, "wfb_123")


@pytest.mark.asyncio
async def test_handler_persists_response_model_not_router_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler must persist response.model (normalized), not the config key used to resolve the handler."""
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = False
    context.cached_static_prompt = None
    context.hashed_href_map = {}
    context.workflow_run_id = None
    context.task_id = None

    llm_config = LLMConfig(
        model_name="GEMINI_2_5_FLASH_WITH_FALLBACK",  # router group name, not what response.model returns
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
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.01)

    # LiteLLM returns the actual backing model with its provider prefix
    async def mock_acompletion(*args, **kwargs):
        return FakeLLMResponse("vertex_ai/gemini-2.5-flash")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    # Capture update_step kwargs to assert on the llm_model value
    captured_kwargs: dict = {}

    async def mock_update_step(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    artifact_manager = MagicMock()
    artifact_manager.prepare_llm_artifact = AsyncMock(return_value=None)
    artifact_manager.bulk_create_artifacts = AsyncMock()
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.app.ARTIFACT_MANAGER", artifact_manager)
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.app.DATABASE.tasks.update_step", mock_update_step
    )

    now = datetime.now()
    step = Step(
        created_at=now,
        modified_at=now,
        task_id="tsk_test",
        step_id="stp_test",
        status=StepStatus.running,
        order=0,
        is_last=False,
        retry_index=0,
        organization_id="org_test",
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler("GEMINI_2_5_FLASH_WITH_FALLBACK")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME, step=step)

    # The persisted model should be the bare response.model, not the router group key
    assert captured_kwargs.get("last_llm_model") == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_single_handler_maps_an_unparseable_body_to_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_config = LLMConfig(
        model_name="vertex_ai/gemini-2.5-flash",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_router_config", lambda _: False)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )

    async def _whitespace_only_200(*args: Any, **kwargs: Any) -> Any:
        raise json.JSONDecodeError("Expecting value", "     ", 0)

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", _whitespace_only_200)
    LLMAPIHandlerFactory._handler_cache.pop("TEST_UNPARSEABLE_SINGLE", None)

    handler = LLMAPIHandlerFactory.get_llm_api_handler("TEST_UNPARSEABLE_SINGLE")
    with pytest.raises(LLMProviderErrorRetryableTask):
        await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)


def test_aiohttp_transport_disabled_for_per_request_timeouts() -> None:
    """Importing the LLM package disables litellm's aiohttp transport so per-request `timeout` is honored."""
    assert litellm.disable_aiohttp_transport is True
    assert api_handler_factory.litellm.disable_aiohttp_transport is True


@pytest.mark.parametrize("override", [None, ""])
def test_get_override_llm_api_handler_treats_empty_as_no_override(
    override: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty override_llm_key must return default — not the dummy handler.

    Block models persist `llm_key=""` rather than NULL when the user hasn't picked a
    model; SKY-9674 narrowed the gate to `is None`, which routed those calls to
    `dummy_llm_api_handler` and broke text_prompt blocks on staging.
    """

    async def default_handler(*_: object, **__: object) -> dict[str, str]:
        return {"ok": True}

    monkeypatch.setattr(LLMAPIHandlerFactory, "_maybe_get_flex_handler", staticmethod(lambda _default: None))

    resolved = LLMAPIHandlerFactory.get_override_llm_api_handler(override, default=default_handler)
    assert resolved is default_handler


def test_get_org_aware_secondary_llm_api_handler_returns_org_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_handler = MagicMock(name="org_handler")
    registry_lookup = MagicMock(return_value=True)
    handler_lookup = MagicMock(return_value=org_handler)
    monkeypatch.setattr(
        api_handler_factory.skyvern_context,
        "current",
        lambda: SkyvernContext(
            organization_id="o_test",
            org_default_secondary_llm_key="CUSTOM_LLM_oat_fast",
        ),
    )
    monkeypatch.setattr(api_handler_factory, "is_custom_llm_owned_by_organization", lambda _id, _org: True)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_registered", registry_lookup)
    monkeypatch.setattr(api_handler_factory.LLMAPIHandlerFactory, "get_llm_api_handler", handler_lookup)

    result = get_org_aware_secondary_llm_api_handler(default=MagicMock(name="default_handler"))

    assert result is org_handler
    registry_lookup.assert_called_once_with("CUSTOM_LLM_oat_fast")
    handler_lookup.assert_called_once_with("CUSTOM_LLM_oat_fast")


def test_get_org_aware_secondary_llm_api_handler_warns_once_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_handler = MagicMock(name="default_handler")
    log = MagicMock()
    monkeypatch.setattr(
        api_handler_factory.skyvern_context,
        "current",
        lambda: SkyvernContext(org_default_secondary_llm_key="CUSTOM_LLM_oat_deleted"),
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_registered", MagicMock(return_value=False))
    monkeypatch.setattr(api_handler_factory, "LOG", log)

    result = get_org_aware_secondary_llm_api_handler(default=default_handler)

    assert result is default_handler
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["llm_key"] == "CUSTOM_LLM_oat_deleted"


def test_get_org_aware_secondary_llm_api_handler_is_safe_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_handler = MagicMock(name="platform_handler")
    registry_lookup = MagicMock(side_effect=AssertionError("registry must not be read without context"))
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_registered", registry_lookup)
    monkeypatch.setattr(api_handler_factory.app, "SECONDARY_LLM_API_HANDLER", platform_handler)

    result = get_org_aware_secondary_llm_api_handler()

    assert result is platform_handler
    registry_lookup.assert_not_called()


def test_get_override_llm_api_handler_falls_back_for_stale_org_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_handler = MagicMock(name="default_handler")
    log = MagicMock()
    monkeypatch.setattr(
        api_handler_factory.skyvern_context,
        "current",
        lambda: SkyvernContext(org_default_llm_key="CUSTOM_LLM_oat_deleted"),
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_registered", MagicMock(return_value=False))
    monkeypatch.setattr(api_handler_factory, "LOG", log)

    result = LLMAPIHandlerFactory.get_override_llm_api_handler(
        "CUSTOM_LLM_oat_deleted",
        default=default_handler,
    )

    assert result is default_handler
    log.warning.assert_called_once()


def test_get_override_llm_api_handler_rejects_foreign_org_custom_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_handler = MagicMock(name="default_handler")
    handler_lookup = MagicMock(side_effect=AssertionError("foreign custom key must not resolve"))
    log = MagicMock()
    monkeypatch.setattr(
        api_handler_factory.skyvern_context,
        "current",
        lambda: SkyvernContext(organization_id="o_attacker"),
    )
    monkeypatch.setattr(api_handler_factory, "is_custom_llm_owned_by_organization", lambda _id, _org: False)
    monkeypatch.setattr(api_handler_factory.LLMAPIHandlerFactory, "get_llm_api_handler", handler_lookup)
    monkeypatch.setattr(api_handler_factory, "LOG", log)

    result = LLMAPIHandlerFactory.get_override_llm_api_handler(
        "CUSTOM_LLM_oat_victim",
        default=default_handler,
    )

    assert result is default_handler
    handler_lookup.assert_not_called()
    log.warning.assert_called_once()
    assert log.warning.call_args.kwargs["organization_id"] == "o_attacker"


def test_get_override_llm_api_handler_rejects_custom_key_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_handler = MagicMock(name="default_handler")
    handler_lookup = MagicMock(side_effect=AssertionError("custom key must not resolve without an org"))
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(api_handler_factory.LLMAPIHandlerFactory, "get_llm_api_handler", handler_lookup)

    result = LLMAPIHandlerFactory.get_override_llm_api_handler(
        "CUSTOM_LLM_oat_orphan",
        default=default_handler,
    )

    assert result is default_handler
    handler_lookup.assert_not_called()


def test_get_org_aware_secondary_llm_api_handler_rejects_unowned_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_handler = MagicMock(name="default_handler")
    handler_lookup = MagicMock(side_effect=AssertionError("unowned org default must not resolve"))
    monkeypatch.setattr(
        api_handler_factory.skyvern_context,
        "current",
        lambda: SkyvernContext(
            organization_id="o_test",
            org_default_secondary_llm_key="CUSTOM_LLM_oat_foreign",
        ),
    )
    monkeypatch.setattr(api_handler_factory, "is_custom_llm_owned_by_organization", lambda _id, _org: False)
    monkeypatch.setattr(api_handler_factory.LLMAPIHandlerFactory, "get_llm_api_handler", handler_lookup)

    result = get_org_aware_secondary_llm_api_handler(default=default_handler)

    assert result is default_handler
    handler_lookup.assert_not_called()


@pytest.mark.parametrize("base_parameters", [None, {}])
def test_get_llm_api_handler_caches_plain_registered_config_without_parameters(
    monkeypatch: pytest.MonkeyPatch,
    base_parameters: dict[str, Any] | None,
) -> None:
    llm_key = "TEST_PLAIN_HANDLER_CACHE"
    llm_config = _custom_llm_config("openai/cache-model")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_router_config", lambda _: False)
    LLMAPIHandlerFactory._handler_cache.pop(llm_key, None)

    try:
        first = LLMAPIHandlerFactory.get_llm_api_handler(llm_key, base_parameters)
        second = LLMAPIHandlerFactory.get_llm_api_handler(llm_key, base_parameters)
    finally:
        LLMAPIHandlerFactory._handler_cache.pop(llm_key, None)

    assert second is first


def test_get_llm_api_handler_cache_self_invalidates_after_reregistration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_key = "TEST_PLAIN_HANDLER_CACHE_REREGISTER"
    current_config = _custom_llm_config("openai/first-cache-model")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: current_config)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_router_config", lambda _: False)
    LLMAPIHandlerFactory._handler_cache.pop(llm_key, None)

    try:
        first = LLMAPIHandlerFactory.get_llm_api_handler(llm_key)
        current_config = _custom_llm_config("openai/second-cache-model")
        second = LLMAPIHandlerFactory.get_llm_api_handler(llm_key)
    finally:
        LLMAPIHandlerFactory._handler_cache.pop(llm_key, None)

    assert second is not first


def test_get_llm_api_handler_does_not_cache_nonempty_base_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_key = "TEST_PARAMETERIZED_HANDLER_CACHE"
    llm_config = _custom_llm_config("openai/parameterized-cache-model")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "is_router_config", lambda _: False)
    LLMAPIHandlerFactory._handler_cache.pop(llm_key, None)

    first = LLMAPIHandlerFactory.get_llm_api_handler(llm_key, {"temperature": 0})
    second = LLMAPIHandlerFactory.get_llm_api_handler(llm_key, {"temperature": 0})

    assert second is not first
    assert llm_key not in LLMAPIHandlerFactory._handler_cache


# ---------------------------------------------------------------------------
# SKY-9785: Gemini 3 reasoning_effort experiment
# ---------------------------------------------------------------------------


def _gemini_3_flash_router() -> LLMRouterConfig:
    return LLMRouterConfig(
        model_name="gemini-3.0-flash-gpt-5-fallback-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="vertex-gemini-3-flash-preview",
                litellm_params={"model": "vertex_ai/gemini-3-flash-preview"},
            ),
        ],
        main_model_group="vertex-gemini-3-flash-preview",
        fallback_model_group="gpt-5-fallback",
    )


def _gemini_2_5_flash_router() -> LLMRouterConfig:
    return LLMRouterConfig(
        model_name="gemini-2.5-flash-gpt-5-mini-fallback-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="vertex-gemini-2.5-flash",
                litellm_params={"model": "vertex_ai/gemini-2.5-flash"},
            ),
        ],
        main_model_group="vertex-gemini-2.5-flash",
        fallback_model_group="gpt-5-mini-fallback",
    )


class TestGemini3ReasoningEffortExperiment:
    """SKY-9785 experiment. Grouped under a class so the autouse reset doesn't
    couple unrelated tests in this module to the new class-level override."""

    @pytest.fixture(autouse=True)
    def _reset_gemini_3_override(self) -> Any:
        """Make sure the class-level override doesn't leak between tests."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(None)
        yield
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(None)

    def test_is_gemini_3_model_detects_router_primary(self) -> None:
        """Router primary `main_model_group` carries the gemini-3 substring."""
        assert LLMAPIHandlerFactory._is_gemini_3_model(_gemini_3_flash_router()) is True

    def test_is_gemini_3_model_rejects_gemini_2(self) -> None:
        assert LLMAPIHandlerFactory._is_gemini_3_model(_gemini_2_5_flash_router()) is False

    def test_is_gemini_3_model_detects_direct_config(self) -> None:
        cfg = LLMConfig(
            model_name="vertex_ai/gemini-3-flash-preview",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        )
        assert LLMAPIHandlerFactory._is_gemini_3_model(cfg) is True

    def test_apply_gemini_thinking_optimization_uses_reasoning_effort_for_gemini_3(self) -> None:
        """With the override set, Gemini 3 calls switch to reasoning_effort and drop the
        legacy `thinking` payload that litellm silently discards for Gemini 3."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("medium")
        params: dict[str, Any] = {"max_completion_tokens": 65536}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params,
            new_budget=1024,
            llm_config=_gemini_3_flash_router(),
            prompt_name="extract-information-from-file-text",
        )
        assert params["reasoning_effort"] == "medium"
        assert "thinking" not in params

    def test_apply_gemini_thinking_optimization_strips_existing_thinking_for_gemini_3(self) -> None:
        """Sending both reasoning_effort and thinking_budget would 400 in litellm for
        Gemini 3 — the override path must clean up any stale `thinking` payload."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("low")
        params: dict[str, Any] = {"thinking": {"budget_tokens": 1024}}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_3_flash_router(), prompt_name="text-prompt"
        )
        assert params["reasoning_effort"] == "low"
        assert "thinking" not in params

    def test_apply_gemini_thinking_optimization_leaves_gemini_2_5_alone(self) -> None:
        """The experiment only rewrites Gemini 3 calls. Gemini 2.5 keeps the strict
        `thinking={budget_tokens:N}` path that Vertex 2.5 honors."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("low")
        params: dict[str, Any] = {}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_2_5_flash_router(), prompt_name="extract-actions"
        )
        assert "reasoning_effort" not in params
        assert params["thinking"]["budget_tokens"] == 1024

    def test_apply_gemini_thinking_optimization_control_leaves_gemini_3_alone(self) -> None:
        """Override unset (control arm) — Gemini 3 keeps today's behavior so we have a
        clean comparison baseline."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(None)
        params: dict[str, Any] = {}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_3_flash_router(), prompt_name="extract-actions"
        )
        assert "reasoning_effort" not in params
        assert params["thinking"]["budget_tokens"] == 1024

    @pytest.mark.parametrize("value", ["minimal", "low", "medium", "high", "MEDIUM", " low "])
    def test_set_gemini_3_reasoning_effort_override_accepts_valid_values(self, value: str) -> None:
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(value)
        assert LLMAPIHandlerFactory._gemini_3_reasoning_effort_override == value.strip().lower()

    @pytest.mark.parametrize("value", ["disable", "off", "high-er", 1024])
    def test_set_gemini_3_reasoning_effort_override_rejects_invalid_values(self, value: Any) -> None:
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override(value)
        assert LLMAPIHandlerFactory._gemini_3_reasoning_effort_override is None

    def test_apply_gemini_thinking_optimization_overrides_when_thinking_level_pre_merged(self) -> None:
        """Single-handler path (api_handler_factory.py:1402-1404) merges
        `llm_config.litellm_params` into parameters before optimization runs. For
        Gemini 3 configs this lifts `thinking_level="minimal"` into parameters and
        would otherwise trigger the early-return guard and silently skip the
        override. The reorder makes the override fire first."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("medium")
        # Simulate post-merge state from the single-handler path.
        params: dict[str, Any] = {
            "max_completion_tokens": 65536,
            "thinking_level": "minimal",
            "thinking": {"budget_tokens": 1024},
        }
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_3_flash_router(), prompt_name="extract-actions"
        )
        assert params["reasoning_effort"] == "medium"
        assert "thinking_level" not in params
        assert "thinking" not in params

    def test_apply_gemini_thinking_optimization_keeps_guard_for_gemini_2_5_with_thinking_level(self) -> None:
        """Override is set, but model is Gemini 2.5 — the override doesn't apply,
        and the legacy thinking_level guard fires as before (preserves the historic
        behavior that Gemini 2.5 routes with a thinking_level config field never
        get a budget_tokens write)."""
        LLMAPIHandlerFactory.set_gemini_3_reasoning_effort_override("medium")
        params: dict[str, Any] = {"thinking_level": "minimal"}
        LLMAPIHandlerFactory._apply_gemini_thinking_optimization(
            params, new_budget=1024, llm_config=_gemini_2_5_flash_router(), prompt_name="extract-actions"
        )
        # Override didn't fire (not gemini-3), guard fired, nothing else changed.
        assert "reasoning_effort" not in params
        assert "thinking" not in params
        assert params["thinking_level"] == "minimal"


class TestThinkingBudgetOptimization:
    @pytest.mark.parametrize("reasoning_effort", ["medium", "high"])
    def test_xai_reasoning_model_preserves_configured_reasoning_effort(
        self, reasoning_effort: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_handler_factory.litellm, "supports_reasoning", lambda model: True)

        llm_config = LLMConfig(
            model_name="xai/grok-4.5",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            reasoning_effort=reasoning_effort,
        )
        params = LLMAPIHandlerFactory.get_api_parameters(llm_config)

        LLMAPIHandlerFactory._apply_thinking_budget_optimization(
            params, new_budget=1024, llm_config=llm_config, prompt_name="extract-actions"
        )

        assert params["reasoning_effort"] == reasoning_effort

    @pytest.mark.parametrize("reasoning_effort", ["medium", "high"])
    def test_non_xai_reasoning_model_clamps_configured_reasoning_effort(
        self, reasoning_effort: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_handler_factory.litellm, "supports_reasoning", lambda model: True)

        llm_config = LLMConfig(
            model_name="gpt-5",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
            reasoning_effort=reasoning_effort,
        )
        params = LLMAPIHandlerFactory.get_api_parameters(llm_config)

        LLMAPIHandlerFactory._apply_thinking_budget_optimization(
            params, new_budget=1024, llm_config=llm_config, prompt_name="extract-actions"
        )

        assert params["reasoning_effort"] == "low"

    def test_other_reasoning_model_defaults_to_low_without_configured_reasoning_effort(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_handler_factory.litellm, "supports_reasoning", lambda model: True)

        llm_config = LLMConfig(
            model_name="deepseek/deepseek-reasoner",
            required_env_vars=[],
            supports_vision=True,
            add_assistant_prefix=False,
        )
        params = LLMAPIHandlerFactory.get_api_parameters(llm_config)

        LLMAPIHandlerFactory._apply_thinking_budget_optimization(
            params, new_budget=1024, llm_config=llm_config, prompt_name="extract-actions"
        )

        assert params["reasoning_effort"] == "low"


# SKY-10200 — runtime tests for the router timeout-precedence fix and per-hop
# fallback chain expansion. These complement the config-shape tests in
# tests/cloud/test_llm_router_fallback.py by pinning the api_handler_factory
# wiring: that the router is constructed with a default timeout, the call
# sites don't pass a per-call timeout kwarg (which would clobber per-deployment
# values), and the fallbacks list expands into per-hop entries.


def _make_three_tier_router_config(
    *,
    fallback_groups: list[str],
    redis_max_connections: int | None = None,
    litellm_models: dict[str, str] | None = None,
) -> LLMRouterConfig:
    """Synthetic 3+ tier router config that doesn't depend on the cloud
    `LLMConfigRegistry` registration that's conditional on prod env vars.
    `litellm_models` overrides the per-group litellm model string (flex-style
    routers point two groups at the same underlying model)."""
    litellm_models = litellm_models or {}
    deployments = [
        LLMRouterModelConfig(
            model_name="primary-group",
            litellm_params={"model": litellm_models.get("primary-group", "openai/primary"), "timeout": 60},
        ),
    ] + [
        LLMRouterModelConfig(
            model_name=group,
            litellm_params={"model": litellm_models.get(group, f"openai/{group}"), "timeout": 60},
        )
        for group in fallback_groups
    ]
    return LLMRouterConfig(
        model_name="test-router",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        model_list=deployments,
        redis_host="localhost",
        redis_port=6379,
        redis_password="",
        redis_max_connections=redis_max_connections,
        main_model_group="primary-group",
        fallback_model_group=fallback_groups,
        routing_strategy="simple-shuffle",
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )


def _stub_for_router_test(monkeypatch: pytest.MonkeyPatch, *, llm_key: str, config: LLMRouterConfig) -> None:
    """Wire a synthetic LLMRouterConfig into the registry and bypass env-var
    validation. Mirrors `router_test_context` from tests/unit/helpers.py."""
    from skyvern.forge.sdk.api.llm.config_registry import LLMConfigRegistry  # local import

    monkeypatch.setattr(LLMConfigRegistry, "validate_config", classmethod(lambda cls, key, cfg: None))
    LLMConfigRegistry._configs.pop(llm_key, None)  # type: ignore[attr-defined]
    LLMConfigRegistry.register_config(llm_key, config)
    LLMAPIHandlerFactory._router_handler_cache.pop(llm_key, None)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda completion_response: 0.0)

    async def fake_llm_messages_builder(prompt, screenshots, add_assistant_prefix):
        return [{"role": "user", "content": prompt}]

    monkeypatch.setattr(api_handler_factory, "llm_messages_builder", fake_llm_messages_builder)


def test_router_constructor_receives_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Router constructor must receive `timeout=settings.LLM_CONFIG_TIMEOUT`
    so deployments without an explicit per-deployment timeout fall back to this
    Router-level default (third precedence level per litellm/router.py
    _get_non_stream_timeout). Pre-fix this was passed at the per-call site
    instead, clobbering per-deployment values. SKY-10200 CORR-1."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["fallback-a", "fallback-b"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_ROUTER_DEFAULT_TIMEOUT", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_ROUTER_DEFAULT_TIMEOUT")

    assert captured.get("timeout") == api_handler_factory.settings.LLM_CONFIG_TIMEOUT, (
        f"Router must be constructed with timeout=settings.LLM_CONFIG_TIMEOUT; got timeout={captured.get('timeout')!r}"
    )


@pytest.mark.parametrize(
    ("redis_max_connections", "expected_cache_kwargs"),
    [(10, {"max_connections": 10}), (None, {})],
)
def test_router_constructor_receives_redis_connection_limit(
    monkeypatch: pytest.MonkeyPatch,
    redis_max_connections: int | None,
    expected_cache_kwargs: dict[str, int],
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["fallback"], redis_max_connections=redis_max_connections)
    _stub_for_router_test(monkeypatch, llm_key="TEST_REDIS_CONNECTION_POOL", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_REDIS_CONNECTION_POOL")

    assert captured["cache_kwargs"] == expected_cache_kwargs


def test_router_fallbacks_payload_expands_per_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """fallbacks=[{main: [a, b, c]}, {a: [b, c]}, {b: [c]}] — each non-terminal
    hop carries its own outgoing chain so secondary entry points (e.g.
    truncation retry at api_handler_factory.py:1119 which calls
    router.acompletion(model=fallback_groups[0])) also benefit from the
    remaining chain. SKY-10200 COMP-4."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["hop-a", "hop-b", "hop-c"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_FALLBACK_EXPANSION", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FALLBACK_EXPANSION")

    expected = [
        {"primary-group": ["hop-a", "hop-b", "hop-c"]},
        {"hop-a": ["hop-b", "hop-c"]},
        {"hop-b": ["hop-c"]},
    ]
    assert captured.get("fallbacks") == expected, (
        f"fallbacks payload must expand to per-hop entries; got {captured.get('fallbacks')!r}"
    )


def test_router_fallbacks_payload_single_hop_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """For a single-hop chain the expansion produces the same single-dict shape
    as the legacy payload — no behavior change for routers that don't have a
    deeper chain. SKY-10200 regression check."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["only-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_FALLBACK_SINGLE_HOP", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FALLBACK_SINGLE_HOP")

    assert captured.get("fallbacks") == [{"primary-group": ["only-fallback"]}], (
        f"single-hop fallbacks payload must match legacy single-dict shape; got {captured.get('fallbacks')!r}"
    )


def test_router_fallbacks_payload_empty_when_no_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fallback groups → empty fallbacks list. SKY-10200 regression check."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = LLMRouterConfig(
        model_name="test-router-no-fb",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(model_name="primary-group", litellm_params={"model": "openai/primary"}),
        ],
        redis_host="localhost",
        redis_port=6379,
        redis_password="",
        main_model_group="primary-group",
        fallback_model_group=None,
        routing_strategy="simple-shuffle",
        num_retries=0,
        disable_cooldowns=True,
        temperature=None,
    )
    _stub_for_router_test(monkeypatch, llm_key="TEST_FALLBACK_EMPTY", config=config)

    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FALLBACK_EMPTY")

    assert captured.get("fallbacks") == [], (
        f"no-fallback router must construct with empty fallbacks list; got {captured.get('fallbacks')!r}"
    )


@pytest.mark.asyncio
async def test_router_acompletion_does_not_pass_timeout_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler must NOT pass `timeout=` as a kwarg to router.acompletion;
    that would override per-deployment litellm_params['timeout'] per litellm
    precedence (litellm/router.py:_get_non_stream_timeout). SKY-10200 CORR-1."""

    captured_calls: list[dict[str, Any]] = []

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            self._main = kwargs.get("model_list", [{}])[0].get("model_name", "primary-group")

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            captured_calls.append({"model": model, "kwargs": dict(kwargs)})
            return FakeLLMResponse(model)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["hop-a", "hop-b"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_NO_TIMEOUT_KWARG", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_NO_TIMEOUT_KWARG")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert captured_calls, "router.acompletion was never invoked"
    for call in captured_calls:
        assert "timeout" not in call["kwargs"], (
            f"router.acompletion must not receive timeout= kwarg (it overrides per-deployment timeout); got call={call}"
        )


@pytest.mark.asyncio
async def test_router_handler_reports_an_unparseable_body_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    """The router ladder already treated this as retryable through its ValueError branch, which
    logs every such failure as a token limit. Keep the classification, name the real cause."""

    class _UnparseableRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> Any:
            raise json.JSONDecodeError("Expecting value", "     ", 0)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _UnparseableRouter)

    config = _make_three_tier_router_config(fallback_groups=[])
    _stub_for_router_test(monkeypatch, llm_key="TEST_UNPARSEABLE_ROUTER", config=config)
    logger = DummyLogger()
    monkeypatch.setattr(api_handler_factory, "LOG", logger)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_UNPARSEABLE_ROUTER")
    with pytest.raises(LLMProviderErrorRetryableTask):
        await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    assert "LLM response body was not parseable JSON" in [event for event, _ in logger.warnings]
    assert "LLM token limit exceeded" not in [event for event, _ in logger.exceptions]


@pytest.mark.asyncio
async def test_router_retries_content_filter_on_first_non_gemini_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini's non-configurable content filter blocks a PII-heavy prompt, returning a *valid*
    empty ModelResponse that litellm's exception-driven router fallback never recovers. Retrying
    on another Gemini tier hits the same block, so the handler must skip the Gemini
    standard-fallback and jump to the first NON-Gemini fallback group (SKY-11766)."""

    calls: list[str] = []
    fallback_kwargs: dict[str, Any] = {}

    class _FilterThenSucceedRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            if len(calls) == 1:
                return FakeLLMResponse("gemini-3.1-flash-lite", content=None, finish_reason="content_filter")
            fallback_kwargs.update(kwargs)
            return FakeLLMResponse("gpt-5-fallback", content='{"actions": []}')

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _FilterThenSucceedRouter)

    config = _make_three_tier_router_config(fallback_groups=["vertex-gemini-standard-fallback", "gpt-5-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_CONTENT_FILTER_FALLBACK", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_CONTENT_FILTER_FALLBACK")
    result = await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert calls == ["primary-group", "gpt-5-fallback"], (
        "handler must skip the Gemini standard-fallback tier and retry the first non-Gemini "
        f"fallback after a content_filter response; got calls={calls}"
    )
    assert result == {"actions": []}
    # The non-Gemini fallback call must not carry Gemini's safety_settings param — Azure 400s on
    # it and the fallback dies. get_api_parameters keeps it off router configs (per-deployment
    # injection instead), so **parameters stays clean here. Regression guard for incident #646.
    assert "safety_settings" not in fallback_kwargs, (
        f"non-Gemini fallback call must not carry safety_settings; got {fallback_kwargs}"
    )


@pytest.mark.asyncio
async def test_router_does_not_retry_content_filter_without_non_gemini_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every fallback group is also Gemini there is no filter-free tier to escape to — the
    content_filter must surface as a parse failure, not loop or retry another Gemini (SKY-11766)."""
    from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat

    calls: list[str] = []

    class _AlwaysFilterRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            return FakeLLMResponse("gemini-3.1-flash-lite", content=None, finish_reason="content_filter")

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _AlwaysFilterRouter)

    config = _make_three_tier_router_config(fallback_groups=["vertex-gemini-standard-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_CONTENT_FILTER_NO_NON_GEMINI", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_CONTENT_FILTER_NO_NON_GEMINI")
    with pytest.raises((EmptyLLMResponseError, InvalidLLMResponseFormat)):
        await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert calls == ["primary-group"], f"must not retry when there is no non-Gemini fallback; got calls={calls}"


@pytest.mark.asyncio
async def test_router_does_not_retry_content_filter_for_non_gemini_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch is scoped to Gemini's non-configurable filter. A content_filter from a
    non-Gemini model must not trigger the Gemini-specific fallback retry (SKY-11766)."""
    from skyvern.forge.sdk.api.llm.exceptions import EmptyLLMResponseError, InvalidLLMResponseFormat

    calls: list[str] = []

    class _FilterRouter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
            calls.append(model)
            return FakeLLMResponse("gpt-5", content=None, finish_reason="content_filter")

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _FilterRouter)

    config = _make_three_tier_router_config(fallback_groups=["gpt-5-mini-fallback"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_CONTENT_FILTER_NON_GEMINI_MODEL", config=config)

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_CONTENT_FILTER_NON_GEMINI_MODEL")
    with pytest.raises((EmptyLLMResponseError, InvalidLLMResponseFormat)):
        await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert calls == ["primary-group"], f"non-Gemini content_filter must not trigger Gemini fallback; got calls={calls}"


def test_router_fallback_chain_no_duplicate_keys_or_overlapping_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-hop fallback expansion is constructed as strict suffixes — each
    non-terminal hop appears as a key at most once and each entry's chain
    drops one head from the parent. Together these prevent litellm from
    retrying the same hop more than once in a single request. SKY-10200."""

    captured: dict[str, Any] = {}

    class _CapturingRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_handler_factory.litellm, "Router", _CapturingRouter)

    config = _make_three_tier_router_config(fallback_groups=["hop-a", "hop-b", "hop-c"])
    _stub_for_router_test(monkeypatch, llm_key="TEST_NO_DOUBLE_INVOCATION", config=config)
    LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_NO_DOUBLE_INVOCATION")

    fallbacks = captured.get("fallbacks", [])
    assert fallbacks, "fallbacks payload must be non-empty for a multi-hop chain"

    keys = [next(iter(entry.keys())) for entry in fallbacks]
    assert len(keys) == len(set(keys)), (
        f"each non-terminal hop must appear as a key at most once; got duplicates in {keys}. "
        "A repeated key would cause litellm to retry the same chain twice from that hop."
    )

    chains = [list(entry.values())[0] for entry in fallbacks]
    for i in range(1, len(chains)):
        assert chains[i] == chains[i - 1][1:], (
            f"each chain must drop one head from the previous (strict suffix); got chains={chains}. "
            "Non-suffix expansion could re-list already-tried hops and amplify retries."
        )


def test_completion_cost_halves_vertex_flex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vertex flex responses (trafficType ON_DEMAND_FLEX) bill at 50%: litellm reports
    them at the standard rate, so the helper applies the flex discount itself."""
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.10)

    flex = SimpleNamespace(_hidden_params={"provider_specific_fields": {"traffic_type": "ON_DEMAND_FLEX"}})
    standard = SimpleNamespace(_hidden_params={"provider_specific_fields": {"traffic_type": "ON_DEMAND"}})
    no_meta = SimpleNamespace(_hidden_params={})

    assert LLMAPIHandlerFactory.completion_cost_or_none(flex) == pytest.approx(0.05)
    assert LLMAPIHandlerFactory.completion_cost_or_none(standard) == pytest.approx(0.10)
    assert LLMAPIHandlerFactory.completion_cost_or_none(no_meta) == pytest.approx(0.10)


def test_completion_cost_halves_long_context_openai_direct_gpt5_6_flex(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flex-tagged OpenAI-direct GPT-5.6 call over the 272k-token threshold bills at
    litellm's untiered standard long-context rate (ModelInfo drops the *_flex threshold
    keys), so the helper halves it itself. Azure, short prompts, and standard tier are not."""
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: 0.10)

    long_context_flex = SimpleNamespace(
        service_tier="flex",
        usage=SimpleNamespace(prompt_tokens=300_000),
        _hidden_params={"litellm_model_name": "gpt-5.6-luna"},
    )
    short_prompt_flex = SimpleNamespace(
        service_tier="flex",
        usage=SimpleNamespace(prompt_tokens=200_000),
        _hidden_params={"litellm_model_name": "gpt-5.6-luna"},
    )
    azure_long_context_flex = SimpleNamespace(
        service_tier="flex",
        usage=SimpleNamespace(prompt_tokens=300_000),
        _hidden_params={"litellm_model_name": "azure/my-luna-deployment"},
    )
    standard_tier_long_context = SimpleNamespace(
        service_tier=None,
        usage=SimpleNamespace(prompt_tokens=300_000),
        _hidden_params={"litellm_model_name": "gpt-5.6-luna"},
    )

    assert LLMAPIHandlerFactory.completion_cost_or_none(long_context_flex) == pytest.approx(0.05)
    assert LLMAPIHandlerFactory.completion_cost_or_none(short_prompt_flex) == pytest.approx(0.10)
    assert LLMAPIHandlerFactory.completion_cost_or_none(azure_long_context_flex) == pytest.approx(0.10)
    assert LLMAPIHandlerFactory.completion_cost_or_none(standard_tier_long_context) == pytest.approx(0.10)


def test_completion_cost_returns_zero_when_litellm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(completion_response: Any) -> float:
        raise RuntimeError("provider unsupported")

    monkeypatch.setattr(litellm, "completion_cost", _raise)
    resp = SimpleNamespace(_hidden_params={"provider_specific_fields": {"traffic_type": "ON_DEMAND_FLEX"}})
    assert LLMAPIHandlerFactory.completion_cost_or_none(resp) is None


# OpenAI rejects tools + reasoning_effort on /v1/chat/completions for gpt-5.6, so
# litellm bridges those calls through /v1/responses and its response translation drops
# service_tier. completion_cost then misses the *_flex price keys and bills flex traffic at the
# standard rate. The tier is recovered from the deployment that served the call.

_TIER_TEST_MODEL = "gpt-5.6-unittest"
_TIER_TEST_STANDARD = {"input_cost_per_token": 1e-07, "output_cost_per_token": 6e-07}
_TIER_TEST_LONG_CONTEXT = {
    "input_cost_per_token_above_272k_tokens": 2e-07,
    "output_cost_per_token_above_272k_tokens": 9e-07,
}


@pytest.fixture
def tier_priced_model() -> Any:
    """A model registered with real flex price keys, so the cost assertions exercise litellm's
    own tier pricing instead of a stubbed completion_cost."""
    litellm.register_model(
        {
            _TIER_TEST_MODEL: {
                "litellm_provider": "openai",
                "mode": "chat",
                "supports_service_tier": True,
                **_TIER_TEST_STANDARD,
                **{f"{key}_flex": value / 2 for key, value in _TIER_TEST_STANDARD.items()},
                **_TIER_TEST_LONG_CONTEXT,
                **{f"{key}_flex": value / 2 for key, value in _TIER_TEST_LONG_CONTEXT.items()},
            }
        }
    )
    yield _TIER_TEST_MODEL
    litellm.model_cost.pop(_TIER_TEST_MODEL, None)
    # register_model also registers the name as an OpenAI chat model; popping the cost map alone
    # leaks it into every later test in the session.
    litellm.open_ai_chat_completion_models.discard(_TIER_TEST_MODEL)


def _bridge_response(
    *,
    model_id: str | None = "id:flex-leg",
    service_tier: str | None = None,
    prompt_tokens: int = 1000,
    reasoning_tokens: int = 0,
) -> litellm.ModelResponse:
    """A ModelResponse shaped like one that came back through litellm's Responses-API bridge:
    real usage and a serving deployment id, but no service_tier unless one is forced on."""
    response = litellm.ModelResponse(
        model=_TIER_TEST_MODEL,
        choices=[{"index": 0, "message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
    )
    response.usage = litellm.Usage(
        prompt_tokens=prompt_tokens, completion_tokens=1000, total_tokens=prompt_tokens + 1000
    )
    response.usage.completion_tokens_details = litellm.types.utils.CompletionTokensDetailsWrapper(
        reasoning_tokens=reasoning_tokens
    )
    response._hidden_params = {"litellm_model_name": _TIER_TEST_MODEL, "custom_llm_provider": "openai"}
    if model_id is not None:
        response._hidden_params["model_id"] = model_id
    if service_tier is not None:
        response.service_tier = service_tier
    return response


class _TieredRouter:
    """Router double whose deployments carry litellm_params the way litellm's do — the flex leg
    declares a tier as a pydantic extra, the fallback leg declares none."""

    def __init__(self) -> None:
        self.deployments = {
            "id:flex-leg": SimpleNamespace(
                model_name="openai-unittest-flex",
                litellm_params=litellm.types.router.LiteLLM_Params(model=_TIER_TEST_MODEL, service_tier="flex"),
            ),
            "id:fallback-leg": SimpleNamespace(
                model_name="openai-unittest-fallback",
                litellm_params=litellm.types.router.LiteLLM_Params(model=_TIER_TEST_MODEL),
            ),
            "id:mixed-case-flex-leg": SimpleNamespace(
                model_name="openai-unittest-flex",
                litellm_params=litellm.types.router.LiteLLM_Params(model=_TIER_TEST_MODEL, service_tier="Flex"),
            ),
            "id:vertex-style-leg": SimpleNamespace(
                model_name="vertex-unittest-flex",
                litellm_params=litellm.types.router.LiteLLM_Params(
                    model=_TIER_TEST_MODEL, service_tier="SERVICE_TIER_FLEX"
                ),
            ),
        }

    def get_deployment(self, model_id: str) -> Any:
        return self.deployments.get(model_id)


def test_served_tier_prefers_the_provider_over_the_deployment_it_ran_on(tier_priced_model: str) -> None:
    """A tier the provider reported is authoritative even when it contradicts the leg we
    dispatched on. Overwriting a downgrade would hide a real billing event and under-report."""
    response = _bridge_response(model_id="id:flex-leg", service_tier="default")

    tier, source = LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert (tier, source) == ("default", "reported")
    assert response.service_tier == "default"
    assert LLMAPIHandlerFactory.completion_cost_or_none(response) == pytest.approx(0.0007)


def test_dropped_tier_is_recovered_from_the_serving_deployment() -> None:
    response = _bridge_response(model_id="id:flex-leg")

    tier, source = LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert (tier, source) == ("flex", "inferred")
    assert api_handler_factory._effective_service_tier(response) == "flex"
    # Never written onto the response itself: that object is persisted as the LLM_RESPONSE
    # artifact, where a tier we synthesized would read as one the provider sent.
    assert getattr(response, "service_tier", None) is None


def test_unresolvable_deployment_is_reported_rather_than_passing_as_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing to resolve the serving deployment leaves the call priced at the standard rate,
    which is exactly the bug's signature — it must not be indistinguishable from a real one."""
    logger = DummyLogger()
    monkeypatch.setattr(api_handler_factory, "LOG", logger)
    response = _bridge_response(model_id=None)

    tier, source = LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert (tier, source) == (None, "unresolved")
    assert any(
        event == "Router response carried no service tier and no resolvable deployment" for event, _ in logger.events
    )


def test_bridge_flex_call_is_costed_at_the_flex_rate_and_fallback_at_the_standard_rate(
    tier_priced_model: str,
) -> None:
    """The headline accounting fix, against the real litellm price map: a flex-served call must
    cost half a fallback-served one, where today both cost the same."""
    router = _TieredRouter()
    flex = _bridge_response(model_id="id:flex-leg")
    fallback = _bridge_response(model_id="id:fallback-leg")

    LLMAPIHandlerFactory._record_served_service_tier(router, flex)
    LLMAPIHandlerFactory._record_served_service_tier(router, fallback)

    flex_cost = LLMAPIHandlerFactory.completion_cost_or_none(flex)
    fallback_cost = LLMAPIHandlerFactory.completion_cost_or_none(fallback)
    assert fallback_cost == pytest.approx(0.0007)
    assert flex_cost == pytest.approx(0.00035)


def test_a_tier_litellm_cannot_price_is_not_recorded(tier_priced_model: str) -> None:
    """Vertex deployments declare `service_tier="SERVICE_TIER_FLEX"`, which litellm does not map to
    a price key — its flex tier travels as `provider_specific_fields.traffic_type` and is corrected
    separately. Recording it would buy no discount and would put the working Vertex path through
    this one."""
    response = _bridge_response(model_id="id:vertex-style-leg")

    tier, source = LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert tier is None
    assert api_handler_factory._effective_service_tier(response) is None


def test_a_recovered_tier_is_normalised_to_lower_case(tier_priced_model: str) -> None:
    """litellm lower-cases before picking a price key, but our own >272k correction compares
    exactly — so a deployment written as "Flex" would take the short-call discount and silently
    lose the long-context halving, and would split the Datadog dimension in two."""
    short = _bridge_response(model_id="id:mixed-case-flex-leg")
    long_context = _bridge_response(model_id="id:mixed-case-flex-leg", prompt_tokens=300_000)

    assert LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), short) == ("flex", "inferred")
    LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), long_context)

    assert LLMAPIHandlerFactory.completion_cost_or_none(long_context) == pytest.approx(0.03045)


def test_a_reported_tier_outranks_a_previously_recovered_one(tier_priced_model: str) -> None:
    """Pins the precedence itself, not just the write guard: with both values present on the same
    response, the provider's must win. Inverting the order in `_effective_service_tier` would
    otherwise stay green, because recording never produces both and no other test builds the pair.
    Only the priced value is asserted — a response carrying both is unreachable in production, so
    what the log would say about it is not a contract worth freezing."""
    response = _bridge_response(model_id="id:flex-leg")
    LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)
    assert api_handler_factory._effective_service_tier(response) == "flex"

    response.service_tier = "default"

    assert api_handler_factory._effective_service_tier(response) == "default"


def test_a_deployment_with_no_tier_reports_no_provenance(tier_priced_model: str) -> None:
    """A leg that declares no tier inferred nothing, so it must not claim "inferred". Most router
    traffic is that shape — every Gemini router call among it — and labelling it would leave
    `service_tier_source="inferred"` on millions of calls carrying no tier, which is the query
    that is supposed to isolate the recovered ones."""
    response = _bridge_response(model_id="id:fallback-leg")

    tier, source = LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert (tier, source) == (None, None)
    assert api_handler_factory._service_tier_with_provenance(response) == (None, None)


def test_recovered_tier_does_not_double_discount_a_long_context_call(tier_priced_model: str) -> None:
    """Pins the composite of litellm's pricing and Skyvern's >272k correction. litellm cannot
    resolve the combined above-272k + flex price key today, so the correction supplies the
    halving; if a litellm release starts resolving it, this fails instead of quietly billing
    a quarter of the real cost."""
    long_context = _bridge_response(model_id="id:flex-leg", prompt_tokens=300_000)
    standard_reference = _bridge_response(model_id="id:fallback-leg", prompt_tokens=300_000)

    LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), long_context)

    assert api_handler_factory._effective_service_tier(long_context) == "flex"
    # litellm still prices the long-context flex call at the untiered rate, even when handed the
    # tier explicitly — it cannot resolve the combined above-272k + flex key...
    assert litellm.completion_cost(completion_response=long_context, service_tier="flex") == pytest.approx(0.0609)
    # ...so exactly one halving reaches the books.
    assert LLMAPIHandlerFactory.completion_cost_or_none(long_context) == pytest.approx(0.03045)
    assert LLMAPIHandlerFactory.completion_cost_or_none(standard_reference) == pytest.approx(0.0609)


def test_vertex_flex_correction_survives_tier_restoration(tier_priced_model: str) -> None:
    """No-regression for the working Vertex path: its flex tier travels as a traffic_type, which
    the restoration must neither consume nor disturb, and which litellm still does not price."""
    response = _bridge_response(model_id="id:fallback-leg")
    response._hidden_params["provider_specific_fields"] = {"traffic_type": "ON_DEMAND_FLEX"}

    tier, source = LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert (tier, source) == (None, None)
    assert getattr(response, "service_tier", None) is None
    # litellm prices ON_DEMAND_FLEX at the standard rate, which is why the halving below is ours.
    assert litellm.completion_cost(completion_response=response) == pytest.approx(0.0007)
    assert LLMAPIHandlerFactory.completion_cost_or_none(response) == pytest.approx(0.00035)


@pytest.mark.asyncio
async def test_llm_caller_logs_the_served_leg_and_where_the_tier_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`model` on this log line is the router group, which is identical for the flex and the
    fallback leg — so the split between them, and the mispricing riding on it, was invisible."""
    caller, logger = _stub_successful_llm_caller(monkeypatch)
    caller._router = _TieredRouter()  # type: ignore[assignment]
    monkeypatch.setattr(caller, "_dispatch_llm_call", AsyncMock(return_value=_bridge_response(model_id="id:flex-leg")))

    await caller.call(prompt="test", prompt_name="taskv3-agent-loop")

    metrics = next(fields for event, fields in logger.events if event == "LLM API handler duration metrics")
    assert metrics["served_model_group"] == "openai-unittest-flex"
    assert metrics["service_tier_source"] == "inferred"
    assert metrics["service_tier"] == "flex"


def test_recovered_tier_stays_out_of_the_persisted_response_artifact() -> None:
    """The LLM_RESPONSE artifact is dumped from this object. A tier we recovered must not appear
    there, or a later investigation reads our own inference as something the provider reported —
    which is how the mispricing survived in the first place."""
    response = _bridge_response(model_id="id:flex-leg")

    LLMAPIHandlerFactory._record_served_service_tier(_TieredRouter(), response)

    assert api_handler_factory._effective_service_tier(response) == "flex"
    assert "service_tier" not in api_handler_factory._safe_model_dump_json(response)


@pytest.mark.asyncio
async def test_reported_tier_is_still_logged_with_its_provenance_without_a_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tier logged with no source beside it is the same conflation, just relocated to the log."""
    caller, logger = _stub_successful_llm_caller(monkeypatch)
    response = _bridge_response(model_id=None, service_tier="flex")
    monkeypatch.setattr(caller, "_dispatch_llm_call", AsyncMock(return_value=response))

    await caller.call(prompt="test", prompt_name="extract-actions")

    metrics = next(fields for event, fields in logger.events if event == "LLM API handler duration metrics")
    assert metrics["service_tier"] == "flex"
    assert metrics["service_tier_source"] == "reported"


@pytest.mark.asyncio
async def test_non_speculative_cancelled_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run-level cancellation (elapsed-time timeout / user stop) landing inside an LLM call must
    propagate as CancelledError so the timeout actually halts the run. It must NOT be converted into
    a retryable LLMProviderError, which the step loop would treat as a failure and retry."""
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
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)
    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=CancelledError()))

    # No step is passed, so is_speculative_step is False (the non-speculative branch).
    handler = LLMAPIHandlerFactory.get_llm_api_handler("gpt-4")
    with pytest.raises(BaseException) as exc_info:
        await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)
    assert isinstance(exc_info.value, CancelledError), (
        f"expected CancelledError to propagate, got {type(exc_info.value).__name__}"
    )


def test_get_api_parameters_injects_safety_settings_for_gemini_direct_config() -> None:
    llm_config = LLMConfig(
        model_name="vertex_ai/gemini-2.5-flash",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    params = LLMAPIHandlerFactory.get_api_parameters(llm_config)
    assert params["safety_settings"] == GEMINI_SAFETY_SETTINGS
    assert all(setting["threshold"] == "BLOCK_NONE" for setting in params["safety_settings"])


def test_get_api_parameters_omits_safety_settings_for_gemini_router_config() -> None:
    # Router configs must NOT carry safety_settings at the request level — it would ride along
    # to the non-Gemini fallback deployment and 400. Injection happens per-deployment instead.
    params = LLMAPIHandlerFactory.get_api_parameters(_gemini_2_5_flash_router())
    assert "safety_settings" not in params


def test_inject_gemini_safety_settings_targets_only_gemini_deployments() -> None:
    # Reproduces incident #646: a Gemini primary + Azure fallback in one router. safety_settings
    # must land on the Gemini deployment and stay off the Azure one so the fallback hop survives.
    model_list = [
        {"model_name": "vertex-gemini-2.5-flash-lite", "litellm_params": {"model": "vertex_ai/gemini-2.5-flash-lite"}},
        {"model_name": "gpt-5-mini-fallback", "litellm_params": {"model": "azure/gpt-5-mini"}},
    ]
    result = api_handler_factory._inject_gemini_safety_settings(model_list)
    assert result[0]["litellm_params"]["safety_settings"] == GEMINI_SAFETY_SETTINGS
    assert "safety_settings" not in result[1]["litellm_params"]


def test_get_api_parameters_omits_safety_settings_for_non_gemini_config() -> None:
    llm_config = LLMConfig(
        model_name="gpt-4",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    params = LLMAPIHandlerFactory.get_api_parameters(llm_config)
    assert "safety_settings" not in params


# SKY-12589: the fallback-succeeded log and the truncation-retry gate must key off the
# deployment that actually served the request (litellm _hidden_params.model_id), not the
# provider label — flex and standard tiers of the same model return identical labels.


def _make_flex_style_router_config() -> LLMRouterConfig:
    return _make_three_tier_router_config(
        fallback_groups=["standard-group", "gpt-fallback-group"],
        litellm_models={
            "primary-group": "vertex_ai/shared-model",
            "standard-group": "vertex_ai/shared-model",
            "gpt-fallback-group": "azure/gpt-x",
        },
    )


class _DeploymentAwareRouter:
    """FakeRouter whose responses carry _hidden_params.model_id resolvable via get_deployment."""

    responses: list[FakeLLMResponse] = []

    def __init__(self, **kwargs: Any) -> None:
        self.model_list = kwargs.get("model_list") or []
        self.calls: list[str] = []
        type(self).last_instance = self

    async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> FakeLLMResponse:
        self.calls.append(model)
        return type(self).responses[len(self.calls) - 1]

    def get_deployment(self, model_id: str) -> Any:
        group = model_id.removeprefix("id:")
        return SimpleNamespace(model_name=group)


def _run_flex_router_test(
    monkeypatch: pytest.MonkeyPatch, llm_key: str, responses: list[FakeLLMResponse]
) -> tuple[DummyLogger, _DeploymentAwareRouter]:
    logger = DummyLogger()
    monkeypatch.setattr(api_handler_factory, "LOG", logger)

    class _Router(_DeploymentAwareRouter):
        pass

    _Router.responses = responses
    monkeypatch.setattr(api_handler_factory.litellm, "Router", _Router)
    _stub_for_router_test(monkeypatch, llm_key=llm_key, config=_make_flex_style_router_config())
    return logger, _Router


def _fallback_log_events(logger: DummyLogger) -> list[dict[str, Any]]:
    return [kwargs for event, kwargs in logger.events if event == "LLM router fallback succeeded"]


@pytest.mark.asyncio
async def test_step_engine_router_logs_the_served_leg_on_its_metrics_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step engine has its own metrics log line, separate from LLMCaller's, and carries the
    bulk of router traffic. `model` there is the router group for both legs, so without the served
    deployment the flex/fallback split is as invisible as it was on the other path."""
    logger, _ = _run_flex_router_test(
        monkeypatch,
        "TEST_ROUTER_METRICS_SERVED_GROUP",
        [FakeLLMResponse("shared-model", hidden_params={"model_id": "id:primary-group"})],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_ROUTER_METRICS_SERVED_GROUP")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    metrics = next(fields for event, fields in logger.events if event == "LLM API handler duration metrics")
    assert metrics["served_model_group"] == "primary-group"
    # The deployment declares no priceable tier, so there is nothing to infer and no provenance
    # to claim — `served_model_group` alone carries the leg.
    assert metrics["service_tier_source"] is None


@pytest.mark.asyncio
async def test_router_flex_served_primary_does_not_log_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request served by the flex primary returns the same provider label as the standard
    tier; the serving deployment id must prove it was the primary and suppress the log."""
    logger, router_cls = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_PRIMARY_SERVED",
        [FakeLLMResponse("shared-model", hidden_params={"model_id": "id:primary-group"})],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_PRIMARY_SERVED")
    result = await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert result == {"actions": []}
    assert router_cls.last_instance.calls == ["primary-group"]
    assert _fallback_log_events(logger) == []


@pytest.mark.asyncio
async def test_router_same_label_tier_fallback_logs_served_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tier fallback serves the same provider label as the primary; the log must fire and
    carry the serving deployment group."""
    logger, _ = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_TIER_FALLBACK",
        [FakeLLMResponse("shared-model", hidden_params={"model_id": "id:standard-group"})],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_TIER_FALLBACK")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    events = _fallback_log_events(logger)
    assert len(events) == 1
    assert events[0]["primary_model"] == "primary-group"
    assert events[0]["fallback_model"] == "shared-model"
    assert events[0]["served_model_group"] == "standard-group"


@pytest.mark.asyncio
async def test_router_cross_model_fallback_logs_served_group(monkeypatch: pytest.MonkeyPatch) -> None:
    logger, _ = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_CROSS_MODEL_FALLBACK",
        [FakeLLMResponse("gpt-x-2025", hidden_params={"model_id": "id:gpt-fallback-group"})],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_CROSS_MODEL_FALLBACK")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    events = _fallback_log_events(logger)
    assert len(events) == 1
    assert events[0]["served_model_group"] == "gpt-fallback-group"
    assert events[0]["fallback_model"] == "gpt-x-2025"


@pytest.mark.asyncio
async def test_router_degraded_mode_label_match_does_not_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a serving deployment id, a response label matching the primary deployment's
    configured model must count as primary-served (no false fallback log)."""
    logger, _ = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_DEGRADED_PRIMARY",
        [FakeLLMResponse("shared-model")],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_DEGRADED_PRIMARY")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert _fallback_log_events(logger) == []


@pytest.mark.asyncio
async def test_router_degraded_mode_cross_model_still_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    logger, _ = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_DEGRADED_CROSS_MODEL",
        [FakeLLMResponse("gpt-x-2025")],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_DEGRADED_CROSS_MODEL")
    await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    events = _fallback_log_events(logger)
    assert len(events) == 1
    assert events[0]["served_model_group"] is None


@pytest.mark.asyncio
async def test_truncation_retry_fires_for_flex_served_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-fix, the label-vs-group comparison made the truncation gate always False for
    flex-style routers, so truncated flex responses never got the fallback retry."""
    logger, router_cls = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_TRUNCATION_RETRY",
        [
            FakeLLMResponse(
                "shared-model", content=None, finish_reason="length", hidden_params={"model_id": "id:primary-group"}
            ),
            FakeLLMResponse("shared-model", hidden_params={"model_id": "id:standard-group"}),
        ],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_TRUNCATION_RETRY")
    result = await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert result == {"actions": []}
    assert router_cls.last_instance.calls == ["primary-group", "standard-group"]


@pytest.mark.asyncio
async def test_truncation_retry_skipped_when_fallback_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated response that was already served by a fallback leg must not retry again
    (retry-only-from-primary contract)."""
    logger, router_cls = _run_flex_router_test(
        monkeypatch,
        "TEST_FLEX_TRUNCATION_NO_RETRY",
        [
            FakeLLMResponse(
                "gpt-x-2025", content=None, finish_reason="length", hidden_params={"model_id": "id:gpt-fallback-group"}
            ),
        ],
    )

    handler = LLMAPIHandlerFactory.get_llm_api_handler_with_router("TEST_FLEX_TRUNCATION_NO_RETRY")
    with pytest.raises(Exception):
        await handler(prompt='{"actions": []}', prompt_name="extract-actions")

    assert router_cls.last_instance.calls == ["primary-group"]


@pytest.mark.asyncio
async def test_extra_body_from_litellm_params_reaches_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """litellm_params["extra_body"] must survive to the acompletion call.

    A top-level reasoning_effort is stripped by drop_params=True whenever LiteLLM
    does not recognize the model as a reasoning model, which silently disables
    thinking on custom OpenAI-compatible deployments. extra_body is the escape
    hatch: LiteLLM forwards it verbatim, so it must not be swallowed en route.
    """
    context = MagicMock()
    context.vertex_cache_name = None
    context.use_prompt_caching = False
    context.cached_static_prompt = None
    context.hashed_href_map = {}

    llm_config = LLMConfig(
        model_name="openai/some-custom-deployment",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        litellm_params={
            "api_key": "test-key",
            "api_base": "https://llm.example.test/openai/v1",
            "extra_body": {"reasoning_effort": "high"},
        },
    )

    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.get_config", lambda _: llm_config
    )
    monkeypatch.setattr(
        "skyvern.forge.sdk.api.llm.api_handler_factory.LLMConfigRegistry.is_router_config", lambda _: False
    )
    monkeypatch.setattr("skyvern.forge.sdk.api.llm.api_handler_factory.skyvern_context.current", lambda: context)
    monkeypatch.setattr(
        api_handler_factory, "llm_messages_builder", AsyncMock(return_value=[{"role": "user", "content": "test"}])
    )
    monkeypatch.setattr(api_handler_factory.litellm, "completion_cost", lambda _: 0.0)

    completion_params: dict[str, Any] = {}

    async def mock_acompletion(*args: Any, **kwargs: Any) -> FakeLLMResponse:
        completion_params.update(kwargs)
        return FakeLLMResponse("some-custom-deployment")

    monkeypatch.setattr(api_handler_factory.litellm, "acompletion", AsyncMock(side_effect=mock_acompletion))

    handler = LLMAPIHandlerFactory.get_llm_api_handler("TEST_EXTRA_BODY_PASSTHROUGH")
    await handler(prompt="test prompt", prompt_name=EXTRACT_ACTION_PROMPT_NAME)

    assert completion_params["extra_body"] == {"reasoning_effort": "high"}


# ---------------------------------------------------------------------------
# LLMCaller router-config support (SKY: v3 / direct-call engines can use fallback/flex groups)
# ---------------------------------------------------------------------------


def test_llmcaller_builds_router_for_router_config(monkeypatch: pytest.MonkeyPatch) -> None:
    router_config = _gemini_3_flash_router()
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: router_config)
    monkeypatch.setattr(api_handler_factory, "_LLMCALLER_ROUTER_CACHE", {})
    sentinel = MagicMock(name="litellm_router")
    monkeypatch.setattr(api_handler_factory, "_build_litellm_router", lambda cfg: sentinel)

    caller = LLMCaller("GEMINI_3_FLASH_WITH_FALLBACK")

    assert caller._router is sentinel
    assert caller._router_model_group == router_config.main_model_group


def test_llmcaller_no_router_for_direct_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api_handler_factory.LLMConfigRegistry, "get_config", lambda _: _custom_llm_config("openai/example-model")
    )
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)

    caller = LLMCaller("SOME_DIRECT_KEY")

    assert caller._router is None
    assert caller._router_model_group is None


@pytest.mark.asyncio
async def test_llmcaller_dispatches_router_config_through_router(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: a router config used to crash LLMCaller (no .litellm_params on the config, and a
    # bare group name handed to litellm.acompletion). It must now dispatch through the Router with
    # model=main_model_group.
    router_config = _gemini_3_flash_router()
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: router_config)
    monkeypatch.setattr(api_handler_factory, "_LLMCALLER_ROUTER_CACHE", {})
    fake_router = MagicMock(name="litellm_router")
    fake_router.acompletion = AsyncMock(return_value="ROUTED")
    monkeypatch.setattr(api_handler_factory, "_build_litellm_router", lambda cfg: fake_router)
    monkeypatch.setattr(api_handler_factory, "_validate_custom_llm_api_base", AsyncMock())

    caller = LLMCaller("GEMINI_3_FLASH_WITH_FALLBACK")
    result = await caller._dispatch_llm_call(messages=[{"role": "user", "content": "hi"}], tools=None)

    assert result == "ROUTED"
    fake_router.acompletion.assert_awaited_once()
    assert fake_router.acompletion.await_args.kwargs["model"] == router_config.main_model_group


def test_llmcaller_router_is_cached_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    # v3 builds one LLMCaller per run; routers must be shared by key (one redis pool), not rebuilt.
    router_config = _gemini_3_flash_router()
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: router_config)
    monkeypatch.setattr(api_handler_factory, "_LLMCALLER_ROUTER_CACHE", {})
    build_calls: list[object] = []

    def _fake_build(_cfg: object) -> MagicMock:
        built = MagicMock(name="litellm_router")
        build_calls.append(built)
        return built

    monkeypatch.setattr(api_handler_factory, "_build_litellm_router", _fake_build)

    first = LLMCaller("GEMINI_3_FLASH_WITH_FALLBACK")
    second = LLMCaller("GEMINI_3_FLASH_WITH_FALLBACK")

    assert first._router is second._router
    assert len(build_calls) == 1


# Deployments have to be models litellm's *bundled* cost map knows: CI forces
# LITELLM_LOCAL_MODEL_COST_MAP to match production, and a model that only the fetched map
# carries would make the probe answer differently depending on which suite ran first.
_TOOL_CHOICE_CAPABLE_MODEL = "gpt-4o"


def _flex_fallback_router(*, fallback_deployment_model: str = _TOOL_CHOICE_CAPABLE_MODEL) -> LLMRouterConfig:
    return LLMRouterConfig(
        model_name="openai-flex-fallback-router",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
        model_list=[
            LLMRouterModelConfig(
                model_name="openai-flex",
                litellm_params={"model": _TOOL_CHOICE_CAPABLE_MODEL},
            ),
            LLMRouterModelConfig(
                model_name="openai-flex-fallback",
                litellm_params={"model": fallback_deployment_model},
            ),
        ],
        main_model_group="openai-flex",
        fallback_model_group="openai-flex-fallback",
    )


def test_supports_tool_choice_resolves_router_through_its_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe must resolve a router's tool_choice support through its deployments' underlying
    litellm models, not the router's own group name -- litellm knows nothing about the latter."""
    router_config = _flex_fallback_router()
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: router_config)

    caller = LLMCaller("FLEX_FALLBACK_ROUTER")

    assert caller.supports_tool_choice() is True
    # Pins why the probe must go through model_list: asking litellm about the router's own group
    # name denies every router, which would make the feature a no-op on the model it targets.
    assert litellm.utils.supports_tool_choice(model=router_config.model_name) is False


def test_supports_tool_choice_denies_router_when_any_deployment_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # litellm.Router validates every deployment's model string at construction time, so the
    # "unsupported" deployment must be a real, recognized model (just one litellm knows doesn't
    # take tool_choice) rather than a made-up string, which would blow up LLMCaller.__init__.
    router_config = _flex_fallback_router(fallback_deployment_model="openai/gpt-3.5-turbo-instruct")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: router_config)

    caller = LLMCaller("FLEX_FALLBACK_ROUTER_PARTIAL")

    assert caller.supports_tool_choice() is False


def test_supports_tool_choice_follows_dispatch_order_for_anthropic_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # A router-backed key dispatches through litellm.Router, which forwards tool_choice, even when
    # its name contains ANTHROPIC. Denying on the substring alone would silently disable the lever.
    router_config = _flex_fallback_router()
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: router_config)
    monkeypatch.setattr(api_handler_factory, "_LLMCALLER_ROUTER_CACHE", {})
    monkeypatch.setattr(api_handler_factory, "_build_litellm_router", lambda cfg: MagicMock())

    assert LLMCaller("BEDROCK_ANTHROPIC_CLAUDE5_OPUS_WITH_FALLBACK").supports_tool_choice() is True

    # A direct ANTHROPIC key reaches _call_anthropic, which builds its provider kwargs from an
    # explicit allowlist and would discard the parameter while the run still reported it applied.
    direct_config = LLMConfig(
        model_name="anthropic/claude-sonnet-4-6",
        required_env_vars=[],
        supports_vision=True,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: direct_config)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)

    assert LLMCaller("ANTHROPIC_CLAUDE4.6_SONNET").supports_tool_choice() is False


def test_supports_tool_choice_denies_unrecognized_direct_model(monkeypatch: pytest.MonkeyPatch) -> None:
    llm_config = _custom_llm_config("not-a-real-model-xyz")
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: llm_config)

    caller = LLMCaller("UNRECOGNIZED_DIRECT_MODEL")

    assert caller.supports_tool_choice() is False


@pytest.mark.asyncio
async def test_call_drops_tool_choice_the_model_cannot_take(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared helper hardcodes model_name="gpt-4" and patches get_config, so the "supported"
    # arm cannot reuse it and is built inline against a real tool_choice-capable model.
    caller, _ = _stub_successful_llm_caller(monkeypatch)
    monkeypatch.setattr(caller, "supports_tool_choice", lambda: False)

    await caller.call(prompt="test", prompt_name="taskv3-agent-loop", tool_choice="required")

    dispatch_kwargs = caller._dispatch_llm_call.await_args.kwargs
    assert "tool_choice" not in dispatch_kwargs

    supported_llm_config = LLMConfig(
        model_name="gpt-4.1",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
    )
    monkeypatch.setattr(api_handler_factory.LLMConfigRegistry, "get_config", lambda _: supported_llm_config)
    monkeypatch.setattr(api_handler_factory.skyvern_context, "current", lambda: None)
    monkeypatch.setattr(
        api_handler_factory,
        "llm_messages_builder_with_history",
        AsyncMock(return_value=[{"role": "user", "content": "test"}]),
    )
    supported_caller = LLMCaller("TEST_LLM_CALLER_SUPPORTED_TOOL_CHOICE")
    monkeypatch.setattr(supported_caller, "_dispatch_llm_call", AsyncMock(return_value=FakeLLMResponse("gpt-4.1")))
    monkeypatch.setattr(api_handler_factory, "parse_api_response", lambda *args: {"actions": []})
    artifact_manager = MagicMock()
    artifact_manager.bulk_create_artifacts = AsyncMock()
    monkeypatch.setattr(api_handler_factory.app, "ARTIFACT_MANAGER", artifact_manager)

    await supported_caller.call(prompt="test", prompt_name="taskv3-agent-loop", tool_choice="required")

    supported_dispatch_kwargs = supported_caller._dispatch_llm_call.await_args.kwargs
    assert supported_dispatch_kwargs["tool_choice"] == "required"
