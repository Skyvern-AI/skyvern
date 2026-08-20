"""Tests for the post-render 180k token ceiling in load_prompt_with_elements (SKY-8920 Phase C + E)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def small_prompt_ceiling(monkeypatch: pytest.MonkeyPatch) -> int:
    from skyvern.utils import prompt_engine

    ceiling = 500
    monkeypatch.setattr(prompt_engine, "PROMPT_HARD_CEILING_TOKENS", ceiling)
    return ceiling


def test_prompt_hard_ceiling_is_below_gpt5_mini_cap() -> None:
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS

    assert PROMPT_HARD_CEILING_TOKENS == 180_000
    assert PROMPT_HARD_CEILING_TOKENS < 272_000


def test_ceiling_fallback_keys_by_template_has_known_mappings() -> None:
    from skyvern.utils.prompt_engine import CEILING_FALLBACK_KEYS_BY_TEMPLATE

    assert CEILING_FALLBACK_KEYS_BY_TEMPLATE["extract-information"] == [
        "virtualized_grid_rows",
        "previous_extracted_information",
        "extracted_information_schema",
        "extracted_text",
    ]
    assert CEILING_FALLBACK_KEYS_BY_TEMPLATE["extract-action"] == [
        "action_history",
        "navigation_payload_str",
    ]
    assert CEILING_FALLBACK_KEYS_BY_TEMPLATE["data-extraction-summary"] == [
        "data_extraction_schema",
    ]


def _make_element_tree_builder() -> MagicMock:
    builder = MagicMock()
    builder.build_element_tree = MagicMock(return_value="<a>link</a>")
    builder.support_economy_elements_tree = MagicMock(return_value=False)
    return builder


def test_load_prompt_with_elements_drops_previous_info_when_over_ceiling() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements
    from skyvern.utils.token_counter import count_tokens

    oversized_prev = [{"iter": 0, "marker": "UNIQUE_BLOCK_0_" + ("lorem " * 185_000)}]

    rendered = load_prompt_with_elements(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-information",
        data_extraction_goal="Extract documents",
        extracted_information_schema={"type": "object"},
        current_url="https://example.test",
        extracted_text=None,
        error_code_mapping_str=None,
        navigation_payload=None,
        local_datetime="2026-04-14T12:00:00",
        previous_extracted_information=oversized_prev,
    )

    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    assert "UNIQUE_BLOCK_0_" not in rendered


def test_enforce_prompt_ceiling_drops_fallback_keys_without_elements(small_prompt_ceiling: int) -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, enforce_prompt_ceiling
    from skyvern.utils.token_counter import count_tokens

    giant_schema = {"type": "object", "_blob": "lorem " * (small_prompt_ceiling + 100)}
    kwargs = {
        "data_extraction_goal": "Extract",
        "data_extraction_schema": giant_schema,
        "current_url": "https://example.test",
        "local_datetime": "2026-04-14T12:00:00",
    }
    rendered = engine_module.load_prompt("data-extraction-summary", **kwargs)
    assert count_tokens(rendered) > PROMPT_HARD_CEILING_TOKENS

    rendered = enforce_prompt_ceiling(
        rendered,
        prompt_engine=engine_module,
        template_name="data-extraction-summary",
        kwargs=kwargs,
    )
    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS


def test_load_prompt_with_elements_respects_ceiling_for_small_prompts() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements
    from skyvern.utils.token_counter import count_tokens

    rendered = load_prompt_with_elements(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-information",
        data_extraction_goal="Extract documents",
        extracted_information_schema={"type": "object"},
        current_url="https://example.test",
        extracted_text=None,
        error_code_mapping_str=None,
        navigation_payload=None,
        local_datetime="2026-04-14T12:00:00",
        previous_extracted_information="small blob",
    )

    assert "small blob" in rendered
    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS


def test_load_prompt_with_elements_tracked_drops_extracted_text_as_last_resort(
    small_prompt_ceiling: int,
) -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements_tracked
    from skyvern.utils.token_counter import count_tokens

    oversized_extracted_text = "UNIQUE_EXTRACTED_TEXT " + ("lorem " * (small_prompt_ceiling + 100))

    rendered, post_kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-information",
        data_extraction_goal="Extract documents",
        extracted_information_schema=None,
        current_url="https://example.test",
        extracted_text=oversized_extracted_text,
        error_code_mapping_str=None,
        navigation_payload=None,
        local_datetime="2026-04-14T12:00:00",
        previous_extracted_information=None,
    )

    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    assert "UNIQUE_EXTRACTED_TEXT" not in rendered
    assert post_kwargs["extracted_text"] is None


def test_extract_information_ceiling_drops_grid_rows_before_required_inputs(small_prompt_ceiling: int) -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    schema_marker = "REQUIRED_SCHEMA_MARKER"
    text_marker = "REQUIRED_EXTRACTED_TEXT_MARKER"
    rendered, post_kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-information",
        data_extraction_goal="Extract documents",
        extracted_information_schema={"type": "object", "description": schema_marker},
        current_url="https://example.test",
        extracted_text=text_marker,
        error_code_mapping_str=None,
        navigation_payload=None,
        local_datetime="2026-04-14T12:00:00",
        previous_extracted_information=None,
        virtualized_grid_rows="OPTIONAL_GRID_MARKER " + ("lorem " * (small_prompt_ceiling + 100)),
    )

    assert "OPTIONAL_GRID_MARKER" not in rendered
    assert schema_marker in rendered
    assert text_marker in rendered
    assert post_kwargs["virtualized_grid_rows"] is None
    assert post_kwargs["extracted_information_schema"] is not None
    assert post_kwargs["extracted_text"] == text_marker


def test_extract_information_ceiling_preserves_legacy_order_without_grid_rows(small_prompt_ceiling: int) -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    rendered, post_kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-information",
        data_extraction_goal="Extract documents",
        extracted_information_schema={"type": "object", "description": "RETAINED_SCHEMA_MARKER"},
        current_url="https://example.test",
        extracted_text="RETAINED_TEXT_MARKER",
        error_code_mapping_str=None,
        navigation_payload=None,
        local_datetime="2026-04-14T12:00:00",
        previous_extracted_information="LEGACY_PREVIOUS_MARKER " + ("lorem " * (small_prompt_ceiling + 100)),
        virtualized_grid_rows=None,
    )

    assert "LEGACY_PREVIOUS_MARKER" not in rendered
    assert "RETAINED_SCHEMA_MARKER" in rendered
    assert "RETAINED_TEXT_MARKER" in rendered
    assert post_kwargs["virtualized_grid_rows"] is None
    assert post_kwargs["previous_extracted_information"] is None
    assert post_kwargs["extracted_information_schema"] is not None
    assert post_kwargs["extracted_text"] == "RETAINED_TEXT_MARKER"


def test_enforce_prompt_ceiling_tracked_reports_dropped_keys(small_prompt_ceiling: int) -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, enforce_prompt_ceiling_tracked
    from skyvern.utils.token_counter import count_tokens

    giant_schema = {"type": "object", "_blob": "lorem " * (small_prompt_ceiling + 100)}
    kwargs = {
        "data_extraction_goal": "Extract",
        "data_extraction_schema": giant_schema,
        "current_url": "https://example.test",
        "local_datetime": "2026-04-14T12:00:00",
    }
    rendered = engine_module.load_prompt("data-extraction-summary", **kwargs)
    assert count_tokens(rendered) > PROMPT_HARD_CEILING_TOKENS

    rendered, post_kwargs = enforce_prompt_ceiling_tracked(
        rendered,
        prompt_engine=engine_module,
        template_name="data-extraction-summary",
        kwargs=kwargs,
    )
    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    assert post_kwargs["data_extraction_schema"] is None
    # kwargs dict is not mutated in place
    assert kwargs["data_extraction_schema"] is giant_schema


def test_enforce_prompt_ceiling_tracked_noop_under_ceiling() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import enforce_prompt_ceiling_tracked

    kwargs = {
        "data_extraction_goal": "Extract",
        "data_extraction_schema": {"type": "object"},
        "current_url": "https://example.test",
        "local_datetime": "2026-04-14T12:00:00",
    }
    rendered = engine_module.load_prompt("data-extraction-summary", **kwargs)

    rendered_after, post_kwargs = enforce_prompt_ceiling_tracked(
        rendered,
        prompt_engine=engine_module,
        template_name="data-extraction-summary",
        kwargs=kwargs,
    )
    assert rendered_after == rendered
    assert post_kwargs["data_extraction_schema"] == {"type": "object"}


def test_enforce_prompt_ceiling_tracked_error_log_reports_zero_drops_and_html_share(
    small_prompt_ceiling: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.exceptions import SkyvernContextWindowExceededError
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils import prompt_engine

    log = MagicMock()
    monkeypatch.setattr(prompt_engine, "LOG", log)

    # check-user-goal has no CEILING_FALLBACK_KEYS_BY_TEMPLATE entry, so no drop is attempted
    with pytest.raises(SkyvernContextWindowExceededError):
        prompt_engine.enforce_prompt_ceiling_tracked(
            "lorem " * (small_prompt_ceiling + 100),
            prompt_engine=engine_module,
            template_name="check-user-goal",
            kwargs={"action_history": "some history"},
            elements="<a>link</a>",
        )

    message = log.error.call_args.args[0]
    fields = log.error.call_args.kwargs
    assert "after all fallback drops" not in message
    assert fields["fallback_keys_configured"] == 0
    assert fields["drops_applied"] == 0
    assert fields["elements_char_count"] == len("<a>link</a>")


def test_load_prompt_with_elements_tracked_reports_dropped_keys(small_prompt_ceiling: int) -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements_tracked
    from skyvern.utils.token_counter import count_tokens

    oversized_prev = [{"iter": 0, "marker": "UNIQUE_BLOCK_0_" + ("lorem " * (small_prompt_ceiling + 100))}]

    rendered, post_kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-information",
        data_extraction_goal="Extract documents",
        extracted_information_schema={"type": "object"},
        current_url="https://example.test",
        extracted_text=None,
        error_code_mapping_str=None,
        navigation_payload=None,
        local_datetime="2026-04-14T12:00:00",
        previous_extracted_information=oversized_prev,
    )

    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    # previous_extracted_information is first in the drop chain and large enough to have been dropped
    assert post_kwargs["previous_extracted_information"] is None


class _CountTokensSpy:
    """Wrap ``prompt_engine.count_tokens`` to count invocations while delegating to the real encoder."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.utils import prompt_engine
        from skyvern.utils.token_counter import count_tokens as real_count_tokens

        self.calls = 0
        self._real = real_count_tokens

        def _spy(text: str) -> int:
            self.calls += 1
            return self._real(text)

        monkeypatch.setattr(prompt_engine, "count_tokens", _spy)


