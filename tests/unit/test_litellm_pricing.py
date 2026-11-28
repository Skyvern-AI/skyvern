import pytest

from skyvern.forge.sdk.api.llm.litellm_pricing import long_prompt_cost_multiplier


@pytest.mark.parametrize(
    ("model_name", "prompt_tokens", "expected"),
    [
        # Haiku 5.5's 100k tier is the one LiteLLM 1.89 drops; every rate above it is 5x.
        ("anthropic/claude-haiku-5-5", 100_000, 1.0),
        ("anthropic/claude-haiku-5-5", 100_001, 5.0),
        ("claude-haiku-5-5", 100_001, 5.0),
        ("anthropic/claude-haiku-5-5-2026-10-01", 100_001, 5.0),
        ("bedrock/us.anthropic.claude-haiku-5-5", 100_001, 5.0),
        ("global.anthropic.claude-haiku-5-5", 100_001, 5.0),
        # OpenRouter's dotted id for the same model, used when OpenRouter returns no billed cost.
        ("openrouter/anthropic/claude-haiku-5.5", 100_000, 5.0),
        # LiteLLM prices its own 200k and 272k tiers, so they must not be applied twice.
        ("gpt-5.6-sol", 300_000, 1.0),
        ("xai/grok-4.5", 300_000, 1.0),
        # No prompt-size tier at all.
        ("anthropic/claude-sonnet-4-6", 300_000, 1.0),
        ("bedrock/us.anthropic.claude-opus-5-5", 100_001, 1.0),
        ("not-a-real-model", 1_000_000, 1.0),
        (None, 1_000_000, 1.0),
        ("anthropic/claude-haiku-5-5", None, 1.0),
    ],
)
def test_long_prompt_cost_multiplier(model_name: str | None, prompt_tokens: int | None, expected: float) -> None:
    assert long_prompt_cost_multiplier(model_name, prompt_tokens) == pytest.approx(expected)
