"""
Tests for ai_* behavior when the LLM returns actions that parse to nothing.

Covers the fix for SKY-7577 (cached click actions succeeding even when the target
element didn't exist) and SKY-12329 (the select-option sibling, where an empty parse
skipped the select entirely but returned the requested value as if it had succeeded).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.exceptions import SkyvernActionFailed


@pytest.fixture
def mock_page():
    """Create a mock Playwright page."""
    page = MagicMock()
    page.url = "https://example.com"
    mock_locator = MagicMock()
    mock_locator.click = AsyncMock()
    mock_locator.select_option = AsyncMock()
    page.locator = MagicMock(return_value=mock_locator)
    return page


@pytest.fixture
def mock_scraped_page():
    """Create a mock ScrapedPage that properly supports async methods."""
    scraped_page = MagicMock()
    scraped_page.build_element_tree = MagicMock(return_value="<element_tree>")
    # The generate_scraped_page method is async and returns self
    scraped_page.generate_scraped_page = AsyncMock(return_value=scraped_page)
    return scraped_page


@pytest.fixture
def mock_context():
    """Create a mock skyvern context."""
    context = MagicMock()
    context.organization_id = "org_123"
    context.task_id = "task_123"
    context.step_id = "step_123"
    context.prompt = "Test prompt"
    context.tz_info = None
    return context


@pytest.fixture
def mock_app():
    """Create a mock app with SINGLE_CLICK_AGENT_LLM_API_HANDLER."""
    mock = MagicMock()
    mock.SINGLE_CLICK_AGENT_LLM_API_HANDLER = AsyncMock(return_value={"actions": []})
    mock.SELECT_AGENT_LLM_API_HANDLER = AsyncMock(return_value={"actions": []})
    mock.DATABASE = MagicMock()
    mock.DATABASE.tasks.get_step = AsyncMock(return_value=MagicMock())
    mock.DATABASE.tasks.get_task = AsyncMock(return_value=MagicMock())
    return mock


class TestAiClickEmptyActions:
    """Test that ai_click properly fails when LLM returns no actions."""

    @pytest.mark.asyncio
    async def test_ai_click_raises_when_llm_returns_empty_actions_no_selector(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """
        When the LLM returns no actions (element doesn't exist on page) and
        there's no selector to fall back to, ai_click should raise an exception.
        """
        real_skyvern_page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        mock_app.SINGLE_CLICK_AGENT_LLM_API_HANDLER = AsyncMock(return_value={"actions": []})

        with (
            patch.object(real_skyvern_page_ai, "_refresh_scraped_page", new_callable=AsyncMock),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.skyvern_context.ensure_context",
                return_value=mock_context,
            ),
            patch("skyvern.core.script_generations.real_skyvern_page_ai.app", mock_app),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.prompt_engine.load_prompt",
                return_value="mock_prompt",
            ),
        ):
            with pytest.raises(SkyvernActionFailed) as exc_info:
                await real_skyvern_page_ai.ai_click(
                    selector=None,
                    intention="Click the download button",
                )

            assert "AI click failed" in str(exc_info.value) or "AI could not find" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ai_click_propagates_unknown_exception_when_no_selector(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """
        When the AI path raises a non-operational exception (LLM down, etc.) and
        there's no selector to fall back to, the original exception should
        propagate so the caller surfaces it as a 500, not a 422.
        """
        real_skyvern_page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        mock_app.SINGLE_CLICK_AGENT_LLM_API_HANDLER = AsyncMock(side_effect=RuntimeError("LLM provider down"))

        with (
            patch.object(real_skyvern_page_ai, "_refresh_scraped_page", new_callable=AsyncMock),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.skyvern_context.ensure_context",
                return_value=mock_context,
            ),
            patch("skyvern.core.script_generations.real_skyvern_page_ai.app", mock_app),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.prompt_engine.load_prompt",
                return_value="mock_prompt",
            ),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await real_skyvern_page_ai.ai_click(
                    selector=None,
                    intention="Click the download button",
                )

            assert "LLM provider down" in str(exc_info.value)
            assert not isinstance(exc_info.value, SkyvernActionFailed)

    @pytest.mark.asyncio
    async def test_ai_click_falls_back_to_selector_when_llm_returns_empty(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """
        When AI returns empty actions but there IS a selector to fall back to,
        ai_click should use the selector and succeed.
        """
        # Set up the locator mock properly with AsyncMock for click
        mock_locator = MagicMock()
        mock_locator.click = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        real_skyvern_page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        mock_app.SINGLE_CLICK_AGENT_LLM_API_HANDLER = AsyncMock(return_value={"actions": []})

        with (
            patch.object(real_skyvern_page_ai, "_refresh_scraped_page", new_callable=AsyncMock),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.skyvern_context.ensure_context",
                return_value=mock_context,
            ),
            patch("skyvern.core.script_generations.real_skyvern_page_ai.app", mock_app),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.prompt_engine.load_prompt",
                return_value="mock_prompt",
            ),
        ):
            # Should NOT raise because we have a fallback selector
            result = await real_skyvern_page_ai.ai_click(
                selector="xpath=//button[@id='download']",  # Has fallback
                intention="Click the download button",
            )

            # Should have used the fallback selector
            mock_page.locator.assert_called_once_with("xpath=//button[@id='download']")
            assert result == "xpath=//button[@id='download']"


MODULE = "skyvern.core.script_generations.real_skyvern_page_ai"


@contextmanager
def select_option_patches(page_ai, mock_context, mock_app, parsed_actions):
    """Patch ai_select_option's collaborators, with parse_actions returning parsed_actions."""
    with (
        patch.object(page_ai, "_refresh_scraped_page", new_callable=AsyncMock),
        patch(f"{MODULE}.skyvern_context.current", return_value=mock_context),
        patch(f"{MODULE}.app", mock_app),
        patch(f"{MODULE}.prompt_engine.load_prompt", return_value="mock_prompt"),
        patch(f"{MODULE}._resolve_assist_llm_handler", new_callable=AsyncMock) as resolve,
        patch(f"{MODULE}.parse_actions", return_value=parsed_actions),
        patch(f"{MODULE}.handle_select_option_action", new_callable=AsyncMock) as handle,
    ):
        resolve.return_value = AsyncMock(return_value={"actions": [{"action_type": "SELECT_OPTION"}]})
        yield handle


class TestAiSelectOptionEmptyActions:
    """SKY-12329: an empty parse must never report success without selecting anything."""

    @pytest.mark.asyncio
    async def test_falls_back_to_raw_select_when_actions_do_not_parse(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """
        Empty parse with a usable selector: the option must actually be selected via the
        raw locator. Previously this branch only logged and returned `value`, so the
        dropdown kept its old value while the caller was told the select succeeded.
        """
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        with select_option_patches(page_ai, mock_context, mock_app, []) as handle_select:
            result = await page_ai.ai_select_option(
                selector="xpath=//select[@id='state']",
                value="California",
                intention="Select the state",
            )

        handle_select.assert_not_called()
        mock_page.locator.assert_called_once_with("xpath=//select[@id='state']")
        mock_page.locator.return_value.select_option.assert_awaited_once()
        assert mock_page.locator.return_value.select_option.await_args.args[0] == "California"
        assert result == "California"

    @pytest.mark.asyncio
    async def test_raises_when_actions_do_not_parse_and_no_selector(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """Empty parse with no selector to fall back to: fail loudly, never return `value`."""
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        with select_option_patches(page_ai, mock_context, mock_app, []):
            with pytest.raises(SkyvernActionFailed):
                await page_ai.ai_select_option(
                    selector=None,
                    value="California",
                    intention="Select the state",
                )

        mock_page.locator.return_value.select_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_when_actions_do_not_parse_without_option_value(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """An empty AI parse must not select a dropdown's empty placeholder."""
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        with select_option_patches(page_ai, mock_context, mock_app, []):
            with pytest.raises(SkyvernActionFailed):
                await page_ai.ai_select_option(
                    selector="xpath=//select[@id='state']",
                    value="",
                    intention="Select the state",
                )

        mock_page.locator.return_value.select_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_without_task_context_when_no_selector(self, mock_page, mock_scraped_page):
        """No task context and no selector: nothing can be selected, so fail instead of
        returning `value` as though it had been."""
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        with patch(f"{MODULE}.skyvern_context.current", return_value=None):
            with pytest.raises(SkyvernActionFailed):
                await page_ai.ai_select_option(
                    selector=None,
                    value="California",
                    intention="Select the state",
                )

        mock_page.locator.return_value.select_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_ai_select_does_not_also_raw_select(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """When the AI action is applied, the raw locator fallback must not fire as well."""
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)
        action = MagicMock()
        action.option = MagicMock(value="CA", label="California")

        with select_option_patches(page_ai, mock_context, mock_app, [action]) as handle_select:
            result = await page_ai.ai_select_option(
                selector="xpath=//select[@id='state']",
                value="California",
                intention="Select the state",
            )

        handle_select.assert_awaited_once()
        mock_page.locator.return_value.select_option.assert_not_awaited()
        assert result == "CA"
