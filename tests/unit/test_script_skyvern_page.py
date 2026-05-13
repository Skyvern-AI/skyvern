"""
Unit tests for ScriptSkyvernPage.

Tests _wait_for_page_ready_before_action (regression test for self._page bug, PR #8425),
_ensure_element_ids_on_page (injects unique_id attrs after page navigation),
terminate() (raises ScriptTerminationException for Code 2.0 cached execution),
and wait() (accepts both seconds= and timeout_ms= parameter styles).
"""

import inspect
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.exceptions import IllegitCompleteScriptTermination, ScriptTerminationException


def create_mock_page():
    """Create a mock Playwright Page object with required attributes."""
    page = MagicMock()
    page.url = "https://example.com"
    # Required for Playwright Page base class
    page._loop = MagicMock()
    page._impl_obj = page
    return page


@pytest.fixture
def mock_scraped_page():
    """Create a mock ScrapedPage object."""
    scraped_page = MagicMock()
    scraped_page._browser_state = MagicMock()
    return scraped_page


@pytest.fixture
def mock_ai():
    """Create a mock SkyvernPageAi object."""
    return MagicMock()


@pytest.mark.asyncio
async def test_wait_for_page_ready_before_action_calls_skyvern_frame(mock_scraped_page, mock_ai):
    """
    Test that _wait_for_page_ready_before_action correctly calls SkyvernFrame.

    This is a regression test for the bug in PR #8273 where self._page was used
    instead of self.page, causing AttributeError because SkyvernPage stores the
    Playwright page in self.page.
    """
    mock_page = create_mock_page()

    # Patch the Page base class to avoid Playwright internals
    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        # Create ScriptSkyvernPage instance
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        # Mock SkyvernFrame to verify it's called with self.page
        mock_skyvern_frame = MagicMock()
        mock_skyvern_frame.wait_for_page_ready = AsyncMock()

        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
            return_value=mock_skyvern_frame,
        ) as mock_create_instance:
            await script_page._wait_for_page_ready_before_action()

            # Verify SkyvernFrame.create_instance was called exactly once
            mock_create_instance.assert_called_once()

            # Get the actual call argument
            call_kwargs = mock_create_instance.call_args.kwargs
            assert "frame" in call_kwargs, "create_instance should be called with frame argument"
            # The frame argument should be a MagicMock (the page object)
            assert call_kwargs["frame"] is not None, "frame should not be None"

            # Verify wait_for_page_ready was called with correct settings
            mock_skyvern_frame.wait_for_page_ready.assert_called_once_with(
                network_idle_timeout_ms=settings.PAGE_READY_NETWORK_IDLE_TIMEOUT_MS,
                loading_indicator_timeout_ms=settings.PAGE_READY_LOADING_INDICATOR_TIMEOUT_MS,
                dom_stable_ms=settings.PAGE_READY_DOM_STABLE_MS,
                dom_stability_timeout_ms=settings.PAGE_READY_DOM_STABILITY_TIMEOUT_MS,
            )


@pytest.mark.asyncio
async def test_wait_for_page_ready_before_action_handles_no_page(mock_scraped_page, mock_ai):
    """
    Test that _wait_for_page_ready_before_action returns early if self.page is None.
    """
    # Patch the Page base class to avoid Playwright internals
    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        # Create a mock page first, then set page to None after construction
        mock_page = create_mock_page()
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )
        # Simulate page being None (e.g., after page was closed)
        script_page.page = None

        # This should return early without raising an error
        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
        ) as mock_create_instance:
            await script_page._wait_for_page_ready_before_action()

            # SkyvernFrame.create_instance should NOT be called
            mock_create_instance.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_page_ready_before_action_catches_exceptions(mock_scraped_page, mock_ai):
    """
    Test that exceptions in _wait_for_page_ready_before_action are caught
    and don't block action execution.

    This verifies the defensive behavior - page readiness check failures
    should not prevent actions from executing.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        # Make SkyvernFrame.create_instance raise an exception
        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
            side_effect=Exception("Simulated page readiness error"),
        ):
            # Should NOT raise - exception should be caught
            await script_page._wait_for_page_ready_before_action()


@pytest.mark.asyncio
async def test_wait_for_page_ready_before_action_catches_wait_for_page_ready_exceptions(mock_scraped_page, mock_ai):
    """
    Test that exceptions from wait_for_page_ready are caught and logged.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        # Make wait_for_page_ready raise an exception
        mock_skyvern_frame = MagicMock()
        mock_skyvern_frame.wait_for_page_ready = AsyncMock(side_effect=TimeoutError("Page never became idle"))

        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
            return_value=mock_skyvern_frame,
        ):
            # Should NOT raise - exception should be caught
            await script_page._wait_for_page_ready_before_action()