def _steady_builder_and_engine(html: str) -> tuple[MagicMock, MagicMock]:
    builder = MagicMock()
    builder.build_element_tree = MagicMock(return_value=html)
    builder.support_economy_elements_tree = MagicMock(return_value=False)
    builder.support_lean_elements_tree = MagicMock(return_value=False)
    builder.last_used_element_tree_html = None
    engine = MagicMock()
    engine.load_prompt = MagicMock(return_value="SYSTEM_PREFIX\n" + html + "\nUSER_SUFFIX")
    return builder, engine


def test_steady_path_makes_one_count_token_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below-ceiling no-economy steady path with active context makes exactly 1 count_tokens
    call: the full prompt once. The HTML breakdown is estimated, never encoded."""
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    spy = _CountTokensSpy(monkeypatch)
    ctx = SkyvernContext()
    token = skyvern_context._context.set(ctx)
    try:
        builder, engine = _steady_builder_and_engine("<html><body>" + "x " * 400 + "</body></html>")
        load_prompt_with_elements_tracked(
            element_tree_builder=builder,
            prompt_engine=engine,
            template_name="check-user-goal",
        )
        assert spy.calls == 1
    finally:
        skyvern_context._context.reset(token)


def test_steady_path_breakdown_matches_recomputed_token_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The telemetry total is the final prompt's exact count; no HTML fields are emitted."""
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked
    from skyvern.utils.token_counter import count_tokens

    html = "<html><body>" + "x " * 400 + "</body></html>"
    ctx = SkyvernContext()
    token = skyvern_context._context.set(ctx)
    try:
        builder, engine = _steady_builder_and_engine(html)
        rendered, _ = load_prompt_with_elements_tracked(
            element_tree_builder=builder,
            prompt_engine=engine,
            template_name="check-user-goal",
        )
        bd = ctx.last_prompt_breakdown
        assert bd["total_tokens_local"] == count_tokens(rendered)
        assert bd["template_name"] == "check-user-goal"
        assert "html_token_count" not in bd
        assert "html_pct" not in bd
    finally:
        skyvern_context._context.reset(token)


