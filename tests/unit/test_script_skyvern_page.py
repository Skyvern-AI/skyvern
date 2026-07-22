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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.core.script_generations.real_skyvern_page_ai import RealSkyvernPageAi
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage
from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.exceptions import IllegitCompleteScriptTermination, NoTOTPSecretFound, ScriptTerminationException


class _KeyboardStub:
    async def press(self, *_args: object, **_kwargs: object) -> None:
        return None


class _PageStub:
    def __init__(self) -> None:
        self.url = "https://example.com"
        self.keyboard = _KeyboardStub()

    async def evaluate(self, *_args: object, **_kwargs: object) -> None:
        return None


def create_mock_page() -> _PageStub:
    return _PageStub()


class _AiStub:
    pass


class _ScrapedPageStub:
    def __init__(self) -> None:
        self._browser_state = SimpleNamespace()


@pytest.fixture(autouse=True)
def no_cached_action_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CACHED_ACTION_DELAY_SECONDS", 0)


@pytest.fixture
def mock_scraped_page():
    return _ScrapedPageStub()


@pytest.fixture
def mock_ai():
    return _AiStub()


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
    mock_page.evaluate = AsyncMock(return_value=True)  # unique_ids exist

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
    mock_page.evaluate = AsyncMock(return_value=False)  # no unique_ids

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
    mock_page.evaluate = AsyncMock(side_effect=Exception("Page crashed"))

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