@pytest.mark.asyncio
async def test_wait_for_page_ready_attribute_access_regression():
    """
    Regression test: Verify that the code accesses self.page, not self._page.

    The original bug (fixed in PR #8425) used self._page which caused:
    AttributeError: 'ScriptSkyvernPage' object has no attribute '_page'

    This test directly inspects the source code to ensure self._page is not used.
    """
    source = inspect.getsource(ScriptSkyvernPage._wait_for_page_ready_before_action)

    # The fixed code should use self.page
    # nosemgrep false positive: "self.page" is an attribute name, not a URL.
    assert "self.page" in source, "Method should access self.page"  # nosemgrep: incomplete-url-substring-sanitization

    # The fixed code should NOT use self._page (except in comments)
    # Remove comments and docstrings first
    # Remove docstrings
    source_no_docstrings = re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
    source_no_docstrings = re.sub(r"'''.*?'''", "", source_no_docstrings, flags=re.DOTALL)
    # Remove single-line comments
    source_no_comments = re.sub(r"#.*$", "", source_no_docstrings, flags=re.MULTILINE)

    # Now check - self._page should NOT appear in the actual code
    # (It may appear in comments explaining the fix, which is fine)
    lines_with_code = [
        line for line in source_no_comments.split("\n") if line.strip() and not line.strip().startswith("#")
    ]
    code_only = "\n".join(lines_with_code)

    # Check for the bug pattern
    if "self._page" in code_only:
        # Find the line for better error reporting
        for i, line in enumerate(source.split("\n"), 1):
            if "self._page" in line and not line.strip().startswith("#"):
                pytest.fail(
                    f"Found 'self._page' in code at line {i}: {line.strip()}\n"
                    "This is a regression! SkyvernPage uses self.page, not self._page."
                )


# =============================================================================
# Tests for _ensure_element_ids_on_page
# =============================================================================


@pytest.mark.asyncio
async def test_ensure_element_ids_skips_when_ids_exist(mock_scraped_page, mock_ai):
    """
    When unique_id attributes already exist on the page, build_tree_from_body
    should NOT be called (fast path).
    """
    mock_page = create_mock_page()
    # SkyvernPage.__getattribute__ delegates self.page to mock_page.page
    mock_page.page.evaluate = AsyncMock(return_value=True)  # unique_ids exist

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
        ) as mock_create_instance:
            await script_page._ensure_element_ids_on_page()

            # Should NOT inject domUtils.js since IDs already exist
            mock_create_instance.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_element_ids_injects_when_ids_missing(mock_scraped_page, mock_ai):
    """
    When no unique_id attributes exist (after page navigation), should inject
    domUtils.js and call buildTreeFromBody to set them.
    """
    mock_page = create_mock_page()
    # SkyvernPage.__getattribute__ delegates self.page to mock_page.page,
    # so set evaluate on the delegated object
    mock_page.page.evaluate = AsyncMock(return_value=False)  # no unique_ids

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        mock_skyvern_frame = MagicMock()
        mock_skyvern_frame.build_tree_from_body = AsyncMock(return_value=([], []))

        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
            return_value=mock_skyvern_frame,
        ) as mock_create_instance:
            await script_page._ensure_element_ids_on_page()

            # Should inject domUtils.js
            mock_create_instance.assert_called_once()

            # Should build element tree
            mock_skyvern_frame.build_tree_from_body.assert_called_once_with(
                frame_name="main.frame",
                frame_index=0,
                timeout_ms=15000,
            )


@pytest.mark.asyncio
async def test_ensure_element_ids_handles_no_page(mock_scraped_page, mock_ai):
    """
    When self.page is None, should return early without error.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )
        script_page.page = None

        with patch(
            "skyvern.core.script_generations.script_skyvern_page.SkyvernFrame.create_instance",
            new_callable=AsyncMock,
        ) as mock_create_instance:
            await script_page._ensure_element_ids_on_page()
            mock_create_instance.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_element_ids_catches_exceptions(mock_scraped_page, mock_ai):
    """
    Exceptions in _ensure_element_ids_on_page should be caught and not block
    action execution.
    """
    mock_page = create_mock_page()
    # SkyvernPage.__getattribute__ delegates self.page to mock_page.page
    mock_page.page.evaluate = AsyncMock(side_effect=Exception("Page crashed"))

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        # Should NOT raise
        await script_page._ensure_element_ids_on_page()


# =============================================================================
# Tests for terminate()
# =============================================================================


@pytest.mark.asyncio
async def test_terminate_raises_script_termination_exception_without_context(mock_scraped_page, mock_ai):
    """
    When there is no SkyvernContext, terminate() should raise ScriptTerminationException
    with the error messages from the errors list.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        with patch(
            "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
            return_value=None,
        ):
            with pytest.raises(ScriptTerminationException, match="Terminate called: page not found"):
                await script_page.terminate(errors=["page not found"])


