"""SKY-9718 — html_token_count / html_pct breakdown plumbing tests.

Covers the SkyvernContext ferry: prompt-build site writes, LLM-handler
log site reads + clears. Doesn't run the actual LLM call — just the
breakdown plumbing helper and the prompt_engine writer.
"""

from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.api.llm.api_handler_factory import _consume_prompt_breakdown
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext


def test_consume_prompt_breakdown_returns_empty_when_context_is_none() -> None:
    assert _consume_prompt_breakdown(None) == {}


def test_consume_prompt_breakdown_returns_empty_when_unset() -> None:
    ctx = SkyvernContext()
    assert _consume_prompt_breakdown(ctx) == {}
    # Nothing to clear.
    assert ctx.last_prompt_breakdown is None


def test_consume_prompt_breakdown_extracts_and_clears() -> None:
    ctx = SkyvernContext()
    ctx.last_prompt_breakdown = {
        "html_token_count": 12345,
        "total_tokens_local": 50000,
        "html_pct": 0.2469,
        "template_name": "check-user-goal",
    }
    out = _consume_prompt_breakdown(ctx)
    assert out == {
        "html_token_count": 12345,
        "html_pct": 0.2469,
        "total_tokens_local": 50000,
        "prompt_template_name": "check-user-goal",
    }
    # Cleared so the next LLM call doesn't inherit a stale value.
    assert ctx.last_prompt_breakdown is None


def test_consume_handles_partial_breakdown_dict() -> None:
    """Defensive — don't crash if the writer ever stops emitting one of the keys."""
    ctx = SkyvernContext()
    ctx.last_prompt_breakdown = {"html_token_count": 7}
    out = _consume_prompt_breakdown(ctx)
    assert out["html_token_count"] == 7
    assert out["html_pct"] is None
    assert out["total_tokens_local"] is None
    assert out["prompt_template_name"] is None


def test_load_prompt_with_elements_writes_breakdown_to_context() -> None:
    """End-to-end: load_prompt_with_elements_tracked stashes html_token_count."""
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    ctx = SkyvernContext()
    token = skyvern_context._context.set(ctx)
    try:
        # Fake element-tree builder + prompt engine — we only care about plumbing.
        builder = MagicMock()
        builder.build_element_tree.return_value = "<html><body>" + "x" * 1000 + "</body></html>"
        builder.support_economy_elements_tree.return_value = False

        engine = MagicMock()
        # Render a small prompt so we don't hit the economy-tree fallback.
        engine.load_prompt.return_value = "system\n" + builder.build_element_tree.return_value + "\nuser"

        load_prompt_with_elements_tracked(
            element_tree_builder=builder,
            prompt_engine=engine,
            template_name="check-user-goal",
        )

        assert ctx.last_prompt_breakdown is not None
        assert ctx.last_prompt_breakdown["html_token_count"] > 0
        assert ctx.last_prompt_breakdown["total_tokens_local"] > 0
        assert ctx.last_prompt_breakdown["template_name"] == "check-user-goal"
        # html_pct must be in (0, 1] for a real prompt that contains its elements.
        assert 0 < ctx.last_prompt_breakdown["html_pct"] <= 1.0
    finally:
        skyvern_context._context.reset(token)


def test_load_prompt_breakdown_survives_context_missing() -> None:
    """The instrumentation is wrapped in try/except — never break the LLM call."""
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    # No context set.
    builder = MagicMock()
    builder.build_element_tree.return_value = "<html></html>"
    builder.support_economy_elements_tree.return_value = False
    engine = MagicMock()
    engine.load_prompt.return_value = "prompt body"

    # Should not raise even though there's no SkyvernContext active.
    prompt, kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=builder,
        prompt_engine=engine,
        template_name="check-user-goal",
    )
    assert isinstance(prompt, str)
    assert isinstance(kwargs, dict)


@pytest.mark.parametrize(
    "html, total_pct_lower",
    [
        ("<a></a>", 0.0),
        ("<div>" + "x" * 5000 + "</div>", 0.5),
    ],
)
def test_html_pct_scales_with_html_size(html: str, total_pct_lower: float) -> None:
    """Larger HTML produces larger html_pct given a fixed surrounding prompt."""
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    ctx = SkyvernContext()
    token = skyvern_context._context.set(ctx)
    try:
        builder = MagicMock()
        builder.build_element_tree.return_value = html
        builder.support_economy_elements_tree.return_value = False
        engine = MagicMock()
        engine.load_prompt.return_value = f"fixed prefix {html} fixed suffix"

        load_prompt_with_elements_tracked(
            element_tree_builder=builder,
            prompt_engine=engine,
            template_name="extract-action",
        )
        assert ctx.last_prompt_breakdown["html_pct"] >= total_pct_lower
    finally:
        skyvern_context._context.reset(token)