def test_enforce_prompt_ceiling_tracked_returns_two_tuple() -> None:
    """enforce_prompt_ceiling_tracked stays a 2-tuple for external callers/mocks."""
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import enforce_prompt_ceiling_tracked

    kwargs = {
        "data_extraction_goal": "Extract",
        "data_extraction_schema": {"type": "object"},
        "current_url": "https://example.test",
        "local_datetime": "2026-04-14T12:00:00",
    }
    rendered = engine_module.load_prompt("data-extraction-summary", **kwargs)
    result = enforce_prompt_ceiling_tracked(
        rendered,
        prompt_engine=engine_module,
        template_name="data-extraction-summary",
        kwargs=kwargs,
    )
    assert isinstance(result, tuple) and len(result) == 2
    prompt, post_kwargs = result
    assert isinstance(prompt, str) and isinstance(post_kwargs, dict)


def test_missing_context_encodes_once_and_returns_valid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """No active context: still a valid 2-tuple, no breakdown written, no crash."""
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked

    spy = _CountTokensSpy(monkeypatch)
    builder, engine = _steady_builder_and_engine("<html></html>")
    prompt, kwargs = load_prompt_with_elements_tracked(
        element_tree_builder=builder,
        prompt_engine=engine,
        template_name="check-user-goal",
    )
    assert isinstance(prompt, str) and isinstance(kwargs, dict)
    # No telemetry encode when context missing: only the 100k-gate encode runs.
    assert spy.calls == 1


