"""Tests for the post-render 180k token ceiling in load_prompt_with_elements (SKY-8920 Phase C + E)."""

from __future__ import annotations

from unittest.mock import MagicMock


def test_prompt_hard_ceiling_is_below_gpt5_mini_cap() -> None:
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS

    assert PROMPT_HARD_CEILING_TOKENS == 180_000
    assert PROMPT_HARD_CEILING_TOKENS < 272_000


def test_ceiling_fallback_keys_by_template_has_known_mappings() -> None:
    from skyvern.utils.prompt_engine import CEILING_FALLBACK_KEYS_BY_TEMPLATE

    assert CEILING_FALLBACK_KEYS_BY_TEMPLATE["extract-information"] == [
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

    # List of distinct English-ish words well over the 180k token ceiling.
    oversized_prev = [{"iter": i, "marker": f"UNIQUE_BLOCK_{i}_" + ("lorem ipsum " * 200)} for i in range(3000)]

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


def test_enforce_prompt_ceiling_drops_fallback_keys_without_elements() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, enforce_prompt_ceiling
    from skyvern.utils.token_counter import count_tokens

    giant_schema = {"type": "object", "_blob": "lorem " * 300_000}
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
