from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import litellm
from agents.handoffs import Handoff
from agents.items import TResponseInputItem
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter, ShouldReplayReasoningContent
from agents.tool import Tool
from agents.util._json import _to_dump_compatible
from litellm.completion_extras import responses_api_bridge
from litellm.main import responses_api_bridge_check
from litellm.types.llms.openai import ChatCompletionTextObject
from litellm.utils import peek_reasoning_summary_aliases

_GPT_ROUTE_PREFIXES = ("openai/responses/", "azure/responses/", "openai/", "azure/")
_GPT_VERSION = re.compile(r"gpt-(\d+)(?:\.(\d+))?(?:-|$)")
_EXPLICIT_BREAKPOINT = {"mode": "explicit"}
# LiteLLM runs each fallback with the primary's kwargs, so a hop that does not reset these would
# send the primary's credentials, endpoint and service tier to its own provider. A dict hop may set
# any of them without opting its chain out of explicit caching, so never add an envelope field here.
ROUTER_HOP_FIELDS = (
    "api_key",
    "api_version",
    "model_info",
    "vertex_credentials",
    "vertex_location",
    "thinking",
    "service_tier",
)
_ROUTER_HOP_KEYS = frozenset({"model", "base_url", "timeout", *ROUTER_HOP_FIELDS})


class CacheableSystemInstructions(str):
    """The joined system prompt string, carrying its stable prefix and dynamic suffix so ``CopilotLitellmModel`` can
    cache the prefix: as a GPT Responses envelope, or as two Claude system blocks with the same text."""

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

    # LiteLLM copies the system message for Anthropic and Bedrock; copy and pickle rebuild via __new__.
    def __getnewargs_ex__(self) -> tuple[tuple[str, str], dict[str, str | None]]:
        return (self.stable_prefix, self.dynamic_suffix), {"cache_namespace": self.cache_namespace}


@dataclass(slots=True)
class ExplicitCacheEnvelope:
    """Logical and Responses API views of one cacheable Copilot request."""

    logical_messages: list[Any]
    responses_input: list[Any]
    responses_tools: list[Any]
    prompt_cache_key: str
    extra_body: dict[str, Any]

    @property
    def breakpoint_count(self) -> int:
        return sum(
            isinstance(part, dict) and "prompt_cache_breakpoint" in part
            for item in self.responses_input
            for part in item.get(item_parts_key(item)) or []
        )


def _gpt_cache_model(model: str) -> str | None:
    normalized = model.lower()
    for prefix in _GPT_ROUTE_PREFIXES:
        if normalized.startswith(prefix):
            normalized = normalized.removeprefix(prefix)
            break
    version = _GPT_VERSION.match(normalized)
    if version is None or (int(version[1]), int(version[2] or 0)) < (5, 6):
        return None
    return normalized


def responses_route_model(model: str) -> str:
    """Drop LiteLLM's ``responses/`` route marker, which only its chat bridge strips before the request."""
    for prefix in _GPT_ROUTE_PREFIXES:
        if prefix.endswith("/responses/") and model.startswith(prefix):
            return prefix.removesuffix("responses/") + model.removeprefix(prefix)
    return model


def _hop_sends_responses(hop: dict[str, Any]) -> bool:
    try:
        provider_model, provider, _, _ = litellm.get_llm_provider(hop["model"])
    except litellm.exceptions.BadRequestError:
        return False
    # A non-Azure hop with its own base is a gateway that may reject the cache params, so it keeps implicit caching.
    if provider != "azure" and (hop.get("api_base") or hop.get("base_url")):
        return False
    model_info, _ = responses_api_bridge_check(
        model=provider_model,
        custom_llm_provider=provider,
        tools=hop.get("tools"),
        reasoning_effort=hop.get("reasoning_effort"),
        reasoning_summary=peek_reasoning_summary_aliases(hop),
    )
    return model_info.get("mode") == "responses"


def _chain_sends_responses(base: dict[str, Any], fallbacks: list[str | dict[str, Any]]) -> bool:
    # Any other key on a dict hop would replace part of the cache envelope on that hop.
    if any(isinstance(hop, dict) and not hop.keys() <= _ROUTER_HOP_KEYS for hop in fallbacks):
        return False
    hops = [base, *({**base, **({"model": hop} if isinstance(hop, str) else hop)} for hop in fallbacks)]
    return all(_gpt_cache_model(hop["model"]) and _hop_sends_responses(hop) for hop in hops)


def item_parts_key(item: dict[str, Any]) -> str:
    return "content" if item.get("type") == "message" else "output"


def _mark_last_text_part(item: dict[str, Any]) -> None:
    key = item_parts_key(item)
    parts = item.get(key)
    if isinstance(parts, list) and parts and parts[-1].get("type") == "input_text":
        item[key] = [*parts[:-1], {**parts[-1], "prompt_cache_breakpoint": dict(_EXPLICIT_BREAKPOINT)}]


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


