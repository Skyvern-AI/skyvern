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
        show_close_page_action=False,
        open_tabs_context=None,
    )

    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    assert "UNIQUE_ACTION_BLOCK_0_" not in rendered


def test_enforce_ceiling_raises_when_unfixable() -> None:
    import pytest

    from skyvern.exceptions import SkyvernContextWindowExceededError
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, enforce_prompt_ceiling_tracked
    from skyvern.utils.token_counter import count_tokens

    oversized_goal = "extract " + ("important data " * 200_000)
    prompt = engine_module.load_prompt(
        "data-extraction-summary",
        data_extraction_goal=oversized_goal,
        data_extraction_schema=None,
        local_datetime="2026-05-19T12:00:00",
    )
    assert count_tokens(prompt) > PROMPT_HARD_CEILING_TOKENS

    with pytest.raises(SkyvernContextWindowExceededError):
        enforce_prompt_ceiling_tracked(
            prompt,
            prompt_engine=engine_module,
            template_name="data-extraction-summary",
            kwargs={
                "data_extraction_goal": oversized_goal,
                "data_extraction_schema": None,
                "local_datetime": "2026-05-19T12:00:00",
            },
        )


def test_extract_action_trims_element_tree_that_alone_exceeds_ceiling() -> None:
    """SKY-14634: a page whose element tree alone blows the ceiling must still render.

    Dropping every fallback key cannot rescue such a prompt, so before this the step died
    with SkyvernContextWindowExceededError even though no provider had rejected anything.
    """
    from skyvern.forge.prompts import prompt_engine as engine_module
    from skyvern.utils.prompt_engine import PROMPT_HARD_CEILING_TOKENS, load_prompt_with_elements
    from skyvern.utils.token_counter import count_tokens

    oversized_tree = "".join(f'<div id="{i}">lorem ipsum dolor sit amet</div>' for i in range(30_000))
    assert count_tokens(oversized_tree) > PROMPT_HARD_CEILING_TOKENS

    builder = _make_element_tree_builder()
    builder.build_element_tree = MagicMock(return_value=oversized_tree)

    rendered = load_prompt_with_elements(
        element_tree_builder=builder,
        prompt_engine=engine_module,
        template_name="extract-action",
        navigation_goal="Log in to the site",
        navigation_payload_str='{"username": "UNIQUE_USER_DETAIL"}',
        starting_url="https://example.test",
        current_url="https://example.test",
        data_extraction_goal=None,
        action_history="UNIQUE_ACTION_HISTORY",
        error_code_mapping_str=None,
        local_datetime="2026-04-14T12:00:00",
        verification_code_check=False,
        complete_criterion=None,
        terminate_criterion=None,
        show_close_page_action=False,
        open_tabs_context=None,
    )

    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
    # The tree is the bulk, so it is what gets trimmed — the fallback drops are not spent
    # discarding the user's details and action history for a prompt they cannot rescue.
    assert "UNIQUE_ACTION_HISTORY" in rendered
    assert "UNIQUE_USER_DETAIL" in rendered
    # The extraction cache hashes last_used_element_tree_html, so it has to track the
    # trimmed tree the LLM actually saw rather than the full one.
    assert builder.last_used_element_tree_html in rendered
    assert len(builder.last_used_element_tree_html) < len(oversized_tree)


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
        show_close_page_action=False,
        open_tabs_context=None,
    )

    assert "small history" in rendered
    assert count_tokens(rendered) <= PROMPT_HARD_CEILING_TOKENS