@pytest.mark.asyncio
async def test_fill_autocomplete_does_not_fall_back_to_unresolved_totp_placeholder(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(side_effect=NoTOTPSecretFound())
        script_page._do_autocomplete = AsyncMock(return_value="placeholder_AbCd_totp")

        with pytest.raises(NoTOTPSecretFound):
            await script_page.fill_autocomplete(
                selector="#otp",
                value="placeholder_AbCd_totp",
                ai="fallback",
            )

        script_page._do_autocomplete.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_autocomplete_rejects_raw_totp_placeholder_after_resolution(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(return_value="placeholder_AbCd_totp")
        script_page._do_autocomplete = AsyncMock(return_value="placeholder_AbCd_totp")

        with pytest.raises(NoTOTPSecretFound):
            await script_page.fill_autocomplete(
                selector="#otp",
                value="placeholder_AbCd_totp",
                ai="fallback",
            )

        script_page._do_autocomplete.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_autocomplete_proactive_rejects_raw_totp_placeholder_before_ai(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(return_value="placeholder_AbCd_totp")
        script_page._ai.ai_input_text = AsyncMock(return_value="placeholder_AbCd_totp")

        with pytest.raises(NoTOTPSecretFound):
            await script_page.fill_autocomplete(
                selector="#otp",
                value="placeholder_AbCd_totp",
                prompt="Enter the verification code",
                ai="proactive",
            )

        script_page._ai.ai_input_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_autocomplete_proactive_validates_totp_without_exposing_code_to_ai(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(return_value="654321")
        script_page._ai.ai_input_text = AsyncMock(return_value="654321")

        result = await script_page.fill_autocomplete(
            selector="#otp",
            value="placeholder_AbCd_totp",
            prompt="Enter the verification code",
            ai="proactive",
        )

        assert result == "654321"
        assert script_page._ai.ai_input_text.await_args.kwargs["value"] == "placeholder_AbCd_totp"
        assert "654321" not in script_page._ai.ai_input_text.await_args.kwargs.values()


@pytest.mark.asyncio
async def test_input_text_does_not_send_unresolved_totp_placeholder_to_ai_fallback(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(side_effect=NoTOTPSecretFound())
        script_page._ai.ai_input_text = AsyncMock(return_value="placeholder_AbCd_totp")

        with pytest.raises(NoTOTPSecretFound):
            await script_page._input_text(
                selector="#otp",
                value="placeholder_AbCd_totp",
                ai="fallback",
                intention="Enter the verification code",
            )

        script_page._ai.ai_input_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_text_regenerates_totp_after_selector_preparation(mock_scraped_page, mock_ai):
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

        events: list[str] = []
        generated_codes = iter(("123456", "654321"))
        script_page.get_actual_value = AsyncMock(
            side_effect=lambda *_args, **_kwargs: events.append("generate") or next(generated_codes)
        )
        locator = MagicMock()
        locator.fill = AsyncMock(side_effect=lambda value, **_kwargs: events.append(f"fill:{value}"))
        script_page._wait_for_selector_with_retry = AsyncMock(
            side_effect=lambda *_args, **_kwargs: events.append("selector") or locator
        )
        script_page._prepare_element = AsyncMock(side_effect=lambda *_args, **_kwargs: events.append("prepare"))

        result = await script_page._input_text(
            selector="#otp",
            value="placeholder_AbCd_totp",
            ai="fallback",
        )

        assert result == "placeholder_AbCd_totp"
        assert events == ["generate", "selector", "prepare", "generate", "fill:654321"]
        locator.fill.assert_awaited_once_with("654321", timeout=settings.BROWSER_ACTION_TIMEOUT_MS)


@pytest.mark.asyncio
async def test_fill_from_mapping_keeps_totp_placeholder_out_of_ai_prompt(mock_scraped_page, mock_ai):
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

        script_page._resolve_totp_placeholder_or_raise = AsyncMock(return_value="654321")
        script_page.fill = AsyncMock(side_effect=(RuntimeError("selector fill failed"), "placeholder_AbCd_totp"))

        await script_page.fill_from_mapping(
            form_fields=[{"selector": "#otp", "type": "text", "tag": "input", "label": "Verification code"}],
            mapping={0: "placeholder_AbCd_totp"},
        )

        script_page._resolve_totp_placeholder_or_raise.assert_not_awaited()
        assert script_page.fill.await_count == 2
        first_fill = script_page.fill.await_args_list[0].kwargs
        fallback_fill = script_page.fill.await_args_list[1].kwargs
        assert first_fill == {"selector": "#otp", "value": "placeholder_AbCd_totp", "ai": None}
        assert fallback_fill["value"] == "placeholder_AbCd_totp"
        assert fallback_fill["ai"] == "fallback"
        assert "654321" not in str(fallback_fill)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fill_side_effect, expected_fill_count",
    [
        ((NoTOTPSecretFound(),), 1),
        ((RuntimeError("selector fill failed"), NoTOTPSecretFound()), 2),
    ],
)
async def test_fill_from_mapping_propagates_totp_resolution_failures(
    mock_scraped_page,
    mock_ai,
    fill_side_effect,
    expected_fill_count,
):
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

        script_page.fill = AsyncMock(side_effect=fill_side_effect)

        with pytest.raises(NoTOTPSecretFound):
            await script_page.fill_from_mapping(
                form_fields=[{"selector": "#otp", "type": "text", "tag": "input", "label": "Verification code"}],
                mapping={0: "placeholder_AbCd_totp"},
            )

        assert script_page.fill.await_count == expected_fill_count


@pytest.mark.asyncio
async def test_input_text_rejects_raw_totp_placeholder_after_resolution(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(return_value="placeholder_AbCd_totp")
        script_page._ai.ai_input_text = AsyncMock(return_value="placeholder_AbCd_totp")

        with pytest.raises(NoTOTPSecretFound):
            await script_page._input_text(
                selector="#otp",
                value="placeholder_AbCd_totp",
                ai="fallback",
                intention="Enter the verification code",
            )

        script_page._ai.ai_input_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ai", [None, "proactive"])
async def test_input_text_rejects_raw_totp_placeholder_in_nonfallback_modes(mock_scraped_page, mock_ai, ai):
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

        script_page.get_actual_value = AsyncMock(return_value="placeholder_AbCd_totp")
        script_page._ai.ai_input_text = AsyncMock(return_value="placeholder_AbCd_totp")

        with pytest.raises(NoTOTPSecretFound):
            await script_page._input_text(
                selector="#otp",
                value="placeholder_AbCd_totp",
                ai=ai,
                intention="Enter the verification code",
            )

        script_page._ai.ai_input_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_input_text_proactive_validates_totp_without_exposing_code_to_ai(mock_scraped_page, mock_ai):
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

        script_page.get_actual_value = AsyncMock(return_value="654321")
        script_page._ai.ai_input_text = AsyncMock(return_value="654321")

        result = await script_page._input_text(
            selector="#otp",
            value="placeholder_AbCd_totp",
            ai="proactive",
            intention="Enter the verification code",
        )

        assert result == "654321"
        assert script_page._ai.ai_input_text.await_args.kwargs["value"] == "placeholder_AbCd_totp"
        assert "654321" not in script_page._ai.ai_input_text.await_args.kwargs.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["OP_TOTP", "BW_TOTP", "AZ_TOTP"])
async def test_input_text_rejects_raw_totp_markers_in_direct_mode(mock_scraped_page, mock_ai, marker):
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

        script_page.get_actual_value = AsyncMock(return_value=marker)

        with pytest.raises(NoTOTPSecretFound):
            await script_page._input_text(selector="#otp", value=marker, ai=None)


@pytest.mark.asyncio
async def test_real_page_ai_rejects_raw_totp_placeholder_before_fallback() -> None:
    page_ai = RealSkyvernPageAi.__new__(RealSkyvernPageAi)
    page_ai._maybe_run_v3_midrun = AsyncMock(return_value=(None, False))

    with (
        patch(
            "skyvern.core.script_generations.real_skyvern_page_ai.skyvern_context.ensure_context",
            return_value=SimpleNamespace(
                organization_id=None,
                task_id=None,
                step_id=None,
                workflow_run_id=None,
            ),
        ),
        pytest.raises(NoTOTPSecretFound),
    ):
        await page_ai.ai_input_text(
            selector="#otp",
            value="placeholder_AbCd_totp",
            intention="",
            failed_selector="#stale-otp",
        )

    page_ai._maybe_run_v3_midrun.assert_not_awaited()


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


# =============================================================================
# Tests for fill(mode="direct") — JS-gate event dispatch (SKY-11111)
# =============================================================================


class _RecordingLocator:
    """A fake Playwright locator that records fill/dispatch_event calls in order.

    Optionally models a JS gate by flipping `enabled` to True when it observes a
    `change` dispatch — the unit-level proxy for a disabled control whose listener
    keys on `change`.
    """

    def __init__(
        self,
        *,
        click_error: Exception | None = None,
        dispatch_error: Exception | None = None,
        gate_on_change: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.first = self
        self._click_error = click_error
        self._dispatch_error = dispatch_error
        self._gate_on_change = gate_on_change
        self.enabled = False

    async def wait_for(self, *, state: str, **kwargs: object) -> None:
        self.calls.append(("wait_for", state))

    async def scroll_into_view_if_needed(self, **kwargs: object) -> None:
        self.calls.append(("scroll_into_view_if_needed", None))

    async def click(self, **kwargs: object) -> None:
        self.calls.append(("click", None))
        if self._click_error is not None:
            raise self._click_error

    async def fill(self, value: str, **kwargs: object) -> None:
        self.calls.append(("fill", value))

    async def dispatch_event(self, event_name: str, **kwargs: object) -> None:
        self.calls.append(("dispatch_event", event_name))
        if self._dispatch_error is not None:
            raise self._dispatch_error
        if self._gate_on_change and event_name == "change":
            self.enabled = True


class _RecordingLocatorScope:
    def __init__(self, locator: _RecordingLocator) -> None:
        self._locator = locator

    def locator(self, _selector: str, **_kwargs: object) -> _RecordingLocator:
        return self._locator


def _direct_fill_page(mock_scraped_page, mock_ai, locator: _RecordingLocator) -> ScriptSkyvernPage:
    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        script_page = ScriptSkyvernPage(
            scraped_page=mock_scraped_page,
            page=create_mock_page(),
            ai=mock_ai,
        )
    script_page._working_frame = _RecordingLocatorScope(locator)
    return script_page


def _skyvern_page_with_locator(mock_ai, locator: _RecordingLocator) -> SkyvernPage:
    with patch(
        "skyvern.core.script_generations.skyvern_page.Page.__init__",
        return_value=None,
    ):
        raw_page = create_mock_page()
        skyvern_page = SkyvernPage(
            page=raw_page,
            ai=mock_ai,
        )
    skyvern_page._working_frame = _RecordingLocatorScope(locator)
    return skyvern_page


@pytest.mark.asyncio
async def test_direct_fill_rejects_raw_totp_placeholder_before_browser_write(mock_scraped_page, mock_ai):
    locator = _RecordingLocator()
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)
    script_page.get_actual_value = AsyncMock(return_value="placeholder_AbCd_totp")

    with pytest.raises(NoTOTPSecretFound):
        await script_page.fill("#otp", "placeholder_AbCd_totp", mode="direct")

    assert locator.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unresolved_value",
    [
        " placeholder_AbCd_totp",
        "placeholder_AbCd_totp ",
        "code: placeholder_AbCd_totp",
        "prefixplaceholder_AbCd_totpsuffix",
    ],
)
async def test_direct_fill_rejects_embedded_totp_placeholder_before_browser_write(
    mock_scraped_page,
    mock_ai,
    unresolved_value,
):
    locator = _RecordingLocator()
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)
    script_page.get_actual_value = AsyncMock(return_value=unresolved_value)

    with pytest.raises(NoTOTPSecretFound):
        await script_page.fill("#otp", unresolved_value, mode="direct")

    assert locator.calls == []


@pytest.mark.asyncio
async def test_direct_fill_resolves_totp_placeholder_before_browser_write(mock_scraped_page, mock_ai):
    locator = _RecordingLocator()
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)
    script_page.get_actual_value = AsyncMock(return_value="654321")

    result = await script_page.fill("#otp", "placeholder_AbCd_totp", mode="direct")

    assert result == "654321"
    assert ("fill", "654321") in locator.calls
    assert ("fill", "placeholder_AbCd_totp") not in locator.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["OP_TOTP", "BW_TOTP", "AZ_TOTP"])
