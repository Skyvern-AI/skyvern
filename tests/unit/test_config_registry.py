import json
import runpy
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from skyvern.forge.sdk.api.llm import config_registry
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory, _build_litellm_router
from skyvern.forge.sdk.api.llm.copilot_model_usage import CopilotModelUsageEvent, _emit_copilot_model_usage
from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config
from skyvern.forge.sdk.copilot.secret_scrub import REDACTED_SECRET_PLACEHOLDER
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import _model_log_value, redact_registered_log_payload
from skyvern.schemas import llm as llm_schemas
from tests.unit.forge_log_capture import capture_runtime_logs


def test_only_builtin_model_registration_establishes_log_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.LLMConfigRegistry, "_configs", {})
    config = llm_schemas.LLMConfig("gpt-5.6-terra", [], False, False)
    config_registry._register_builtin_config("BUILTIN", config)
    config_registry.LLMConfigRegistry.register_config("CUSTOM", config)
    builtin = config_registry.LLMConfigRegistry.get_config("BUILTIN")
    custom = config_registry.LLMConfigRegistry.get_config("CUSTOM")
    with skyvern_context.scoped(SkyvernContext(runtime_secret_values={"5.6"})):
        _, values = redact_registered_log_payload(
            "diagnostic",
            {
                "model": _model_log_value("model", builtin.model_name),
                "custom": _model_log_value("custom", custom.model_name),
            },
        )
    assert values["model"] == "gpt-5.6-terra"
    assert values["custom"] == f"gpt-{REDACTED_SECRET_PLACEHOLDER}-terra"
    assert type(config.model_name) is str


@pytest.mark.parametrize("builtin", [True, False])
def test_real_router_serving_group_retains_only_its_configuration_provenance(
    monkeypatch: pytest.MonkeyPatch, builtin: bool
) -> None:
    monkeypatch.setattr(config_registry.LLMConfigRegistry, "_configs", {})
    group = "gpt-4-1-mini-fallback"
    config = llm_schemas.LLMRouterConfig(
        model_name=group,
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        main_model_group=group,
        model_list=[
            llm_schemas.LLMRouterModelConfig(
                model_name=group,
                litellm_params={"model": "openai/gpt-4.1-mini", "api_key": "synthetic-test-key"},
                model_info={"id": "test-registered-deployment"},
            )
        ],
    )
    register = (
        config_registry._register_builtin_config if builtin else config_registry.LLMConfigRegistry.register_config
    )
    register("TEST_ROUTER", config)
    resolved = config_registry.LLMConfigRegistry.get_config("TEST_ROUTER")
    assert isinstance(resolved, llm_schemas.LLMRouterConfig)
    router = _build_litellm_router(resolved)
    response = SimpleNamespace(_hidden_params={"model_id": "test-registered-deployment"}, model=group)
    served = LLMAPIHandlerFactory._served_model_group(router, response)
    with skyvern_context.scoped(SkyvernContext(runtime_secret_values={"1"})):
        _, values = redact_registered_log_payload(
            "diagnostic",
            {"served_model_group": _model_log_value("served_model_group", served), "response_model": response.model},
        )
    redacted = f"gpt-4-{REDACTED_SECRET_PLACEHOLDER}-mini-fallback"
    assert values["served_model_group"] == (group if builtin else redacted)
    assert values["response_model"] == redacted


@pytest.mark.parametrize("builtin", [True, False])
def test_copilot_router_provider_model_retains_only_its_source_provenance(
    monkeypatch: pytest.MonkeyPatch, builtin: bool
) -> None:
    monkeypatch.setattr(config_registry.LLMConfigRegistry, "_configs", {})
    provider_model = "vertex_ai/gemini-2.5-flash"
    credential = "synthetic-test-key"
    parameters = {"model": provider_model, "api_key": credential}
    config = llm_schemas.LLMRouterConfig(
        model_name="router-group",
        required_env_vars=[],
        supports_vision=False,
        add_assistant_prefix=False,
        main_model_group="router-group",
        model_list=[llm_schemas.LLMRouterModelConfig("router-group", parameters)],
    )
    register = (
        config_registry._register_builtin_config if builtin else config_registry.LLMConfigRegistry.register_config
    )
    register("TEST_ROUTER", config)
    with skyvern_context.scoped(SkyvernContext(runtime_secret_values={"5", credential})):
        model_name, run_config, _, _ = resolve_model_config(None, llm_key_override="TEST_ROUTER")
        model = run_config.model_provider.get_model(model_name)
        with capture_runtime_logs() as events:
            _emit_copilot_model_usage(
                CopilotModelUsageEvent(
                    request_model=model.model,
                    response_model=provider_model,
                    input_tokens=15,
                    cost=0.25,
                ),
                logger=structlog.get_logger("skyvern.test.router_provider_provenance"),
            )
            structlog.get_logger().info("Caller parameters", payload=parameters)
    records = json.loads(json.dumps(events))
    usage = next(record for record in records if record.get("log_code") == "copilot_model_usage")
    redacted = f"vertex_ai/gemini-2.{REDACTED_SECRET_PLACEHOLDER}-flash"
    assert usage["gen_ai.request.model"] == (provider_model if builtin else redacted)
    assert usage["gen_ai.response.model"] == redacted
    assert usage["gen_ai.usage.input_tokens"] == 15 and type(usage["gen_ai.usage.input_tokens"]) is int
    assert usage["operation.cost"] == 0.25 and type(usage["operation.cost"]) is float
    assert credential not in json.dumps(records)
    assert type(parameters["model"]) is str


