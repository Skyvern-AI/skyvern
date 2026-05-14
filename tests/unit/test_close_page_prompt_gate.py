"""Tests for the CLOSE_PAGE prompt gate."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.forge.agent import ForgeAgent, _step_last_action_is_close_page
from skyvern.forge.prompts import prompt_engine
from skyvern.webeye.actions.action_types import ActionType

_BASE_KWARGS: dict[str, Any] = {
    "navigation_goal": "close the stuck tab and continue",
    "navigation_payload_str": "{}",
    "starting_url": "https://example.test/start",
    "current_url": "https://example.test/form",
    "data_extraction_goal": None,
    "action_history": "[]",
    "error_code_mapping_str": None,
    "local_datetime": "2026-05-14T00:00:00Z",
    "verification_code_check": False,
    "complete_criterion": None,
    "terminate_criterion": None,
    "parse_select_feature_enabled": False,
    "recent_dialog_messages_str": None,
    "elements": "<html></html>",
}


class TestBuildExtractActionCacheVariant:
    def test_cp_tag_present_when_multiple_tabs(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=True,
            complete_criterion=None,
        )
        assert "cp" in result

    def test_cp_tag_absent_when_single_tab(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=False,
            complete_criterion=None,
        )
        assert "cp" not in result

    def test_ml_tag_never_appears(self) -> None:
        for flag in (True, False):
            result = ForgeAgent._build_extract_action_cache_variant(
                verification_code_check=False,
                show_close_page_action=flag,
                complete_criterion=None,
            )
            assert "ml" not in result

    def test_std_when_no_flags(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=False,
            show_close_page_action=False,
            complete_criterion=None,
        )
        assert result == "std"

    def test_vc_and_cp_combined(self) -> None:
        result = ForgeAgent._build_extract_action_cache_variant(
            verification_code_check=True,
            show_close_page_action=True,
            complete_criterion=None,
        )
        assert "vc" in result
        assert "cp" in result


class TestExtractActionTemplateRendering:
    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_close_page_shown_when_multiple_tabs(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(template, show_close_page_action=True, **_BASE_KWARGS)
        assert '"CLOSE_PAGE"' in rendered
        assert "close the current page" in rendered.lower()

    @pytest.mark.parametrize("template", ["extract-action", "extract-action-static"])
    def test_close_page_hidden_when_single_tab(self, template: str) -> None:
        rendered = prompt_engine.load_prompt(template, show_close_page_action=False, **_BASE_KWARGS)
        assert '"CLOSE_PAGE"' not in rendered


class TestShouldVerifyAfterClosePage:
    def test_close_page_triggers_guard(self) -> None:
        step = MagicMock()
        step.output.actions_and_results = [(MagicMock(action_type=ActionType.CLOSE_PAGE), [])]
        assert _step_last_action_is_close_page(step) is True

    def test_complete_does_not_trigger_guard(self) -> None:
        step = MagicMock()
        step.output.actions_and_results = [(MagicMock(action_type=ActionType.COMPLETE), [])]
        assert _step_last_action_is_close_page(step) is False

    def test_empty_actions_does_not_trigger_guard(self) -> None:
        step = MagicMock()
        step.output.actions_and_results = []
        assert _step_last_action_is_close_page(step) is False

    def test_none_output_does_not_trigger_guard(self) -> None:
        step = MagicMock()
        step.output = None
        assert _step_last_action_is_close_page(step) is False
