import json
import runpy
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from skyvern.forge.sdk.api.llm import config_registry
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory
from skyvern.schemas import llm as llm_schemas


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