@pytest.mark.asyncio
async def test_terminate_calls_handler_and_raises(mock_scraped_page, mock_ai):
    """
    When context, task, and step are available, terminate() should call
    handle_terminate_action and then raise ScriptTerminationException.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        mock_context = MagicMock()
        mock_context.organization_id = "org_123"
        mock_context.workflow_run_id = "wr_456"
        mock_context.task_id = "tsk_789"
        mock_context.step_id = "stp_012"
        mock_context.action_order = 0

        mock_task = MagicMock()
        mock_step = MagicMock()
        mock_step.order = 0

        with (
            patch(
                "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
                return_value=mock_context,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_task",
                new_callable=AsyncMock,
                return_value=mock_task,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_step",
                new_callable=AsyncMock,
                return_value=mock_step,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.handle_terminate_action",
                new_callable=AsyncMock,
                return_value=[MagicMock(success=True)],
            ) as mock_handler,
        ):
            with pytest.raises(ScriptTerminationException, match="Terminate called: error1; error2"):
                await script_page.terminate(errors=["error1", "error2"])

            # Verify handler was called with correct arguments
            mock_handler.assert_called_once()
            call_args = mock_handler.call_args
            action = call_args[0][0]
            assert action.organization_id == "org_123"
            assert action.workflow_run_id == "wr_456"
            assert action.task_id == "tsk_789"
            assert action.step_id == "stp_012"
            # Verify reasoning is set from errors for LLM extraction context
            assert action.reasoning == "error1; error2"


@pytest.mark.asyncio
async def test_terminate_raises_even_when_task_not_found(mock_scraped_page, mock_ai):
    """
    When context exists but task/step are not found in the database,
    terminate() should still raise ScriptTerminationException.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        mock_context = MagicMock()
        mock_context.organization_id = "org_123"
        mock_context.workflow_run_id = "wr_456"
        mock_context.task_id = "tsk_789"
        mock_context.step_id = "stp_012"

        with (
            patch(
                "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
                return_value=mock_context,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_task",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_step",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.handle_terminate_action",
                new_callable=AsyncMock,
            ) as mock_handler,
        ):
            with pytest.raises(ScriptTerminationException, match="Terminate called: task failed"):
                await script_page.terminate(errors=["task failed"])

            # Handler should NOT be called when task/step not found
            mock_handler.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_raises_even_when_handler_fails(mock_scraped_page, mock_ai):
    """
    When handle_terminate_action raises an exception (e.g., LLM call fails during
    extract_user_defined_errors), terminate() should still raise ScriptTerminationException
    so upstream workflow/service.py correctly marks the block as terminated.
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        mock_context = MagicMock()
        mock_context.organization_id = "org_123"
        mock_context.workflow_run_id = "wr_456"
        mock_context.task_id = "tsk_789"
        mock_context.step_id = "stp_012"
        mock_context.action_order = 0

        mock_task = MagicMock()
        mock_step = MagicMock()
        mock_step.order = 0

        with (
            patch(
                "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
                return_value=mock_context,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_task",
                new_callable=AsyncMock,
                return_value=mock_task,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_step",
                new_callable=AsyncMock,
                return_value=mock_step,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.handle_terminate_action",
                new_callable=AsyncMock,
                side_effect=Exception("LLM call failed"),
            ) as mock_handler,
        ):
            # Should raise ScriptTerminationException, NOT the handler's Exception
            with pytest.raises(ScriptTerminationException, match="Terminate called: handler error"):
                await script_page.terminate(errors=["handler error"])

            mock_handler.assert_called_once()


@pytest.mark.asyncio
async def test_complete_raises_illegit_complete_subclass_when_handler_rejects(mock_scraped_page, mock_ai):
    """
    When handle_complete_action returns an ActionFailure (verifier rejected the
    completion), complete() must raise IllegitCompleteScriptTermination — the
    subclass — not the parent ScriptTerminationException. The script_service
    catch sites distinguish the two: subclass → BlockStatus.failed (fallback
    fires), parent → BlockStatus.terminated (no fallback).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        mock_context = MagicMock()
        mock_context.organization_id = "org_123"
        mock_context.workflow_run_id = "wr_456"
        mock_context.task_id = "tsk_789"
        mock_context.step_id = "stp_012"
        mock_context.action_order = 0
        mock_context.skip_complete_verification = False
        mock_context.code_version = 1
        mock_context.is_static_script = False
        mock_context.script_mode = False

        mock_task = MagicMock()
        mock_step = MagicMock()
        mock_step.order = 0

        rejected_result = MagicMock(success=False)
        rejected_result.exception_message = (
            "Illegit complete, data={'error': 'Goal not achieved — page still on landing'}"
        )

        with (
            patch(
                "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
                return_value=mock_context,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_task",
                new_callable=AsyncMock,
                return_value=mock_task,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.tasks.get_step",
                new_callable=AsyncMock,
                return_value=mock_step,
            ),
            patch.object(
                ScriptSkyvernPage,
                "_update_step_output_before_complete",
                new_callable=AsyncMock,
            ),
            patch.object(
                ScriptSkyvernPage,
                "_create_final_screenshot",
                new_callable=AsyncMock,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.handle_complete_action",
                new_callable=AsyncMock,
                return_value=[rejected_result],
            ),
        ):
            # The subclass is what the catch sites in script_service.py key off of
            # to record BlockStatus.failed (so AI fallback fires). If complete()
            # regresses to raising plain ScriptTerminationException, the parent
            # arm catches it and the block is recorded as terminated — no
            # fallback fires for what is genuinely a failure.
            with pytest.raises(IllegitCompleteScriptTermination, match="Illegit complete"):
                await script_page.complete()


