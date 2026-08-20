from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

UsageScalar: TypeAlias = str | int | float
CacheMode: TypeAlias = Literal["implicit", "explicit"]

_PROVIDER_NAMES = {
    "anthropic": "anthropic",
    "aws": "aws.bedrock",
    "bedrock": "aws.bedrock",
    "azure": "azure.ai.openai",
    "azure_ai": "azure.ai.openai",
    "azure_openai": "azure.ai.openai",
    "cohere": "cohere",
    "deepseek": "deepseek",
    "gemini": "gcp.gemini",
    "google": "gcp.gen_ai",
    "groq": "groq",
    "mistral": "mistral_ai",
    "mistral_ai": "mistral_ai",
    "openai": "openai",
    "perplexity": "perplexity",
    "vertex": "gcp.vertex_ai",
    "vertex_ai": "gcp.vertex_ai",
    "watsonx": "ibm.watsonx.ai",
    "xai": "x_ai",
    "x_ai": "x_ai",
}
_CANONICAL_PROVIDER_NAMES = frozenset(_PROVIDER_NAMES.values())


class UsageEventLogger(Protocol):
    def info(self, event: str, **fields: UsageScalar) -> None: ...


def is_workflow_copilot_prompt_name(prompt_name: str | None) -> bool:
    if prompt_name is None:
        return False
    return prompt_name == "workflow-copilot" or prompt_name.startswith("workflow-copilot-")


def normalize_gen_ai_provider(provider_name: str | None, model: str) -> str | None:
    if provider_name:
        provider_name = provider_name.strip().lower()
        if provider_name in _CANONICAL_PROVIDER_NAMES:
            return provider_name
        normalized_provider = provider_name.replace("-", "_").replace(".", "_")
        provider = _PROVIDER_NAMES.get(normalized_provider)
        if provider is not None:
            return provider

    model_provider, separator, _ = model.strip().lower().partition("/")
    if separator:
        provider = _PROVIDER_NAMES.get(model_provider.replace("-", "_").replace(".", "_"))
        if provider is not None:
            return provider
    if model_provider.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return None


@dataclass(frozen=True, slots=True)
class CopilotModelUsageEvent:
    request_model: str
    response_model: str | None = None
    provider_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost: float | None = None
    prompt_name: str | None = None
    model_call_index: int | None = None
    cache_mode: CacheMode | None = None
    cache_breakpoint_count: int | None = None
    cache_stable_prefix_chars: int | None = None

    def log_fields(self) -> dict[str, UsageScalar]:
        provider_name = None
        if self.provider_name is not None:
            provider_name = normalize_gen_ai_provider(
                self.provider_name,
                self.response_model or self.request_model,
            )
        if provider_name is None and self.response_model is not None and "/" in self.response_model:
            provider_name = normalize_gen_ai_provider(None, self.response_model)
        if provider_name is None:
            provider_name = normalize_gen_ai_provider(None, self.request_model)
        if provider_name is None and self.response_model is not None:
            provider_name = normalize_gen_ai_provider(None, self.response_model)
        fields: dict[str, UsageScalar] = {
            "log_code": "copilot_model_usage",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": self.request_model,
        }
        optional_fields: tuple[tuple[str, UsageScalar | None], ...] = (
            ("gen_ai.response.model", self.response_model),
            (
                "gen_ai.provider.name",
                provider_name,
            ),
            ("gen_ai.usage.input_tokens", self.input_tokens),
            ("gen_ai.usage.output_tokens", self.output_tokens),
            ("gen_ai.usage.cache_read.input_tokens", self.cache_read_tokens),
            ("gen_ai.usage.cache_creation.input_tokens", self.cache_creation_tokens),
            ("operation.cost", self.cost),
            ("copilot.prompt_name", self.prompt_name),
            ("copilot.model_call_index", self.model_call_index),
            ("copilot.cache.mode", self.cache_mode),
            ("copilot.cache.breakpoint_count", self.cache_breakpoint_count),
            ("copilot.cache.stable_prefix_chars", self.cache_stable_prefix_chars),
        )
        fields.update((key, value) for key, value in optional_fields if value is not None)
        return fields


def emit_copilot_model_usage(event: CopilotModelUsageEvent, *, logger: UsageEventLogger) -> None:
    logger.info("Copilot model usage", **event.log_fields())


def emit_direct_copilot_model_usage(event: CopilotModelUsageEvent, *, logger: UsageEventLogger) -> bool:
    if not is_workflow_copilot_prompt_name(event.prompt_name):
        return False
    emit_copilot_model_usage(event, logger=logger)
    return True
