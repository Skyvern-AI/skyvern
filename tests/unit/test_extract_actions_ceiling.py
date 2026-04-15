"""Tests for the 180k ceiling applied to extract-action templates (SKY-8920 Phase E)."""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_element_tree_builder() -> MagicMock:
    builder = MagicMock()
    builder.build_element_tree = MagicMock(return_value="<a>link</a>")
    builder.support_economy_elements_tree = MagicMock(return_value=False)
    return builder


def test_extract_action_ceiling_drops_action_history_on_overshoot() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements
    from skyvern.utils.token_counter import count_tokens

    oversized_history = "\n".join(f"UNIQUE_ACTION_BLOCK_{i}_" + ("lorem ipsum " * 200) for i in range(3000))

    rendered = load_prompt_with_elements(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-action",
        navigation_goal="Log in to the site",
        navigation_payload_str="{}",
        starting_url="https://example.test",
        current_url="https://example.test",
        data_extraction_goal=None,
        action_history=oversized_history,
        error_code_mapping_str=None,
        local_datetime="2026-04-14T12:00:00",
        verification_code_check=False,
        complete_criterion=None,
        terminate_criterion=None,
        parse_select_feature_enabled=False,
        has_magic_link_page=False,
    )

    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    assert "UNIQUE_ACTION_BLOCK_0_" not in rendered


def test_extract_action_small_prompt_passes_through() -> None:
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements
    from skyvern.utils.token_counter import count_tokens

    rendered = load_prompt_with_elements(
        element_tree_builder=_make_element_tree_builder(),
        prompt_engine=engine_module,
        template_name="extract-action",
        navigation_goal="Log in to the site",
        navigation_payload_str="{}",
        starting_url="https://example.test",
        current_url="https://example.test",
        data_extraction_goal=None,
        action_history="small history",
        error_code_mapping_str=None,
        local_datetime="2026-04-14T12:00:00",
        verification_code_check=False,
        complete_criterion=None,
        terminate_criterion=None,
        parse_select_feature_enabled=False,
        has_magic_link_page=False,
    )

    assert "small history" in rendered
    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