# =============================================================================
# Tests for fill() proactive upgrade when value=None + prompt
# =============================================================================


@pytest.mark.asyncio
async def test_fill_value_none_with_prompt_upgrades_to_proactive(mock_scraped_page, mock_ai):
    """
    When fill() is called with value=None and a prompt but ai != 'proactive',
    it should upgrade ai to 'proactive' and delegate to _input_text instead of
    returning "" (the old silent no-op behavior).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        # Mock _input_text to capture the call
        script_page._input_text = AsyncMock(return_value="filled_value")

        result = await script_page.fill(
            selector="#email",
            value=None,
            prompt="Fill the email address field",
            ai="fallback",
        )

        # Should NOT return "" — should delegate to _input_text
        assert result == "filled_value"
        script_page._input_text.assert_called_once()
        call_kwargs = script_page._input_text.call_args.kwargs
        assert call_kwargs["ai"] == "proactive"


@pytest.mark.asyncio
async def test_fill_value_none_no_prompt_still_skips(mock_scraped_page, mock_ai):
    """
    When fill() is called with value=None and NO prompt and ai != 'proactive',
    it should still return "" (skip the fill).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        script_page._input_text = AsyncMock(return_value="should_not_reach")

        result = await script_page.fill(
            selector="#email",
            value=None,
            ai="fallback",
        )

        assert result == ""
        script_page._input_text.assert_not_called()


@pytest.mark.asyncio
async def test_fill_value_none_proactive_unchanged(mock_scraped_page, mock_ai):
    """
    When fill() is called with value=None and ai='proactive', it should
    proceed as before (not return early, delegate to _input_text).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        script_page._input_text = AsyncMock(return_value="ai_value")

        result = await script_page.fill(
            selector="#email",
            value=None,
            prompt="Fill the email",
            ai="proactive",
        )

        assert result == "ai_value"
        script_page._input_text.assert_called_once()


# =============================================================================
# Tests for fill_autocomplete() proactive upgrade when value=None + prompt
# =============================================================================


@pytest.mark.asyncio
async def test_fill_autocomplete_value_none_with_prompt_upgrades_to_proactive(mock_scraped_page, mock_ai):
    """
    When fill_autocomplete() is called with value=None and a prompt but ai != 'proactive',
    it should upgrade ai to 'proactive' and delegate to ai_input_text instead of
    returning "" (the old silent no-op behavior).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        script_page._ai.ai_input_text = AsyncMock(return_value="filled_value")

        result = await script_page.fill_autocomplete(
            selector="#city",
            value=None,
            prompt="Fill the city field",
            ai="fallback",
        )

        assert result == "filled_value"
        script_page._ai.ai_input_text.assert_called_once()