async def test_direct_fill_rejects_raw_totp_markers_before_browser_write(mock_scraped_page, mock_ai, marker):
    locator = _RecordingLocator()
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)
    script_page.get_actual_value = AsyncMock(return_value=marker)

    with pytest.raises(NoTOTPSecretFound):
        await script_page.fill("#otp", marker, mode="direct")

    assert locator.calls == []


@pytest.mark.asyncio
async def test_direct_fill_dispatches_change_and_blur_after_value_set(mock_scraped_page, mock_ai):
    """mode='direct' must dispatch change/blur after the value-set so a JS gate
    listening on change/blur unlocks exactly as it does for real keystrokes."""
    locator = _RecordingLocator()
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)

    result = await script_page.fill("#username", "user@example.com", mode="direct")

    assert result == "user@example.com"
    event_names = [name for kind, name in locator.calls if kind == "dispatch_event"]
    assert "change" in event_names
    assert "blur" in event_names
    fill_index = locator.calls.index(("fill", "user@example.com"))
    change_index = locator.calls.index(("dispatch_event", "change"))
    blur_index = locator.calls.index(("dispatch_event", "blur"))
    assert fill_index < change_index < blur_index


@pytest.mark.asyncio
async def test_direct_fill_enables_change_gated_control(mock_scraped_page, mock_ai):
    """Behavioral AC1: a control gated on a `change` listener becomes enabled after
    the direct fill (parity with real keystrokes)."""
    locator = _RecordingLocator(gate_on_change=True)
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)

    assert locator.enabled is False
    await script_page.fill("#username", "user@example.com", mode="direct")
    assert locator.enabled is True


