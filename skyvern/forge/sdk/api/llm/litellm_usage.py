import functools
from typing import Any

from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.types.utils import Usage


def use_anthropic_reported_thinking_tokens() -> None:
    # litellm < 1.99: AnthropicConfig.calculate_usage counts the thinking text, which Claude 5.x returns empty, and
    # ignores the API's thinking_tokens. Delete once litellm >= 1.99 is pinned; a unit test fails when it is.
    original = AnthropicConfig.calculate_usage

    @functools.wraps(original)
    def calculate_usage(self: AnthropicConfig, usage_object: dict, *args: Any, **kwargs: Any) -> Usage:
        usage = original(self, usage_object, *args, **kwargs)
        details = usage_object.get("output_tokens_details")
        thinking_tokens = details.get("thinking_tokens") if isinstance(details, dict) else None
        if isinstance(thinking_tokens, int) and usage.completion_tokens_details is not None:
            reasoning_tokens = min(max(thinking_tokens, 0), usage.completion_tokens)
            usage.completion_tokens_details.reasoning_tokens = reasoning_tokens
            # litellm prices text and reasoning tokens separately; they must still sum to completion_tokens.
            usage.completion_tokens_details.text_tokens = usage.completion_tokens - reasoning_tokens
        return usage

    AnthropicConfig.calculate_usage = calculate_usage  # type: ignore[method-assign]
