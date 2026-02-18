"""
Unit tests for ScriptSkyvernPage.

Tests _wait_for_page_ready_before_action (regression test for self._page bug, PR #8425)
and _ensure_element_ids_on_page (injects unique_id attrs after page navigation).
"""

import inspect
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.core.script_generations.script_skyvern_page import ScriptSkyvernPage


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
    assert "self.page" in source, "Method should access self.page"

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