@pytest.mark.asyncio
async def test_direct_fill_returns_value_and_fills_once(mock_scraped_page, mock_ai):
    """AC3: single-step value-on-submit forms are unaffected — the value is set once
    and returned; the extra events are inert for forms that read `.value` at submit."""
    locator = _RecordingLocator()
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)

    result = await script_page.fill("#password", "hunter2", mode="direct")

    assert result == "hunter2"
    assert [call for call in locator.calls if call[0] == "fill"] == [("fill", "hunter2")]


@pytest.mark.asyncio
async def test_direct_fill_dispatch_failure_does_not_regress_fill(mock_scraped_page, mock_ai):
    """A synthetic-event failure on an exotic element must not fail the fill — the
    value is already set, so dispatch is best-effort."""
    locator = _RecordingLocator(dispatch_error=RuntimeError("element rejected synthetic event"))
    script_page = _direct_fill_page(mock_scraped_page, mock_ai, locator)

    result = await script_page.fill("#password", "hunter2", mode="direct")

    assert result == "hunter2"


# =============================================================================
# Tests for selector fallback prep opt-out — SKY-12096
#
# Patching skyvern_page.asyncio.sleep rebinds the GLOBAL asyncio.sleep, so the mock also
# records sleeps from coroutines running on other threads' event loops. Assert on locator
# behavior or on this path's own sleep values — never on bare await_args.
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["click", "fill", "type"])
async def test_selector_fallback_default_runs_element_prep(mock_scraped_page, mock_ai, action: str):
    locator = _RecordingLocator()
    skyvern_page = _skyvern_page_with_locator(mock_ai, locator)

    with patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new_callable=AsyncMock) as sleep:
        if action == "click":
            result = await skyvern_page.click("#target")
        elif action == "fill":
            result = await skyvern_page.fill("#target", "Noor")
        else:
            result = await skyvern_page.type("#target", "Noor")

    assert result in ("#target", "Noor")
    assert ("wait_for", "attached") in locator.calls
    assert ("wait_for", "visible") in locator.calls
    assert ("scroll_into_view_if_needed", None) in locator.calls
    assert call(0.15) in sleep.await_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["click", "fill", "type"])
