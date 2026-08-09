"""Tests for the flag-gated stable-prefix ordering of extract-action prompts (SKY-9983)."""

from typing import Any

import pytest

from skyvern.forge.prompts import prompt_engine

_BASE_KWARGS: dict[str, Any] = {
    "navigation_goal": "goal-sentinel",
    "navigation_payload_str": "payload-sentinel",
    "starting_url": "https://start.test",
    "current_url": "https://current.test/page",
    "data_extraction_goal": None,
    "action_history": "history-sentinel",
    "error_code_mapping_str": None,
    "local_datetime": "2026-08-02T00:00:00Z",
    "verification_code_check": False,
    "complete_criterion": None,
    "terminate_criterion": None,
    "elements": "elements-sentinel",
    "recent_dialog_messages_str": "dialog-sentinel",
    "open_tabs_context": "tabs-sentinel",
    "show_close_page_action": True,
    "show_switch_tab_action": True,
}

TEMPLATES = ["extract-action", "extract-action-dynamic"]

DIALOG_GUIDANCE_MARKER = "the agent auto-accepted each one"


def _render(template: str, **overrides: Any) -> str:
    return prompt_engine.load_prompt(template, **{**_BASE_KWARGS, **overrides})


@pytest.mark.parametrize("template", TEMPLATES)
class TestDefaultOrdering:
    def test_history_renders_before_elements(self, template: str) -> None:
        rendered = _render(template)
        assert rendered.index("history-sentinel") < rendered.index("elements-sentinel")

    def test_flag_false_matches_flag_absent(self, template: str) -> None:
        assert _render(template) == _render(template, stable_prefix_ordering=False)


@pytest.mark.parametrize("template", TEMPLATES)
class TestStablePrefixOrdering:
    def test_elements_render_before_per_step_sections(self, template: str) -> None:
        rendered = _render(template, stable_prefix_ordering=True)
        elements_at = rendered.index("elements-sentinel")
        assert rendered.index("Current URL: https://current.test/page") < elements_at
        assert elements_at < rendered.index("history-sentinel")
        assert elements_at < rendered.index("dialog-sentinel")
        assert elements_at < rendered.index("tabs-sentinel")

    def test_dialog_guidance_moves_after_untrusted_block(self, template: str) -> None:
        rendered = _render(template, stable_prefix_ordering=True)
        assert rendered.index(DIALOG_GUIDANCE_MARKER) > rendered.index("END_UNTRUSTED_WEB_PAGE_DATA")

    def test_guidance_sections_omitted_without_their_data(self, template: str) -> None:
        rendered = _render(
            template,
            stable_prefix_ordering=True,
            recent_dialog_messages_str=None,
            open_tabs_context=None,
            show_close_page_action=False,
            show_switch_tab_action=False,
        )
        assert DIALOG_GUIDANCE_MARKER not in rendered
        assert "open browser tabs listed above" not in rendered

    def test_untrusted_values_stay_inside_delimiters(self, template: str) -> None:
        rendered = _render(
            template,
            stable_prefix_ordering=True,
            complete_criterion="criterion-sentinel",
            complete_criterion_is_untrusted=True,
        )
        begin = rendered.index("BEGIN_UNTRUSTED_WEB_PAGE_DATA")
        end = rendered.index("END_UNTRUSTED_WEB_PAGE_DATA")
        for sentinel in (
            "criterion-sentinel",
            "https://current.test/page",
            "elements-sentinel",
            "history-sentinel",
            "dialog-sentinel",
            "tabs-sentinel",
        ):
            assert begin < rendered.index(sentinel) < end, sentinel

    def test_datetime_stays_last(self, template: str) -> None:
        rendered = _render(template, stable_prefix_ordering=True)
        assert rendered.index("2026-08-02T00:00:00Z") > rendered.index("END_UNTRUSTED_WEB_PAGE_DATA")


def test_static_template_ignores_flag() -> None:
    off = prompt_engine.load_prompt("extract-action-static", stable_prefix_ordering=False, **_BASE_KWARGS)
    on = prompt_engine.load_prompt("extract-action-static", stable_prefix_ordering=True, **_BASE_KWARGS)
    assert off == on