@pytest.mark.asyncio
async def test_fill_autocomplete_value_none_no_prompt_still_skips(mock_scraped_page, mock_ai):
    """
    When fill_autocomplete() is called with value=None and NO prompt and ai != 'proactive',
    it should still return "" (skip the fill).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        script_page._ai.ai_input_text = AsyncMock(return_value="should_not_reach")

        result = await script_page.fill_autocomplete(
            selector="#city",
            value=None,
            ai="fallback",
        )

        assert result == ""
        script_page._ai.ai_input_text.assert_not_called()


@pytest.mark.asyncio
async def test_fill_autocomplete_value_none_proactive_unchanged(mock_scraped_page, mock_ai):
    """
    When fill_autocomplete() is called with value=None and ai='proactive', it should
    proceed as before (delegate to ai_input_text).
    """
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        script_page._ai.ai_input_text = AsyncMock(return_value="ai_value")

        result = await script_page.fill_autocomplete(
            selector="#city",
            value=None,
            prompt="Fill the city",
            ai="proactive",
        )

        assert result == "ai_value"
        script_page._ai.ai_input_text.assert_called_once()


# =============================================================================
# Tests for wait() — timeout_ms support
# =============================================================================


def _get_wait_inner_fn():
    """Extract the inner wait function from the action_wrap closure.

    action_wrap replaces the method with a wrapper. The original function
    is stored in the closure as the 'fn' free variable.
    """
    from skyvern.core.script_generations.skyvern_page import SkyvernPage

    wrapper = SkyvernPage.wait
    # closure vars are ('action', 'fn') per action_wrap implementation
    for var_name, cell in zip(wrapper.__code__.co_freevars, wrapper.__closure__):
        if var_name == "fn":
            return cell.cell_contents
    raise RuntimeError("Could not extract inner wait function from action_wrap closure")


class TestWaitMethod:
    """Tests for SkyvernPage.wait() accepting both seconds= and timeout_ms=.

    The script reviewer prompt documents the API as page.wait(timeout_ms=5000),
    but the original implementation only accepted wait(seconds=5). This mismatch
    caused every LLM-generated wait call to raise TypeError at runtime, silently
    triggering agent fallback instead of actually waiting.
    """

    @pytest.mark.asyncio
    async def test_wait_with_seconds_kwarg(self):
        """wait(seconds=0.05) should sleep for ~0.05 seconds."""
        fn = _get_wait_inner_fn()
        t0 = time.monotonic()
        await fn(None, seconds=0.05)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04, f"Expected ~0.05s sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_wait_with_seconds_positional(self):
        """wait(0.05) should sleep for ~0.05 seconds (positional arg)."""
        fn = _get_wait_inner_fn()
        t0 = time.monotonic()
        await fn(None, 0.05)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04, f"Expected ~0.05s sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_wait_with_timeout_ms_kwarg(self):
        """wait(timeout_ms=50) should sleep for ~0.05 seconds."""
        fn = _get_wait_inner_fn()
        t0 = time.monotonic()
        await fn(None, timeout_ms=50)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04, f"Expected ~0.05s sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_wait_with_timeout_ms_converts_correctly(self):
        """wait(timeout_ms=100) should sleep ~0.1s, not 100 seconds."""
        fn = _get_wait_inner_fn()
        t0 = time.monotonic()
        await fn(None, timeout_ms=100)
        elapsed = time.monotonic() - t0
        assert 0.08 <= elapsed <= 0.5, f"Expected ~0.1s, got {elapsed:.3f}s — ms→s conversion may be wrong"

    @pytest.mark.asyncio
    async def test_wait_seconds_takes_precedence_over_timeout_ms(self):
        """When both seconds= and timeout_ms= are provided, seconds= wins."""
        fn = _get_wait_inner_fn()
        t0 = time.monotonic()
        await fn(None, seconds=0.05, timeout_ms=10000)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"seconds= should take precedence, but waited {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_wait_no_args_returns_immediately(self):
        """wait() with no args should return immediately (sleep 0)."""
        fn = _get_wait_inner_fn()
        t0 = time.monotonic()
        await fn(None)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"Expected immediate return, got {elapsed:.3f}s"

    def test_wait_signature_allows_timeout_ms(self):
        """The inner wait function must accept timeout_ms via **kwargs without TypeError.

        This is the core regression test: the old signature was wait(self, seconds: float, **kwargs)
        where seconds was required. Calling wait(timeout_ms=5000) raised TypeError because
        seconds had no default. The fix makes seconds optional (default None).
        """
        fn = _get_wait_inner_fn()
        sig = inspect.signature(fn)
        seconds_param = sig.parameters.get("seconds")
        assert seconds_param is not None, "wait() should have a 'seconds' parameter"
        assert seconds_param.default is None, (
            f"seconds should default to None so timeout_ms can be used instead, got default={seconds_param.default}"
        )


class TestActionSubclassPersistence:
    """Regression for SKY-9513: cached script execution must persist the correct
    Action subclass with subclass-specific fields populated.

    Before the fix, `_create_action_and_result_after_execution` constructed a
    base `Action(...)` for every action type, silently dropping fields like
    MoveAction.x/y and ScrollAction.scroll_x/scroll_y. After the fix, the
    function dispatches via `ACTION_TYPE_TO_CLASS` and pulls subclass fields
    from kwargs.
    """

    @staticmethod
    def _build_script_page(mock_scraped_page, mock_ai):
        from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage

        with patch(
            "skyvern.core.script_generations.skyvern_page.Page.__init__",
            return_value=None,
        ):
            return ScriptSkyvernPage(
                scraped_page=mock_scraped_page,
                page=create_mock_page(),
                ai=mock_ai,
            )

    @staticmethod
    def _build_context():
        ctx = MagicMock()
        ctx.organization_id = "o_test"
        ctx.workflow_run_id = "wr_test"
        ctx.task_id = "tsk_test"
        ctx.step_id = "stp_test"
        ctx.action_order = 0
        ctx.script_mode = True
        ctx.sensitive_values = set()
        return ctx

    @staticmethod
    async def _invoke(script_page, action_type, kwargs, ctx, captured):
        async def fake_create_action(action):
            captured.append(action)
            action.action_id = "act_test"
            return action

        with (
            patch(
                "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
                return_value=ctx,
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.workflow_params.create_action",
                new=AsyncMock(side_effect=fake_create_action),
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.workflow_params.update_action_reasoning",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.script_run_context_manager.get_run_context",
                return_value=None,
            ),
        ):
            return await script_page._create_action_and_result_after_execution(
                action_type=action_type,
                kwargs=kwargs,
            )

    @pytest.mark.asyncio
    async def test_move_persists_x_and_y(self, mock_scraped_page, mock_ai):
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import MoveAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []
        await self._invoke(
            script_page,
            ActionType.MOVE,
            {"x": 100, "y": 200},
            self._build_context(),
            captured,
        )

        assert len(captured) == 1
        action = captured[0]
        assert isinstance(action, MoveAction)
        assert action.x == 100
        assert action.y == 200

    @pytest.mark.asyncio
    async def test_scroll_persists_scroll_x_and_scroll_y(self, mock_scraped_page, mock_ai):
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import ScrollAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []
        await self._invoke(
            script_page,
            ActionType.SCROLL,
            {"scroll_x": 0, "scroll_y": 500},
            self._build_context(),
            captured,
        )

        assert len(captured) == 1
        action = captured[0]
        assert isinstance(action, ScrollAction)
        assert action.scroll_x == 0
        assert action.scroll_y == 500

    @pytest.mark.asyncio
    async def test_drag_persists_start_coords_and_path(self, mock_scraped_page, mock_ai):
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import DragAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []
        await self._invoke(
            script_page,
            ActionType.DRAG,
            {"start_x": 10, "start_y": 20, "path": [(30, 40), (50, 60)]},
            self._build_context(),
            captured,
        )

        assert len(captured) == 1
        action = captured[0]
        assert isinstance(action, DragAction)
        assert action.start_x == 10
        assert action.start_y == 20
        assert action.path == [(30, 40), (50, 60)]

    @pytest.mark.asyncio
    async def test_keypress_persists_keys(self, mock_scraped_page, mock_ai):
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import KeypressAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []
        await self._invoke(
            script_page,
            ActionType.KEYPRESS,
            {"keys": ["Enter"], "hold": False, "duration": 0},
            self._build_context(),
            captured,
        )

        assert len(captured) == 1
        action = captured[0]
        assert isinstance(action, KeypressAction)
        assert action.keys == ["Enter"]

    @pytest.mark.asyncio
    async def test_extract_still_maps_prompt_and_schema(self, mock_scraped_page, mock_ai):
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import ExtractAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []
        await self._invoke(
            script_page,
            ActionType.EXTRACT,
            {"prompt": "Extract the price", "schema": {"type": "object"}},
            self._build_context(),
            captured,
        )

        assert len(captured) == 1
        action = captured[0]
        assert isinstance(action, ExtractAction)
        assert action.data_extraction_goal == "Extract the price"
        assert action.data_extraction_schema == {"type": "object"}

    @pytest.mark.asyncio
    async def test_click_records_subclass_and_xpath(self, mock_scraped_page, mock_ai):
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import ClickAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []

        async def fake_create_action(action):
            captured.append(action)
            action.action_id = "act_test"
            return action

        with (
            patch(
                "skyvern.core.script_generations.script_skyvern_page.skyvern_context.current",
                return_value=self._build_context(),
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.workflow_params.create_action",
                new=AsyncMock(side_effect=fake_create_action),
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.app.DATABASE.workflow_params.update_action_reasoning",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "skyvern.core.script_generations.script_skyvern_page.script_run_context_manager.get_run_context",
                return_value=None,
            ),
        ):
            await script_page._create_action_and_result_after_execution(
                action_type=ActionType.CLICK,
                kwargs={"selector": "[name='foo']"},
                call_result="status=ok xpath=//div[@id='foo']",
            )

        assert len(captured) == 1
        action = captured[0]
        assert isinstance(action, ClickAction)
        assert action.xpath == "//div[@id='foo']"

    @pytest.mark.asyncio
    async def test_falls_back_to_base_action_on_validation_error(self, mock_scraped_page, mock_ai):
        """If a subclass has a required field that isn't provided, fall back to base
        Action rather than crash. Mirrors the defensive pattern in hydrate_action
        from PR #10894 (SKY-9512)."""
        from skyvern.webeye.actions.action_types import ActionType
        from skyvern.webeye.actions.actions import Action, VerificationCodeAction

        script_page = self._build_script_page(mock_scraped_page, mock_ai)
        captured: list = []
        await self._invoke(
            script_page,
            ActionType.VERIFICATION_CODE,
            # VerificationCodeAction requires `verification_code: str` — not present
            # in script kwargs (the script-side method only takes `prompt`).
            {"prompt": "Enter the OTP"},
            self._build_context(),
            captured,
        )

        assert len(captured) == 1
        action = captured[0]
        # Falls back to base Action; not VerificationCodeAction.
        assert isinstance(action, Action)
        assert not isinstance(action, VerificationCodeAction)
        assert action.action_type == ActionType.VERIFICATION_CODE