def test_xai_grok_4_5_cost_override_uses_separate_output_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    registered_models: dict[str, dict[str, Any]] = {}
    monkeypatch.setattr(config_registry.litellm, "register_model", registered_models.update)

    config_registry._register_model_cost_overrides()

    model_info = registered_models[config_registry.XAI_GROK_4_5_MODEL]
    assert model_info["max_input_tokens"] == config_registry.XAI_GROK_4_5_CONTEXT_WINDOW
    assert model_info["max_output_tokens"] == config_registry.XAI_GROK_4_5_MAX_OUTPUT_TOKENS
    assert model_info["max_tokens"] == config_registry.XAI_GROK_4_5_MAX_OUTPUT_TOKENS


def test_xai_grok_4_5_config_uses_reasoning_completion_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "XAI_REASONING_EFFORT", "high")

    llm_config = config_registry._build_xai_grok_4_5_config()
    parameters = LLMAPIHandlerFactory.get_api_parameters(llm_config)

    assert llm_config.reasoning_effort == "high"
    assert parameters["max_completion_tokens"] == config_registry.XAI_GROK_4_5_MAX_OUTPUT_TOKENS
    assert "max_tokens" not in parameters


def test_openrouter_deepseek_v4_flash_0731_registry_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_registry.settings, "ENABLE_OPENROUTER", True)
    monkeypatch.setattr(config_registry.settings, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm_schemas, "_settings", lambda: config_registry.settings)
    assert config_registry.__file__ is not None

    registry_namespace = runpy.run_path(str(Path(config_registry.__file__)))
    registry = registry_namespace["LLMConfigRegistry"]

    assert registry.is_registered("OPENROUTER_DEEPSEEK_V4_FLASH_0731")
    llm_config = registry.get_config("OPENROUTER_DEEPSEEK_V4_FLASH_0731")
    assert llm_config.model_name == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert llm_config.supports_vision is False
    assert llm_config.litellm_params is not None
    assert llm_config.litellm_params["extra_body"] == {
        "reasoning_effort": "high",
        "provider": {
            "order": ["cloudflare", "parasail"],
            "allow_fallbacks": False,
            "quantizations": ["fp8"],
        },
    }


@pytest.mark.parametrize(
    ("llm_key", "model_name"),
    [
        ("CHEAPER_INFERENCE_GPT5_4_MINI", "openai/gpt-5.4-mini"),
        ("CHEAPER_INFERENCE_GPT5_4", "openai/gpt-5.4"),
    ],
)
def test_cheaper_inference_registry_config(monkeypatch: pytest.MonkeyPatch, llm_key: str, model_name: str) -> None:
    monkeypatch.setattr(config_registry.settings, "ENABLE_CHEAPER_INFERENCE", True)
    monkeypatch.setattr(config_registry.settings, "CHEAPER_INFERENCE_API_KEY", "test-key")
    monkeypatch.setattr(llm_schemas, "_settings", lambda: config_registry.settings)
    assert config_registry.__file__ is not None

    registry_namespace = runpy.run_path(str(Path(config_registry.__file__)))
    registry = registry_namespace["LLMConfigRegistry"]

    assert registry.is_registered(llm_key)
    llm_config = registry.get_config(llm_key)
    assert llm_config.model_name == model_name
    assert llm_config.required_env_vars == ["CHEAPER_INFERENCE_API_KEY"]
    assert llm_config.supports_vision is True
    assert llm_config.litellm_params is not None
    assert llm_config.litellm_params["api_key"] == "test-key"
    assert llm_config.litellm_params["api_base"] == "https://api.cheaperinference.com/v1"


def _openrouter_capture_server(captured: dict[str, Any]) -> HTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            captured["body"] = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            payload = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek/deepseek-v4-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": '{"actions": []}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    return HTTPServer(("127.0.0.1", 0), _Handler)


@pytest.mark.asyncio
async def test_openrouter_deepseek_v4_flash_sends_provider_ignore_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter skips an unrecognized slug in `ignore` silently instead of rejecting it, so a
    typo would leave the route on the defective provider while the config still looked right."""
    captured: dict[str, Any] = {}
    real_get_config = config_registry.LLMConfigRegistry.get_config

    with _openrouter_capture_server(captured) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(config_registry.settings, "ENABLE_OPENROUTER", True)
            monkeypatch.setattr(config_registry.settings, "OPENROUTER_API_KEY", "test-key")
            monkeypatch.setattr(
                config_registry.settings, "OPENROUTER_API_BASE", f"http://127.0.0.1:{server.server_port}"
            )
            monkeypatch.setattr(llm_schemas, "_settings", lambda: config_registry.settings)
            assert config_registry.__file__ is not None

            registry_namespace = runpy.run_path(str(Path(config_registry.__file__)))
            llm_config = registry_namespace["LLMConfigRegistry"].get_config("OPENROUTER_DEEPSEEK_V4_FLASH")
            assert llm_config.litellm_params is not None
            assert llm_config.litellm_params["extra_body"] == {"provider": {"ignore": ["open-inference"]}}

            # The process-wide registry may hold a config issue for this key (no OpenRouter key in
            # the environment), which would hand back the dummy handler instead of calling out.
            monkeypatch.setattr(config_registry.LLMConfigRegistry, "get_config_issue", lambda _: None)
            monkeypatch.setattr(
                config_registry.LLMConfigRegistry,
                "get_config",
                lambda key: llm_config if key == "OPENROUTER_DEEPSEEK_V4_FLASH" else real_get_config(key),
            )
            monkeypatch.setattr(LLMAPIHandlerFactory, "_handler_cache", {})
            await LLMAPIHandlerFactory.get_llm_api_handler("OPENROUTER_DEEPSEEK_V4_FLASH")(prompt="hello")
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert captured["body"]["provider"] == {"ignore": ["open-inference"]}
