from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from agents.handoffs import Handoff
from agents.items import TResponseInputItem
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter, ShouldReplayReasoningContent
from agents.tool import Tool
from agents.util._json import _to_dump_compatible
from litellm.completion_extras import responses_api_bridge

_DIRECT_OPENAI_GPT56_MODELS = {
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}


class CacheableSystemInstructions(str):
    """A behavior-identical system prompt with an explicit stable-prefix seam.

    The Agents SDK still receives a string and therefore sees exactly the same
    instructions as before. ``CopilotLitellmModel`` uses the attached parts only
    when it builds a direct-OpenAI GPT-5.6 wire request.
    """

    stable_prefix: str
    dynamic_suffix: str
    cache_namespace: str | None

    def __new__(
        cls,
        stable_prefix: str,
        dynamic_suffix: str,
        *,
        cache_namespace: str | None = None,
    ) -> CacheableSystemInstructions:
        value = stable_prefix + dynamic_suffix
        instance = super().__new__(cls, value)
        instance.stable_prefix = stable_prefix
        instance.dynamic_suffix = dynamic_suffix
        instance.cache_namespace = cache_namespace
        return instance


@dataclass(slots=True)
class ExplicitCacheEnvelope:
    """Logical and Responses API views of one cacheable Copilot request."""

    logical_messages: list[Any]
    responses_input: list[Any]
    responses_tools: list[Any]
    prompt_cache_key: str
    extra_body: dict[str, Any]


def _normalized_direct_openai_model(model: str, base_url: str | None) -> str | None:
    # A configured base URL may point at Azure, an OpenAI-compatible gateway,
    # or a regional endpoint with different rollout semantics. Leave every
    # custom route on its current implicit-caching behavior.
    if base_url:
        return None
    normalized = model.lower()
    if normalized.startswith("openai/"):
        normalized = normalized.removeprefix("openai/")
    if not any(
        normalized == model_name
        or (
            normalized.startswith(f"{model_name}-")
            and len(normalized) == len(model_name) + len("-2026-07-09")
            and normalized[-10:].replace("-", "").isdigit()
        )
        for model_name in _DIRECT_OPENAI_GPT56_MODELS
    ):
        return None
    return normalized


def _converted_tools(tools: list[Tool], handoffs: list[Handoff]) -> list[Any]:
    converted: list[Any] = [Converter.tool_to_openai(tool) for tool in tools]
    converted.extend(Converter.convert_handoff_tool(handoff) for handoff in handoffs)
    return converted


def _cache_key(
    *,
    cache_namespace: str,
    model: str,
    stable_prefix: str,
    converted_tools: list[Any],
) -> str:
    tool_surface = json.dumps(converted_tools, sort_keys=True, separators=(",", ":"), default=str)
    material = {
        "cache_namespace": cache_namespace,
        "model": model,
        "stable_prefix_sha256": hashlib.sha256(stable_prefix.encode()).hexdigest(),
        "tool_surface_sha256": hashlib.sha256(tool_surface.encode()).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Keep the routing key opaque: it binds the relevant inputs without
    # disclosing a chat/session identifier to request logs.
    return f"copilot:{digest[:48]}"


def build_explicit_cache_envelope(
    *,
    model: str,
    base_url: str | None,
    system_instructions: str | None,
    input: str | list[TResponseInputItem],
    model_settings: ModelSettings,
    tools: list[Tool],
    handoffs: list[Handoff],
    should_replay_reasoning_content: ShouldReplayReasoningContent | None = None,
) -> ExplicitCacheEnvelope | None:
    """Build a direct GPT-5.6 Responses API cache envelope, or opt out safely."""

    normalized_model = _normalized_direct_openai_model(model, base_url)
    if normalized_model is None:
        return None
    if not isinstance(system_instructions, CacheableSystemInstructions):
        return None
    if not system_instructions.cache_namespace or not system_instructions.stable_prefix:
        return None
    # LiteLLM implements model fallbacks only on the Chat Completions path.
    # Preserve that existing routing behavior instead of silently dropping it
    # when selecting the explicit Responses transport.
    if model_settings.extra_args and model_settings.extra_args.get("fallbacks"):
        return None

    preserve_thinking_blocks = model_settings.reasoning is not None and model_settings.reasoning.effort is not None
    logical_messages: list[Any] = list(
        Converter.items_to_messages(
            input,
            base_url=base_url,
            preserve_thinking_blocks=preserve_thinking_blocks,
            preserve_tool_output_all_content=True,
            model=model,
            should_replay_reasoning_content=should_replay_reasoning_content,
        )
    )
    wire_messages = list(logical_messages)
    logical_messages.insert(
        0,
        {
            "role": "system",
            "content": str(system_instructions),
        },
    )
    logical_messages = cast(list[Any], _to_dump_compatible(logical_messages))
    wire_messages.insert(
        0,
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": system_instructions.stable_prefix,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": system_instructions.dynamic_suffix,
                },
            ],
        },
    )

    converted_tools = cast(list[Any], _to_dump_compatible(_converted_tools(tools, handoffs)))
    responses_input, instructions = (
        responses_api_bridge.transformation_handler.convert_chat_completion_messages_to_responses_api(wire_messages)
    )
    # A list-valued system message must remain an input item so the breakpoint
    # stays attached to its stable text block. Falling back is safer than
    # silently issuing an "explicit" request without a provider breakpoint.
    if instructions is not None:
        return None
    responses_tools = responses_api_bridge.transformation_handler._convert_tools_to_responses_format(converted_tools)

    if model_settings.extra_body is not None and not isinstance(model_settings.extra_body, dict):
        return None
    extra_body: dict[str, Any] = {}
    if model_settings.extra_body:
        extra_body.update(cast(dict[str, Any], model_settings.extra_body))
    extra_body.pop("reasoning_effort", None)
    extra_body["prompt_cache_options"] = {"mode": "explicit"}

    return ExplicitCacheEnvelope(
        logical_messages=logical_messages,
        responses_input=responses_input,
        responses_tools=responses_tools,
        prompt_cache_key=_cache_key(
            cache_namespace=system_instructions.cache_namespace,
            model=normalized_model,
            stable_prefix=system_instructions.stable_prefix,
            converted_tools=converted_tools,
        ),
        extra_body=extra_body,
    )