def test_ceiling_helper_reuses_precomputed_count_under_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The count-aware helper trusts a byte-for-byte precomputed count under the ceiling (no re-encode)."""
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import _enforce_prompt_ceiling_counted

    spy = _CountTokensSpy(monkeypatch)
    prompt = "a short prompt well under the ceiling"
    final_prompt, kwargs, final_count = _enforce_prompt_ceiling_counted(
        prompt,
        prompt_engine=engine_module,
        template_name="check-user-goal",
        kwargs={},
        elements="<a>x</a>",
        precomputed_token_count=42,
    )
    assert final_prompt == prompt
    assert final_count == 42
    assert spy.calls == 0


def test_ceiling_helper_recounts_after_key_drop_mutation(small_prompt_ceiling: int) -> None:
    """Hard-ceiling drop loop must re-encode after every prompt mutation and return the FINAL prompt's count."""
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, _enforce_prompt_ceiling_counted
    from skyvern.utils.token_counter import count_tokens

    giant_schema = {"type": "object", "_blob": "lorem " * (small_prompt_ceiling + 100)}
    kwargs = {
        "data_extraction_goal": "Extract",
        "data_extraction_schema": giant_schema,
        "current_url": "https://example.test",
        "local_datetime": "2026-04-14T12:00:00",
    }
    rendered = engine_module.load_prompt("data-extraction-summary", **kwargs)
    over_count = count_tokens(rendered)
    assert over_count > PROMPT_HARD_CEILING_TOKENS

    final_prompt, post_kwargs, final_count = _enforce_prompt_ceiling_counted(
        rendered,
        prompt_engine=engine_module,
        template_name="data-extraction-summary",
        kwargs=kwargs,
        precomputed_token_count=over_count,
    )
    assert final_count <= PROMPT_HARD_CEILING_TOKENS
    # Returned count is the FINAL (mutated) prompt's real count, not the stale original.
    assert final_count == count_tokens(final_prompt)
    assert final_count != over_count
    assert post_kwargs["data_extraction_schema"] is None


