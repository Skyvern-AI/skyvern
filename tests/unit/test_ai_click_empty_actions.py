"""
Tests for ai_* behavior when the LLM returns actions that parse to nothing.

Covers the fix for SKY-7577 (cached click actions succeeding even when the target
element didn't exist) and SKY-12329 (the select-option sibling, where an empty parse
skipped the select entirely but returned the requested value as if it had succeeded).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.config import settings
from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.exceptions import SkyvernActionFailed
from skyvern.webeye.actions.responses import ActionFailure


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
    async def test_ai_click_warns_when_unknown_exception_falls_back_to_selector(
        self,
        mock_page,
        mock_scraped_page,
        mock_context,
        mock_app,
    ):
        real_page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)
        llm_handler = AsyncMock(side_effect=RuntimeError("synthetic provider failure"))
        selector = "#fallback-target"
        sensitive_test_value = "SENSITIVE_TEST_VALUE"

        with (
            patch.object(real_page_ai, "_refresh_scraped_page", new_callable=AsyncMock),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.skyvern_context.ensure_context",
                return_value=mock_context,
            ),
            patch("skyvern.core.script_generations.real_skyvern_page_ai.app", mock_app),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai.prompt_engine.load_prompt",
                return_value="synthetic prompt",
            ),
            patch(
                "skyvern.core.script_generations.real_skyvern_page_ai._resolve_assist_llm_handler",
                new=AsyncMock(return_value=llm_handler),
            ),
            capture_logs() as logs,
        ):
            result = await real_page_ai.ai_click(
                selector=selector,
                intention="synthetic intention",
                data={"value": sensitive_test_value},
            )

        assert result == selector
        mock_page.locator.assert_called_once_with(selector)
        mock_page.locator.return_value.click.assert_awaited_once_with(timeout=settings.BROWSER_ACTION_TIMEOUT_MS)
        fallback_logs = [
            log for log in logs if log["event"] == "AI click failed unexpectedly; falling back to original selector"
        ]
        assert len(fallback_logs) == 1
        assert fallback_logs[0]["log_level"] == "warning"
        assert fallback_logs[0]["selector"] == selector
        assert sensitive_test_value not in str(logs)
        assert "synthetic intention" not in str(logs)
        assert not [log for log in logs if log["log_level"] == "error"]

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


class TestAiSelectOptionHandlerFailures:
    @pytest.mark.asyncio
    async def test_handler_failure_falls_back_to_raw_select(self, mock_page, mock_scraped_page, mock_context, mock_app):
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)
        action = MagicMock()
        action.option = MagicMock(value="adapted", label=None)

        with select_option_patches(page_ai, mock_context, mock_app, [action]) as handle_select:
            handle_select.return_value = [ActionFailure(RuntimeError("selection was not committed"))]
            result = await page_ai.ai_select_option(
                selector="#country",
                value="original",
                intention="select the synthetic option",
            )

        handle_select.assert_awaited_once()
        mock_page.locator.assert_called_once_with("#country")
        mock_page.locator.return_value.select_option.assert_awaited_once_with(
            "adapted", timeout=settings.BROWSER_ACTION_TIMEOUT_MS
        )
        assert result == "adapted"

    @pytest.mark.asyncio
    async def test_handler_failure_raises_without_selector(self, mock_page, mock_scraped_page, mock_context, mock_app):
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)
        action = MagicMock()
        action.option = MagicMock(value="adapted", label=None)

        with select_option_patches(page_ai, mock_context, mock_app, [action]) as handle_select:
            handle_select.return_value = [ActionFailure(RuntimeError("selection was not committed"))]
            with pytest.raises(SkyvernActionFailed, match="selection was not committed"):
                await page_ai.ai_select_option(
                    selector=None,
                    value="original",
                    intention="select the synthetic option",
                )

        handle_select.assert_awaited_once()
        mock_page.locator.assert_not_called()


class TestAiClickHandlerRefusal:
    """A click the handler refuses with skip_remaining_actions must not be retried as a raw selector click."""

    async def _ai_click(self, mock_page, mock_scraped_page, mock_context, mock_app, handler_result):
        from skyvern.webeye.actions.actions import ClickAction

        action = ClickAction(element_id="opt")
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)
        result = handler_result(action) if callable(handler_result) else handler_result
        with (
            patch.object(page_ai, "_refresh_scraped_page", new_callable=AsyncMock),
            patch(f"{MODULE}.skyvern_context.ensure_context", return_value=mock_context),
            patch(f"{MODULE}.app", mock_app),
            patch(f"{MODULE}.prompt_engine.load_prompt", return_value="mock_prompt"),
            patch(f"{MODULE}._resolve_assist_llm_handler", new_callable=AsyncMock) as resolve,
            patch(f"{MODULE}.parse_actions", return_value=[action]),
            patch(f"{MODULE}.ActionHandler.get_setup_for_action_type", return_value=None),
            patch(f"{MODULE}.handle_click_action", new=AsyncMock(return_value=[result])),
        ):
            resolve.return_value = AsyncMock(return_value={"actions": [{"action_type": "CLICK"}]})
            return await page_ai.ai_click(selector="xpath=//div[@id='opt']", intention="Pick the answer")

    @pytest.mark.asyncio
    async def test_refusal_with_skip_remaining_actions_is_not_retried_with_selector(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        from skyvern.webeye.actions import handler as click_handler

        def refusal(action):
            return click_handler._static_listbox_replan_failure(action, "This option is already selected.")

        with pytest.raises(SkyvernActionFailed):
            await self._ai_click(mock_page, mock_scraped_page, mock_context, mock_app, refusal)
        mock_page.locator.return_value.click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_recoverable_click_failure_still_falls_back_to_selector(
        self, mock_page, mock_scraped_page, mock_context, mock_app
    ):
        failure = ActionFailure(Exception("element moved"))
        assert not failure.skip_remaining_actions and not failure.setup_performed
        result = await self._ai_click(mock_page, mock_scraped_page, mock_context, mock_app, failure)
        assert result == "xpath=//div[@id='opt']"
        mock_page.locator.return_value.click.assert_awaited_once()


class TestAiFallbackAfterBrowserLoss:
    """A cached run that lost its browser starts no AI work and retries nothing with the raw selector."""

    _CALLS = {
        "click": (lambda ai: ai.ai_click(selector="#go", intention="Go"), "#go"),
        "input_text": (lambda ai: ai.ai_input_text("#name", "Noor", "Name", failed_selector="#name"), "Noor"),
        "upload_file": (
            lambda ai: ai.ai_upload_file("#cv", "https://example.com/cv.pdf", "CV"),
            "https://example.com/cv.pdf",
        ),
        "select_option": (lambda ai: ai.ai_select_option("#color", "Blue", "Pick blue"), "Blue"),
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lost", ["before_the_call", "during_the_llm_call", "never"])
    @pytest.mark.parametrize("method", sorted(_CALLS))
    async def test_ai_fallback_dispatches_nothing_after_native_browser_loss(
        self, mock_page, mock_scraped_page, mock_context, mock_app, method: str, lost: str
    ):
        from skyvern.exceptions import MissingBrowserState
        from tests.unit._task_block_fakes import run_bound_browser_state

        browser_state, browser_context = run_bound_browser_state("wr_ai")
        mock_scraped_page._browser_state = browser_state
        mock_context.workflow_run_id = "wr_ai"
        mock_context.script_run_parameters = None
        page_ai = RealSkyvernPageAi(mock_scraped_page, mock_page)
        call, expected = self._CALLS[method]

        async def llm(**_: object) -> dict:
            if lost == "during_the_llm_call":
                browser_state._on_browser_context_closed(browser_context)
            return {"actions": []}

        raw_input = AsyncMock()
        v3_hook = AsyncMock(return_value=(None, False))
        if lost == "before_the_call":
            browser_state._on_browser_context_closed(browser_context)
        with (
            patch.object(page_ai, "_maybe_run_v3_midrun", v3_hook),
            patch(f"{MODULE}.skyvern_context.ensure_context", return_value=mock_context),
            patch(f"{MODULE}.skyvern_context.current", return_value=mock_context),
            patch(f"{MODULE}.app", mock_app),
            patch(f"{MODULE}.prompt_engine.load_prompt", return_value="mock_prompt"),
            patch(f"{MODULE}._resolve_assist_llm_handler", AsyncMock(return_value=AsyncMock(side_effect=llm))),
            patch(f"{MODULE}.handler_utils.input_sequentially", raw_input),
            pytest.raises(MissingBrowserState) if lost != "never" else nullcontext(),
        ):
            assert await call(page_ai) == expected

        raw_locator = mock_page.locator.return_value
        raw_dispatches = raw_locator.click.await_count + raw_locator.select_option.await_count + raw_input.await_count
        assert raw_dispatches == (1 if lost == "never" and method != "upload_file" else 0)
        assert mock_scraped_page.generate_scraped_page.await_count == (0 if lost == "before_the_call" else 1)
        if method == "input_text":
            assert v3_hook.await_count == (0 if lost == "before_the_call" else 1)
