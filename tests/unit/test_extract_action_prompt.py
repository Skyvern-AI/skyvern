"""Contract tests for the flag-gated planner actions (GOTO_URL / RELOAD_PAGE / EXTRACT_INFORMATION)."""

from __future__ import annotations

from typing import Any

import pytest

from skyvern.forge.sdk.prompting import PromptEngine

_EXTRACT_ACTION_KWARGS: dict[str, Any] = {
    "navigation_goal": "test goal",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.com",
    "current_url": "https://example.com",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2025-01-01T00:00:00",
    "verification_code_check": False,
    "complete_criterion": None,
    "terminate_criterion": None,
    "show_close_page_action": False,
    "open_tabs_context": None,
    "recent_dialog_messages_str": None,
    "llm_screenshots_enabled": True,
    "enriched_tree_enabled": False,
    "elements": "<html></html>",
}

_NEW_ACTIONS = ("GOTO_URL", "RELOAD_PAGE", "EXTRACT_INFORMATION")


@pytest.fixture
def prompt_engine() -> PromptEngine:
    return PromptEngine(model="skyvern")


@pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
def test_new_planner_actions_absent_when_flag_off(prompt_engine: PromptEngine, template: str) -> None:
    # Flag defaults off (kwarg omitted -> Jinja Undefined -> falsy). Even with an extraction goal,
    # none of the new actions should surface -> the control cohort matches the pre-PR prompt.
    rendered = prompt_engine.load_prompt(
        template, **{**_EXTRACT_ACTION_KWARGS, "data_extraction_goal": "extract the price"}
    )
    for action in _NEW_ACTIONS:
        assert action not in rendered
    assert '"url": str' not in rendered


@pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
def test_goto_url_and_reload_in_schema_when_flag_on(prompt_engine: PromptEngine, template: str) -> None:
    rendered = prompt_engine.load_prompt(template, **{**_EXTRACT_ACTION_KWARGS, "enable_new_planner_actions": True})
    assert '"GOTO_URL"' in rendered
    assert '"RELOAD_PAGE"' in rendered
    assert '"url": str' in rendered
    assert "TERMINATE, KEYPRESS, GOTO_URL, RELOAD_PAGE" in rendered
    # EXTRACT_INFORMATION still requires an extraction goal even when the flag is on.
    assert "EXTRACT_INFORMATION" not in rendered


@pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
def test_extract_information_requires_flag_and_goal(prompt_engine: PromptEngine, template: str) -> None:
    without_goal = prompt_engine.load_prompt(template, **{**_EXTRACT_ACTION_KWARGS, "enable_new_planner_actions": True})
    assert "EXTRACT_INFORMATION" not in without_goal

    without_flag = prompt_engine.load_prompt(
        template, **{**_EXTRACT_ACTION_KWARGS, "data_extraction_goal": "extract the price"}
    )
    assert "EXTRACT_INFORMATION" not in without_flag

    with_both = prompt_engine.load_prompt(
        template,
        **{**_EXTRACT_ACTION_KWARGS, "enable_new_planner_actions": True, "data_extraction_goal": "extract the price"},
    )
    assert '"EXTRACT_INFORMATION"' in with_both
    assert "KEYPRESS, GOTO_URL, RELOAD_PAGE, EXTRACT_INFORMATION" in with_both


def test_static_template_is_prefix_when_flag_on(prompt_engine: PromptEngine) -> None:
    kwargs = {**_EXTRACT_ACTION_KWARGS, "enable_new_planner_actions": True, "data_extraction_goal": "extract the price"}
    full = prompt_engine.load_prompt("extract-action", **kwargs)
    static = prompt_engine.load_prompt("extract-action-static", **kwargs)
    assert full.startswith(static.rstrip())