@pytest.mark.asyncio
async def test_fill_form_raises_when_zero_fields_mapped_with_data(mock_scraped_page, mock_ai):
    """RuntimeError when N>0 fields extracted, non-empty data, but mapping is empty."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [
            {"label": "username", "type": "text", "selector": "#user"},
            {"label": "password", "type": "password", "selector": "#pass"},
        ]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "validate_mapping", new=AsyncMock(return_value=True)) as validate_mock,
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
        ):
            with pytest.raises(RuntimeError, match="mapped 0 of 2"):
                await script_page.fill_form({"user_email": "x@y.com", "user_pw": "secret"})

            validate_mock.assert_not_called()
            fill_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fill_form_does_not_raise_when_data_is_empty(mock_scraped_page, mock_ai):
    """Empty data legitimately produces an empty mapping; the 0-mapped guard must not fire."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [{"label": "search", "type": "text", "selector": "#q"}]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "validate_mapping", new=AsyncMock(return_value=True)),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
        ):
            await script_page.fill_form({})
            fill_mock.assert_called_once()


@pytest.mark.asyncio
async def test_fill_form_raises_for_all_file_form_when_mapping_empty(mock_scraped_page, mock_ai):
    """0-mapped guard is unconditional - all-file forms also fall back to AI when mapping is empty,
    since fill_from_mapping's post-fill heuristic match is unreliable enough to risk silent no-ops."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [
            {"label": "resume", "type": "file", "selector": "#resume"},
            {"label": "cover_letter", "type": "file", "selector": "#cl"},
        ]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "validate_mapping", new=AsyncMock(return_value=True)),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
        ):
            with pytest.raises(RuntimeError, match="mapped 0 of 2"):
                await script_page.fill_form({"resume_url": "https://example.com/resume.pdf"})
            fill_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fill_multipage_form_raises_when_zero_fields_mapped_with_data(mock_scraped_page, mock_ai):
    """RuntimeError when a multi-page form has fillable fields and data but maps 0 fields."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [
            {"label": "email", "type": "text", "tag": "input", "selector": "#email", "required": True},
            {"label": "password", "type": "password", "tag": "input", "selector": "#password", "required": True},
        ]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock()) as click_mock,
        ):
            with pytest.raises(RuntimeError, match="fill_multipage_form mapped 0 of 2"):
                await script_page.fill_multipage_form({"username": "x@y.com", "user_pw": "secret"})

            fill_mock.assert_not_called()
            click_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fill_multipage_form_does_not_raise_when_data_is_empty(mock_scraped_page, mock_ai):
    """Empty data can legitimately produce an empty multi-page mapping."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [{"label": "search", "type": "text", "tag": "input", "selector": "#q"}]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock(side_effect=Exception)),
        ):
            pages_filled = await script_page.fill_multipage_form({})

            assert pages_filled == 1
            fill_mock.assert_called_once()


@pytest.mark.asyncio
async def test_fill_multipage_form_skips_unmapped_optional_intermediate_page(mock_scraped_page, mock_ai):
    """Optional intermediate pages with no matching data should still advance."""
    mock_page = create_mock_page()
    mock_page.evaluate = AsyncMock(return_value=[])

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        first_page_fields = [{"label": "email", "type": "text", "tag": "input", "selector": "#email"}]
        optional_page_fields = [
            {
                "label": "demographics",
                "type": "radio_group",
                "tag": "input",
                "selector": "#demographics",
                "required": False,
            }
        ]
        final_page_fields = [{"label": "phone", "type": "text", "tag": "input", "selector": "#phone", "required": True}]

        with (
            patch.object(
                script_page,
                "extract_form_fields",
                new=AsyncMock(
                    side_effect=[
                        first_page_fields,
                        first_page_fields,
                        optional_page_fields,
                        optional_page_fields,
                        final_page_fields,
                        final_page_fields,
                    ]
                ),
            ),
            patch.object(
                script_page, "dynamic_field_map", new=AsyncMock(side_effect=[{0: "x@y.com"}, {}, {0: "555-0100"}])
            ),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock(side_effect=[None, None, Exception])) as click_mock,
            patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new=AsyncMock()),
        ):
            pages_filled = await script_page.fill_multipage_form({"email": "x@y.com", "phone": "555-0100"})

            assert pages_filled == 3
            assert fill_mock.call_count == 3
            assert click_mock.call_count == 3


@pytest.mark.asyncio
async def test_fill_multipage_form_skips_unmapped_optional_first_page(mock_scraped_page, mock_ai):
    """Optional first pages with no matching data should still advance to later pages."""
    mock_page = create_mock_page()
    mock_page.evaluate = AsyncMock(return_value=[])

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        optional_page_fields = [
            {
                "label": "referral source",
                "type": "radio_group",
                "tag": "input",
                "selector": "#referral",
                "required": False,
            }
        ]
        second_page_fields = [
            {"label": "email", "type": "text", "tag": "input", "selector": "#email", "required": True}
        ]

        with (
            patch.object(
                script_page,
                "extract_form_fields",
                new=AsyncMock(
                    side_effect=[
                        optional_page_fields,
                        optional_page_fields,
                        second_page_fields,
                        second_page_fields,
                    ]
                ),
            ),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(side_effect=[{}, {0: "x@y.com"}])),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock(side_effect=[None, Exception])) as click_mock,
            patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new=AsyncMock()),
        ):
            pages_filled = await script_page.fill_multipage_form({"email": "x@y.com"})

            assert pages_filled == 2
            assert fill_mock.call_count == 2
            assert click_mock.call_count == 2


@pytest.mark.asyncio
async def test_fill_multipage_form_raises_when_unmapped_optional_page_cannot_advance(mock_scraped_page, mock_ai):
    """An unmapped optional page must either advance or raise to avoid silent no-op success."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [
            {
                "label": "referral source",
                "type": "radio_group",
                "tag": "input",
                "selector": "#referral",
                "required": False,
            }
        ]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock(side_effect=Exception)) as click_mock,
        ):
            with pytest.raises(RuntimeError, match="could not advance"):
                await script_page.fill_multipage_form({"email": "x@y.com"})

            fill_mock.assert_called_once()
            click_mock.assert_called_once()


