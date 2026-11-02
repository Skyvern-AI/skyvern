"""Corrections for known gaps in the pinned LiteLLM price lookup."""

# LiteLLM 1.89.1 drops `*_above_100k_tokens` price keys, so a long Haiku 5.5 prompt prices at the base rate.
# Every Haiku 5.5 rate above 100k prompt tokens is 5x its base rate, including cache reads and writes.
_CLAUDE_HAIKU_5_5_MODEL_SUFFIX = "claude-haiku-5-5"
_CLAUDE_HAIKU_5_5_LONG_PROMPT_THRESHOLD = 100_000
_CLAUDE_HAIKU_5_5_LONG_PROMPT_MULTIPLIER = 5.0


def long_prompt_cost_multiplier(model_name: str | None, prompt_tokens: int | None) -> float:
    if model_name and model_name.endswith(_CLAUDE_HAIKU_5_5_MODEL_SUFFIX):
        if prompt_tokens and prompt_tokens > _CLAUDE_HAIKU_5_5_LONG_PROMPT_THRESHOLD:
            return _CLAUDE_HAIKU_5_5_LONG_PROMPT_MULTIPLIER
    return 1.0
