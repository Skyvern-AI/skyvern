"""
Tests for ai_click behavior when LLM returns empty actions.

This tests the fix for SKY-7577 where cached click actions were succeeding
even when the target element didn't exist on the page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi


@pytest.fixture
def mock_page():
    """Create a mock Playwright page."""
    page = MagicMock()
    page.url = "https://example.com"
    mock_locator = MagicMock()
    mock_locator.click = AsyncMock()
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
    mock.DATABASE = MagicMock()
    mock.DATABASE.get_step = AsyncMock(return_value=MagicMock())
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
            with pytest.raises(Exception) as exc_info:
                await real_skyvern_page_ai.ai_click(
                    selector=None,  # No fallback selector
                    intention="Click the download button",
                )

            # Should raise because no actions and no fallback
            assert "AI click failed" in str(exc_info.value) or "AI could not find" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ai_click_raises_when_llm_call_fails_no_selector(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        """
        When AI fails (exception) and there's no selector to fall back to,
        ai_click should raise an exception.
        """
        real_skyvern_page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)

        mock_app.SINGLE_CLICK_AGENT_LLM_API_HANDLER = AsyncMock(side_effect=Exception("LLM error"))

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
            with pytest.raises(Exception) as exc_info:
                await real_skyvern_page_ai.ai_click(
                    selector=None,  # No fallback selector
                    intention="Click the download button",
                )

            assert "AI click failed" in str(exc_info.value)

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