@pytest.mark.asyncio
async def test_fill_multipage_form_raises_when_unmapped_optional_page_stays_put(mock_scraped_page, mock_ai):
    """An unmapped optional page must not count as skippable if the next click does not advance."""
    mock_page = create_mock_page()
    mock_page.evaluate = AsyncMock(return_value=[])

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [
            {
                "label": "referral source",
                "type": "radio_group",
                "tag": "input",
                "selector": "#referral",
                "required": False,
            }
        ]

        with (
            patch.object(
                script_page,
                "extract_form_fields",
                new=AsyncMock(side_effect=[form_fields, form_fields, form_fields]),
            ),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock()) as click_mock,
            patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(RuntimeError, match="next click did not advance"):
                await script_page.fill_multipage_form({"email": "x@y.com"})

            fill_mock.assert_called_once()
            click_mock.assert_called_once()


@pytest.mark.asyncio
async def test_fill_multipage_form_allows_unmapped_optional_file_page_to_stop(mock_scraped_page, mock_ai):
    """Unmapped optional file pages may succeed through fill_from_mapping's URL fallback."""
    mock_page = create_mock_page()

    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=mock_page,
            ai=mock_ai,
        )

        form_fields = [
            {
                "label": "resume",
                "type": "file",
                "tag": "input",
                "selector": "#resume",
                "required": False,
            }
        ]

        with (
            patch.object(script_page, "extract_form_fields", new=AsyncMock(return_value=form_fields)),
            patch.object(script_page, "dynamic_field_map", new=AsyncMock(return_value={})),
            patch.object(script_page, "fill_from_mapping", new=AsyncMock()) as fill_mock,
            patch.object(script_page, "click", new=AsyncMock(side_effect=Exception)) as click_mock,
        ):
            pages_filled = await script_page.fill_multipage_form({"resume_url": "https://example.com/resume.pdf"})

            assert pages_filled == 1
            fill_mock.assert_called_once()
            click_mock.assert_called_once()