def model_chain(model: str, model_settings: ModelSettings) -> list[str]:
    fallbacks: list[str | dict[str, Any]] = (model_settings.extra_args or {}).get("fallbacks") or []
    return [model, *(hop["model"] if isinstance(hop, dict) else hop for hop in fallbacks)]


def _is_anthropic_model(model: str) -> bool:
    try:
        provider_model, provider, _, _ = litellm.get_llm_provider(model)
    except litellm.exceptions.BadRequestError:
        return False
    return provider == "anthropic" or (provider == "bedrock" and "anthropic." in provider_model)


def anthropic_cached_system_blocks(
    *,
    model: str,
    model_settings: ModelSettings,
    system_instructions: str | None,
) -> list[ChatCompletionTextObject] | None:
    if not isinstance(system_instructions, CacheableSystemInstructions) or not system_instructions.stable_prefix:
        return None
    if (model_settings.extra_args or {}).get("cache_control_injection_points"):
        return None
    if not all(_is_anthropic_model(candidate) for candidate in model_chain(model, model_settings)):
        return None
    blocks: list[ChatCompletionTextObject] = [
        {
            "type": "text",
            "text": system_instructions.stable_prefix,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if system_instructions.dynamic_suffix:
        blocks.append({"type": "text", "text": system_instructions.dynamic_suffix})
    return blocks


def build_explicit_cache_envelope(
    *,
    model: str,
    base_url: str | None,
    system_instructions: str | None,
    input: str | list[TResponseInputItem],
    model_settings: ModelSettings,
    tools: list[Tool],
    handoffs: list[Handoff],
    reasoning_effort: Any = None,
    anchors: Sequence[int] = (),
    should_replay_reasoning_content: ShouldReplayReasoningContent | None = None,
) -> ExplicitCacheEnvelope | None:
    """Build a GPT-5.6+ Responses API cache envelope when every hop already reaches LiteLLM's Responses route."""

    if not isinstance(system_instructions, CacheableSystemInstructions):
        return None
    if not system_instructions.cache_namespace or not system_instructions.stable_prefix:
        return None
    normalized_model = _gpt_cache_model(model)
    if normalized_model is None:
        return None
    if model_settings.extra_body is not None and not isinstance(model_settings.extra_body, dict):
        return None

    converted_tools = cast(list[Any], _to_dump_compatible(_converted_tools(tools, handoffs)))
    extra_args = dict(model_settings.extra_args or {})
    fallbacks = extra_args.pop("fallbacks", None) or []
    base_hop = {
        **extra_args,
        "model": model,
        "base_url": base_url,
        "tools": converted_tools or None,
        "reasoning_effort": reasoning_effort,
        "extra_body": model_settings.extra_body,
    }
    if not _chain_sends_responses(base_hop, fallbacks):
        return None

    preserve_thinking_blocks = model_settings.reasoning is not None and model_settings.reasoning.effort is not None

    def to_messages(items: str | list[TResponseInputItem]) -> list[Any]:
        return list(
            Converter.items_to_messages(
                items,
                base_url=base_url,
                preserve_thinking_blocks=preserve_thinking_blocks,
                preserve_tool_output_all_content=True,
                model=model,
                should_replay_reasoning_content=should_replay_reasoning_content,
            )
        )

    logical_messages = to_messages(input)
    wire_system = {
        "role": "system",
        "content": [
            {
                "type": "input_text",
                "text": system_instructions.stable_prefix,
                "prompt_cache_breakpoint": dict(_EXPLICIT_BREAKPOINT),
            },
            {
                "type": "input_text",
                "text": system_instructions.dynamic_suffix,
            },
        ],
    }
    to_responses = responses_api_bridge.transformation_handler.convert_chat_completion_messages_to_responses_api
    responses_input, instructions = to_responses([wire_system, *logical_messages])
    # A list-valued system message must remain an input item so the breakpoint
    # stays attached to its stable text block. Falling back is safer than
    # silently issuing an "explicit" request without a provider breakpoint.
    if instructions is not None:
        return None
    # The provider reads only at breakpoints present in the request, so every earlier anchor keeps its mark for the
    # next call to read what this one writes; it considers the latest 50, which must still include the system mark.
    marked: list[int] = []
    for anchor in anchors[-49:] if isinstance(input, list) else ():
        anchor_input, _ = to_responses([wire_system, *to_messages(input[: anchor + 1])])
        # The anchor's Responses items must be a prefix of the full request, or the key would mark a different item.
        if len(anchor_input) > 1 and anchor_input == responses_input[: len(anchor_input)]:
            marked.append(len(anchor_input) - 1)
    for index in marked:
        _mark_last_text_part(responses_input[index])
    logical_messages.insert(0, {"role": "system", "content": str(system_instructions)})
    logical_messages = cast(list[Any], _to_dump_compatible(logical_messages))
    responses_tools = responses_api_bridge.transformation_handler._convert_tools_to_responses_format(converted_tools)

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
