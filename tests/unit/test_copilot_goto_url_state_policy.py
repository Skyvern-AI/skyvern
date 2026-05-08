"""Tests for Copilot's goto_url dynamic-state shortcut policy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.copilot.tools import (
    _detect_click_driven_stable_search_drips,
    _detect_navigation_url_stable_search_setups,
    _detect_unverified_goto_url_state_shortcuts,
    _update_workflow,
    run_blocks_tool,
    update_and_run_blocks_tool,
    update_workflow_tool,
)

_AGENT_TEMPLATE_DEFAULTS = dict(
    workflow_knowledge_base="test kb",
    current_datetime="2026-01-01T00:00:00Z",
    tool_usage_guide="",
    security_rules="",
)


def _yaml(*blocks: dict) -> str:
    return yaml.safe_dump(
        {"title": "wf", "workflow_definition": {"blocks": list(blocks)}},
        sort_keys=False,
    )


def _render_agent_prompt() -> str:
    return prompt_engine.load_prompt("workflow-copilot-agent", **_AGENT_TEMPLATE_DEFAULTS)


class TestGotoUrlStateShortcutDetection:
    def test_plain_goto_url_to_extraction_is_allowed(self) -> None:
        submitted = _yaml(
            {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/news"},
            {"block_type": "extraction", "label": "extract_items", "data_extraction_goal": "Extract items"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_stable_search_params_to_extraction_are_allowed(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_results",
                "url": "https://example.com/search?q=budget+headphones&start=2026-08-20&end=2026-08-23",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_short_state_tokens_do_not_match_unrelated_substrings(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_embedded_report",
                "url": "https://example.com/report?embedded=true&classification=public",
            },
            {"block_type": "extraction", "label": "extract_report", "data_extraction_goal": "Extract report"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_dynamic_state_goto_url_directly_to_extraction_is_detected(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": ("https://example.com/search?q=budget+headphones&facet=color&facet=size&filter=available"),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == [
            {
                "label": "open_dynamic_results",
                "dynamic_state_keys": ["facet", "filter"],
                "next_data_block_label": "extract_results",
            }
        ]

    def test_repeated_opaque_query_key_directly_to_extraction_is_detected(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": "https://example.com/search?q=budget+headphones&choice=wireless&choice=noise-canceling",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == [
            {
                "label": "open_dynamic_results",
                "dynamic_state_keys": ["choice"],
                "next_data_block_label": "extract_results",
            }
        ]

    def test_dynamic_state_goto_url_with_validation_before_extraction_is_allowed(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": ("https://example.com/search?q=budget+headphones&facet=color&facet=size&filter=available"),
            },
            {
                "block_type": "validation",
                "label": "verify_state",
                "complete_criterion": (
                    "Verify query, dates, and each requested result constraint are all active from visible page state."
                ),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_dynamic_state_goto_url_with_unrelated_navigation_before_extraction_is_detected(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": (
                    "https://example.com/search?q=budget+headphones&"
                    "state=category%3Delectronics%3Bavailability%3Din_stock"
                ),
            },
            {
                "block_type": "navigation",
                "label": "dismiss_cookie_popup",
                "navigation_goal": "If a cookie popup is visible, close it so the main search form is usable.",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == [
            {
                "label": "open_dynamic_results",
                "dynamic_state_keys": ["state"],
                "next_data_block_label": "extract_results",
            }
        ]

    def test_dynamic_state_goto_url_with_filter_navigation_before_extraction_is_allowed(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": (
                    "https://example.com/search?q=budget+headphones&"
                    "state=category%3Delectronics%3Bavailability%3Din_stock"
                ),
            },
            {
                "block_type": "navigation",
                "label": "apply_missing_filters",
                "navigation_goal": (
                    "Inspect the active constraint chips and checked controls, then select any missing "
                    "requested options."
                ),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_dynamic_state_goto_url_with_code_before_extraction_is_allowed(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": ("https://example.com/search?q=budget+headphones&facet=color&facet=size&filter=available"),
            },
            {
                "block_type": "code",
                "label": "verify_state_from_dom",
                "code": (
                    "checked_labels = await page.locator('[aria-checked=\"true\"], input:checked').all_text_contents()\n"
                    "if not checked_labels:\n"
                    "    raise Exception('No active dynamic page state found')"
                ),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_dynamic_state_goto_url_without_downstream_extraction_is_allowed(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": "https://example.com/search?q=budget+headphones&facet=color&filter=available",
            }
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []

    def test_structural_state_value_directly_to_extraction_is_detected(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": (
                    "https://example.com/search?q=budget+headphones&"
                    "state=category%3Delectronics%3Bavailability%3Din_stock"
                ),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == [
            {
                "label": "open_dynamic_results",
                "dynamic_state_keys": ["state"],
                "next_data_block_label": "extract_results",
            }
        ]

    def test_single_embedded_assignment_directly_to_extraction_is_detected(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_dynamic_results",
                "url": "https://example.com/search?q=budget+headphones&f=shipping%3Dfree",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == [
            {
                "label": "open_dynamic_results",
                "dynamic_state_keys": ["f"],
                "next_data_block_label": "extract_results",
            }
        ]

    def test_url_like_query_values_are_not_treated_as_embedded_state_assignments(self) -> None:
        submitted = _yaml(
            {
                "block_type": "goto_url",
                "label": "open_redirect",
                "url": "https://example.com/start?redirect=https%3A%2F%2Fexample.org%2Fsearch%3Fx%3Dy",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_unverified_goto_url_state_shortcuts(submitted) == []


class TestGotoUrlStableSearchDripDetection:
    def test_navigation_url_with_stable_search_setup_is_detected(self) -> None:
        submitted = _yaml(
            {
                "block_type": "navigation",
                "label": "enter_query",
                "url": "https://example.com/",
                "navigation_goal": (
                    "```Dismiss any pop-up if it appears, then enter budget headphones in the product "
                    "search field without submitting.```"
                ),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_navigation_url_stable_search_setups(submitted) == [
            {"label": "enter_query", "url": "https://example.com/"}
        ]

    def test_navigation_url_without_stable_search_setup_is_allowed(self) -> None:
        submitted = _yaml(
            {
                "block_type": "navigation",
                "label": "open_article_and_accept_cookies",
                "url": "https://example.com/article",
                "navigation_goal": "```Accept the cookie banner if present.```",
            },
            {"block_type": "extraction", "label": "extract_article", "data_extraction_goal": "Extract article"},
        )

        assert _detect_navigation_url_stable_search_setups(submitted) == []

    def test_multiple_click_driven_stable_search_navigation_blocks_are_detected(self) -> None:
        submitted = _yaml(
            {"block_type": "goto_url", "label": "open_search_site", "url": "https://example.com/"},
            {
                "block_type": "navigation",
                "label": "enter_query",
                "navigation_goal": (
                    "Achieve the following mini goal and once it's achieved, complete: "
                    "```Enter budget headphones in the product search field. Do not submit the search yet.```"
                ),
            },
            {
                "block_type": "navigation",
                "label": "set_dates",
                "navigation_goal": (
                    "Achieve the following mini goal and once it's achieved, complete: "
                    "```Select July 10, 2026 as the start date and July 12, 2026 as the end date.```"
                ),
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_click_driven_stable_search_drips(submitted) == [
            {
                "entry_label": "open_search_site",
                "navigation_labels": ["enter_query", "set_dates"],
            }
        ]

    def test_stable_search_navigation_after_dom_inspection_is_allowed(self) -> None:
        submitted = _yaml(
            {"block_type": "goto_url", "label": "open_search_site", "url": "https://example.com/"},
            {
                "block_type": "navigation",
                "label": "enter_query",
                "navigation_goal": "```Enter budget headphones in the product search field.```",
            },
            {
                "block_type": "code",
                "label": "inspect_search_form",
                "code": "form_action = await page.locator('form').first.get_attribute('action')",
            },
            {
                "block_type": "navigation",
                "label": "set_dates",
                "navigation_goal": "```Select July 10, 2026 as the start date and July 12, 2026 as the end date.```",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_click_driven_stable_search_drips(submitted) == []

    def test_single_stable_search_navigation_before_submit_is_allowed(self) -> None:
        submitted = _yaml(
            {"block_type": "goto_url", "label": "open_search_site", "url": "https://example.com/"},
            {
                "block_type": "navigation",
                "label": "enter_query",
                "navigation_goal": "```Enter budget headphones in the product search field.```",
            },
            {
                "block_type": "navigation",
                "label": "submit_search",
                "navigation_goal": "```Click the Search button to submit and show results.```",
            },
            {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
        )

        assert _detect_click_driven_stable_search_drips(submitted) == []


def _ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.workflow_yaml = prior_yaml
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    return ctx


@pytest.mark.asyncio
async def test_update_workflow_rejects_unverified_goto_url_state_shortcut_and_emits_span() -> None:
    submitted = _yaml(
        {
            "block_type": "goto_url",
            "label": "open_dynamic_results",
            "url": "https://example.com/search?q=budget+headphones&facet=color&facet=size&filter=available",
        },
        {"block_type": "extraction", "label": "extract_results", "data_extraction_goal": "Extract results"},
    )
    ctx = _ctx()

    with (
        patch("skyvern.forge.sdk.copilot.tools._record_goto_url_state_shortcut_reject_span") as mock_span,
        patch("skyvern.forge.sdk.copilot.tools.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert "goto_url may bootstrap stable page state" in result["error"]
    assert "URL-encoded dynamic page state must be verified" in result["error"]
    assert "open_dynamic_results" in result["error"]
    assert "extract_results" in result["error"]
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_called()
    mock_span.assert_called_once()


class TestPromptAndToolDescriptions:
    def test_agent_prompt_contains_goto_url_state_policy(self) -> None:
        rendered = _render_agent_prompt()

        assert "GOTO_URL STATE SHORTCUT POLICY" in rendered
        assert "URL refinement for dynamic page state is allowed ONLY when grounded in live site evidence" in rendered
        assert "Before extraction on stateful search/result tasks" in rendered
        assert "do not spend one click-driven `navigation` block per field after the entry page loads" in rendered
        assert "current URL, form action, field names/values, result links, or URL deltas" in rendered
        assert "use `code` for DOM checks" in rendered
        assert "per-tool budget" in rendered
        assert "constraint / expected / observed / source" in rendered

    def test_tool_descriptions_warn_against_direct_state_shortcut_extraction(self) -> None:
        for tool in (update_workflow_tool, run_blocks_tool, update_and_run_blocks_tool):
            desc = tool.description  # type: ignore[attr-defined]
            assert "goto_url" in desc
            assert "dynamic page state" in desc
            assert "code" in desc
            assert "validation" in desc