def _economy_builder(p0_html: str, economy_html: str, two_thirds_html: str | None = None) -> MagicMock:
    builder = MagicMock()
    builder.build_element_tree = MagicMock(return_value=p0_html)
    builder.support_economy_elements_tree = MagicMock(return_value=True)
    builder.support_lean_elements_tree = MagicMock(return_value=False)
    builder.last_used_element_tree_html = None

    def _economy(*, html_need_skyvern_attrs: bool = True, percent_to_keep: float = 1) -> str:
        if percent_to_keep < 1 and two_thirds_html is not None:
            return two_thirds_html
        return economy_html

    builder.build_economy_elements_tree = MagicMock(side_effect=_economy)
    return builder


def test_economy_fallback_count_matches_rebuilt_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """When economy fallback rebuilds the prompt, the telemetry total corresponds to the economy prompt."""
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.utils import prompt_engine
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked
    from skyvern.utils.token_counter import count_tokens

    monkeypatch.setattr(prompt_engine, "DEFAULT_MAX_TOKENS", 20)
    p0_html = "P0 " + ("word " * 200)  # > 20 tokens -> triggers economy
    economy_html = "ECONOMY small"  # <= 20 tokens -> no two-thirds
    builder = _economy_builder(p0_html, economy_html)
    engine = MagicMock()
    engine.load_prompt = MagicMock(side_effect=lambda template_name, elements="", **k: f"PFX {elements} SFX")

    ctx = SkyvernContext()
    token = skyvern_context._context.set(ctx)
    try:
        rendered, _ = load_prompt_with_elements_tracked(
            element_tree_builder=builder,
            prompt_engine=engine,
            template_name="check-user-goal",
        )
        # Final prompt is the economy render; the reported total must be its real count.
        assert "ECONOMY" in rendered
        assert ctx.last_prompt_breakdown["total_tokens_local"] == count_tokens(rendered)
        # And NOT the stale P0 prompt's count.
        assert count_tokens(rendered) != count_tokens(f"PFX {p0_html} SFX")
    finally:
        skyvern_context._context.reset(token)


def test_two_thirds_economy_fallback_count_matches_final_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two-thirds fallback rebuilds again; the reported total corresponds to that final prompt."""
    from skyvern.forge.sdk.core import skyvern_context
    from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
    from skyvern.utils import prompt_engine
    from skyvern.utils.prompt_engine import load_prompt_with_elements_tracked
    from skyvern.utils.token_counter import count_tokens

    monkeypatch.setattr(prompt_engine, "DEFAULT_MAX_TOKENS", 20)
    p0_html = "P0 " + ("word " * 200)
    economy_html = "ECONOMY " + ("word " * 200)  # still > 20 -> triggers two-thirds
    two_thirds_html = "TWOTHIRDS tiny"  # <= 20
    builder = _economy_builder(p0_html, economy_html, two_thirds_html)
    engine = MagicMock()
    engine.load_prompt = MagicMock(side_effect=lambda template_name, elements="", **k: f"PFX {elements} SFX")

    ctx = SkyvernContext()
    token = skyvern_context._context.set(ctx)
    try:
        rendered, _ = load_prompt_with_elements_tracked(
            element_tree_builder=builder,
            prompt_engine=engine,
            template_name="check-user-goal",
        )
        assert "TWOTHIRDS" in rendered
        assert ctx.last_prompt_breakdown["total_tokens_local"] == count_tokens(rendered)
    finally:
        skyvern_context._context.reset(token)