async def test_selector_fallback_prep_opt_out_uses_playwright_actionability(
    mock_scraped_page,
    mock_ai,
    action: str,
):
    locator = _RecordingLocator()
    skyvern_page = _skyvern_page_with_locator(mock_ai, locator)

    with patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new_callable=AsyncMock) as sleep:
        if action == "click":
            result = await skyvern_page.click("#target", _skip_element_prep=True)
        elif action == "fill":
            result = await skyvern_page.fill("#target", "Noor", _skip_element_prep=True)
        else:
            result = await skyvern_page.type("#target", "Noor", _skip_element_prep=True)

    assert result in ("#target", "Noor")
    assert not any(recorded[0] == "wait_for" for recorded in locator.calls)
    assert ("scroll_into_view_if_needed", None) not in locator.calls
    assert call(0.15) not in sleep.await_args_list


@pytest.mark.asyncio
async def test_selector_click_prep_opt_out_preserves_direct_timeout_failure(mock_ai):
    locator = _RecordingLocator(click_error=PlaywrightTimeoutError("Timeout 5000ms exceeded."))
    skyvern_page = _skyvern_page_with_locator(mock_ai, locator)

    with patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(PlaywrightTimeoutError):
            await skyvern_page.click("#target", _skip_element_prep=True, timeout=5000)

    # A single click means the escape-dismiss retry never ran.
    assert [recorded for recorded in locator.calls if recorded[0] == "click"] == [("click", None)]


@pytest.mark.asyncio
async def test_selector_click_prep_opt_out_keeps_escape_retry_for_interception(mock_ai):
    locator = _RecordingLocator(
        click_error=PlaywrightTimeoutError("<div class='overlay'></div> intercepts pointer events")
    )
    skyvern_page = _skyvern_page_with_locator(mock_ai, locator)

    with patch("skyvern.core.script_generations.skyvern_page.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(PlaywrightTimeoutError):
            await skyvern_page.click("#target", _skip_element_prep=True, timeout=5000)

    # The second click is the retry after dismissing the intercepting overlay.
    assert [recorded for recorded in locator.calls if recorded[0] == "click"] == [
        ("click", None),
        ("click", None),
    ]


@pytest.mark.asyncio
async def test_get_or_create_browser_state_forwards_script_id_to_manager(monkeypatch):
    # MUST_FIX 1: the production script caller must forward the active run's script_id so the
    # standalone-script browser is pinned under a real id (and, in cloud, used as the flag distinct id)
    # instead of the id being discarded and the run forced into default-only behavior.
    from skyvern.forge import app
    from skyvern.forge.sdk.core import skyvern_context

    manager = MagicMock()
    manager.get_or_create_for_script = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)

    skyvern_context.set(skyvern_context.SkyvernContext(organization_id="org_1", script_id="scr_1"))
    try:
        await ScriptSkyvernPage._get_or_create_browser_state()
    finally:
        skyvern_context.reset()

    manager.get_or_create_for_script.assert_awaited_once()
    assert manager.get_or_create_for_script.await_args.kwargs["script_id"] == "scr_1"
    # MF2: the real organization_id is forwarded so acquisition uses the same (session, org) key as release.
    assert manager.get_or_create_for_script.await_args.kwargs["organization_id"] == "org_1"


@pytest.mark.asyncio
async def test_get_or_create_browser_state_sources_session_id_from_context(monkeypatch):
    # The acquire must key browser_session_id off the run context (like organization_id/script_id), so it
    # matches what run_script's cleanup releases. Otherwise a standalone run carrying a session id builds a
    # non-persistent browser here but is treated as a persistent session at cleanup and leaked.
    from skyvern.forge import app
    from skyvern.forge.sdk.core import skyvern_context

    manager = MagicMock()
    manager.get_or_create_for_script = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)

    skyvern_context.set(
        skyvern_context.SkyvernContext(organization_id="org_1", script_id="scr_1", browser_session_id="session_1")
    )
    try:
        await ScriptSkyvernPage._get_or_create_browser_state()  # generated caller passes no browser_session_id
    finally:
        skyvern_context.reset()

    assert manager.get_or_create_for_script.await_args.kwargs["browser_session_id"] == "session_1"


