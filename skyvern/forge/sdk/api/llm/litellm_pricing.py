"""Corrections for known gaps in the pinned LiteLLM price lookup."""

from functools import lru_cache

import llm_prices

# LiteLLM 1.89 keeps only these prompt-size tiers in its model info and prices them itself. Any other
# `*_above_<n>_tokens` key is dropped (Haiku 5.5's 100k tier, for example), so a long prompt for that
# model prices at the base rate. llm-prices lists the threshold as the last token count still billed
# at the base rate, which is one below LiteLLM's key for OpenRouter-style rows.
_LITELLM_PRICED_THRESHOLDS = frozenset({128_000, 200_000, 272_000})


@lru_cache(maxsize=256)
def _unpriced_long_prompt_tier(model_name: str) -> tuple[int, float] | None:
    """(threshold, multiplier) for a prompt-size tier LiteLLM drops, if every rate scales by one factor."""
    price = llm_prices.lookup(model_name)
    tier = price.long_context if price else None
    if price is None or tier is None:
        return None
    if tier.threshold_tokens in _LITELLM_PRICED_THRESHOLDS or tier.threshold_tokens + 1 in _LITELLM_PRICED_THRESHOLDS:
        return None
    pairs = [(price.input, tier.input), (price.output, tier.output), (price.cache_read, tier.cache_read)]
    pairs += [(rate, tier.cache_write.get(ttl)) for ttl, rate in price.cache_write.items()]
    ratios = {round(long / base, 6) for base, long in pairs if base and long is not None}
    # The result scales LiteLLM's total, which is only exact when every rate moves together.
    if len(ratios) != 1:
        return None
    return tier.threshold_tokens, ratios.pop()


def long_prompt_cost_multiplier(model_name: str | None, prompt_tokens: int | None) -> float:
    if not model_name or not prompt_tokens:
        return 1.0
    tier = _unpriced_long_prompt_tier(model_name)
    if tier is not None and prompt_tokens > tier[0]:
        return tier[1]
    return 1.0