@pytest.mark.asyncio
async def test_get_or_create_browser_state_records_effective_session_on_context(monkeypatch):
    # An explicit session passed to acquire (e.g. setup(browser_session_id=...)) must be recorded on the run
    # context, so run_script's terminal cleanup releases the same session it attached even when run_script
    # itself was invoked without one. Otherwise cleanup closes the reusable browser and skips release.
    from skyvern.forge import app
    from skyvern.forge.sdk.core import skyvern_context

    manager = MagicMock()
    manager.get_for_script = MagicMock(return_value=None)  # fresh acquisition (no cached state yet)
    manager.get_or_create_for_script = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)

    ctx = skyvern_context.SkyvernContext(organization_id="org_1", script_id="scr_1")  # no session initially
    skyvern_context.set(ctx)
    try:
        await ScriptSkyvernPage._get_or_create_browser_state(browser_session_id="pbs_explicit")
    finally:
        skyvern_context.reset()

    assert manager.get_or_create_for_script.await_args.kwargs["browser_session_id"] == "pbs_explicit"
    assert ctx.browser_session_id == "pbs_explicit"  # recorded so terminal cleanup releases the same session


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_session", [None, "pbs_A"])
async def test_get_or_create_browser_state_fails_closed_on_mid_run_session_switch(monkeypatch, bound_session):
    # A script's browser is pinned under script_id at first acquire; get_or_create_for_script's cache hit
    # would return that existing state and ignore a later, different requested session. Recording the new
    # session on context would then make cleanup release an unattached session and leak the cached browser.
    # Fail closed BEFORE mutating context, covering cached-local (None) and cached-session-A bindings; the
    # bound identity on context must be preserved for terminal cleanup.
    from skyvern.exceptions import BrowserSessionSwitchNotAllowed
    from skyvern.forge import app
    from skyvern.forge.sdk.core import skyvern_context

    manager = MagicMock()
    manager.get_for_script = MagicMock(return_value=MagicMock())  # a browser is already cached for scr_1
    manager.get_or_create_for_script = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)

    ctx = skyvern_context.SkyvernContext(organization_id="org_1", script_id="scr_1", browser_session_id=bound_session)
    skyvern_context.set(ctx)
    try:
        with pytest.raises(BrowserSessionSwitchNotAllowed):
            await ScriptSkyvernPage._get_or_create_browser_state(browser_session_id="pbs_B")
    finally:
        skyvern_context.reset()

    assert ctx.browser_session_id == bound_session  # rejected request never overwrote the bound identity
    manager.get_or_create_for_script.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_or_create_browser_state_allows_same_session_cache_reuse(monkeypatch):
    # Re-acquiring the SAME session (or none) against a cached state is legitimate reuse — not a switch.
    from skyvern.forge import app
    from skyvern.forge.sdk.core import skyvern_context

    manager = MagicMock()
    manager.get_for_script = MagicMock(return_value=MagicMock())  # cached state exists
    manager.get_or_create_for_script = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)

    ctx = skyvern_context.SkyvernContext(organization_id="org_1", script_id="scr_1", browser_session_id="pbs_A")
    skyvern_context.set(ctx)
    try:
        await ScriptSkyvernPage._get_or_create_browser_state(browser_session_id="pbs_A")
    finally:
        skyvern_context.reset()

    manager.get_or_create_for_script.assert_awaited_once()
    assert ctx.browser_session_id == "pbs_A"


@pytest.mark.asyncio
async def test_get_or_create_browser_state_does_not_record_session_on_acquire_failure(monkeypatch):
    # A cold/evicted requested session makes get_or_create_for_script fail closed. The requested session must
    # NOT be written to context, or run_script's terminal cleanup would release a session the script never
    # acquired (and clear its runnable_id, detaching another run's session). The prior binding must stand.
    from skyvern.exceptions import MissingBrowserStateForBrowserSession
    from skyvern.forge import app
    from skyvern.forge.sdk.core import skyvern_context

    manager = MagicMock()
    manager.get_for_script = MagicMock(return_value=None)  # fresh acquisition
    manager.get_or_create_for_script = AsyncMock(side_effect=MissingBrowserStateForBrowserSession("pbs_cold"))
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)

    ctx = skyvern_context.SkyvernContext(organization_id="org_1", script_id="scr_1")  # no prior session bound
    skyvern_context.set(ctx)
    try:
        with pytest.raises(MissingBrowserStateForBrowserSession):
            await ScriptSkyvernPage._get_or_create_browser_state(browser_session_id="pbs_cold")
    finally:
        skyvern_context.reset()

    assert ctx.browser_session_id is None  # requested session not recorded because the attach failed
