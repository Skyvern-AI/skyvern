from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock

import pytest
from playwright._impl._errors import Error as PlaywrightError

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.utils.page import (
    SkyvernFrame,
    _is_navigation_context_lost,
    _wait_for_navigation_settle,
)


class TestIsNavigationContextLost:
    def test_execution_context_destroyed(self) -> None:
        assert (
            _is_navigation_context_lost(
                "Page.evaluate: Execution context was destroyed, most likely because of a navigation."
            )
            is True
        )

    def test_reference_error_not_defined(self) -> None:
        assert _is_navigation_context_lost("Page.evaluate: ReferenceError: scrollToXY is not defined") is True

    def test_missing_protocol_context(self) -> None:
        assert (
            _is_navigation_context_lost(
                "Page.evaluate: Protocol error (DOM.describeNode): Cannot find context with specified id"
            )
            is True
        )

    def test_unrelated_error(self) -> None:
        assert _is_navigation_context_lost("Page.evaluate: TypeError: Cannot read properties of null") is False

    def test_empty_string(self) -> None:
        assert _is_navigation_context_lost("") is False


class TestWaitForNavigationSettle:
    @pytest.mark.asyncio
    async def test_uses_networkidle(self) -> None:
        frame = AsyncMock()
        frame.wait_for_load_state = AsyncMock()
        await _wait_for_navigation_settle(frame, timeout_ms=3000)
        frame.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=3000)

    @pytest.mark.asyncio
    async def test_swallows_playwright_error(self) -> None:
        frame = AsyncMock()
        frame.wait_for_load_state = AsyncMock(side_effect=PlaywrightError("Timeout"))
        await _wait_for_navigation_settle(frame, timeout_ms=3000)

    @pytest.mark.asyncio
    async def test_zero_timeout_returns_immediately(self) -> None:
        frame = AsyncMock()
        await _wait_for_navigation_settle(frame, timeout_ms=0)
        frame.wait_for_load_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_timeout_returns_immediately(self) -> None:
        frame = AsyncMock()
        await _wait_for_navigation_settle(frame, timeout_ms=-100)
        frame.wait_for_load_state.assert_not_awaited()


def _context_destroyed_error() -> PlaywrightError:
    return PlaywrightError("Page.evaluate: Execution context was destroyed, most likely because of a navigation.")


def _reference_error() -> PlaywrightError:
    return PlaywrightError("Page.evaluate: ReferenceError: scrollToXY is not defined")


class TestEvaluateWithNavigationRecovery:
    @pytest.mark.asyncio
    async def test_evaluate_timeout_raises_skyvern_page_analysis_timeout(self) -> None:
        frame = AsyncMock()
        source_error = asyncio.TimeoutError()
        frame.evaluate = AsyncMock(side_effect=source_error)

        with pytest.raises(
            SkyvernPageAnalysisTimeout, match="Skyvern timed out trying to analyze the page"
        ) as exc_info:
            await SkyvernFrame.evaluate(frame=frame, expression="() => 42", timeout_ms=30000)

        assert exc_info.value.__cause__ is source_error

    @pytest.mark.asyncio
    async def test_navigation_recovery_deadline_raises_skyvern_page_analysis_timeout(self) -> None:
        frame = AsyncMock()

        with pytest.raises(SkyvernPageAnalysisTimeout, match="Skyvern timed out trying to analyze the page"):
            await SkyvernFrame._evaluate_with_navigation_recovery(
                frame=frame,
                expression="() => 42",
                evaluate_expression=AsyncMock(),
                timeout_ms=0,
                initial_error="execution context destroyed",
            )

    @pytest.mark.asyncio
    async def test_recovers_after_one_context_destroyed(self) -> None:
        """First eval fails, re-inject + retry succeeds."""
        frame = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=[
                _context_destroyed_error(),
                None,
                42,
            ]
        )
        frame.wait_for_load_state = AsyncMock()

        result = await SkyvernFrame.evaluate(frame=frame, expression="() => 42", timeout_ms=30000)
        assert result == 42

    @pytest.mark.asyncio
    async def test_recovers_after_reference_error(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=[
                _reference_error(),
                None,
                "ok",
            ]
        )
        frame.wait_for_load_state = AsyncMock()

        result = await SkyvernFrame.evaluate(frame=frame, expression="() => getScrollXY()", timeout_ms=30000)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_fails_after_max_attempts_exhausted(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(side_effect=_context_destroyed_error())
        frame.wait_for_load_state = AsyncMock()

        with pytest.raises(PlaywrightError, match="Execution context was destroyed"):
            await SkyvernFrame.evaluate(frame=frame, expression="() => 1", timeout_ms=30000)

    @pytest.mark.asyncio
    async def test_injection_succeeds_but_retry_context_destroyed_continues(self) -> None:
        """SSO form_post pattern: inject succeeds but expression eval hits another navigation."""
        frame = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=[
                _context_destroyed_error(),
                None,
                _context_destroyed_error(),
                None,
                99,
            ]
        )
        frame.wait_for_load_state = AsyncMock()

        result = await SkyvernFrame.evaluate(frame=frame, expression="() => 99", timeout_ms=30000)
        assert result == 99

    @pytest.mark.asyncio
    async def test_non_navigation_error_propagates(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(side_effect=PlaywrightError("Page.evaluate: TypeError: null is not an object"))

        with pytest.raises(PlaywrightError, match="TypeError"):
            await SkyvernFrame.evaluate(frame=frame, expression="() => null.foo", timeout_ms=30000)

    @pytest.mark.asyncio
    async def test_settle_wait_called_with_networkidle(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=[
                _context_destroyed_error(),
                None,
                "done",
            ]
        )
        frame.wait_for_load_state = AsyncMock()

        await SkyvernFrame.evaluate(frame=frame, expression="() => 1", timeout_ms=30000)
        frame.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=ANY)


class TestGetElementVisible:
    @pytest.mark.asyncio
    async def test_stale_locator_context_reinjects_and_reresolves(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(return_value=None)
        frame.wait_for_load_state = AsyncMock()
        locator = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.evaluate = AsyncMock(
            side_effect=[
                PlaywrightError(
                    "Locator.evaluate: Protocol error (DOM.describeNode): Cannot find context with specified id"
                ),
                True,
            ]
        )

        result = await SkyvernFrame(frame).get_element_visible(locator)

        assert result is True
        assert locator.evaluate.await_count == 2
        frame.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_helpers_reinjects_and_reresolves(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(return_value=None)
        frame.wait_for_load_state = AsyncMock()
        locator = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.evaluate = AsyncMock(
            side_effect=[
                PlaywrightError("Locator.evaluate: ReferenceError: isElementVisible is not defined"),
                True,
            ]
        )

        result = await SkyvernFrame(frame).get_element_visible(locator)

        assert result is True
        assert locator.evaluate.await_count == 2
        frame.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_locator_evaluation_instead_of_stale_handle_marshalling(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=PlaywrightError(
                "Page.evaluate: Protocol error (DOM.describeNode): Cannot find context with specified id"
            )
        )
        locator = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        locator.evaluate = AsyncMock(return_value=True)

        result = await SkyvernFrame(frame).get_element_visible(locator)

        assert result is True
        locator.evaluate.assert_awaited_once_with("(element) => isElementVisible(element) && !isHidden(element)")
        frame.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_false_when_locator_no_longer_resolves(self) -> None:
        frame = AsyncMock()
        locator = AsyncMock()
        locator.count = AsyncMock(return_value=0)

        result = await SkyvernFrame(frame).get_element_visible(locator)

        assert result is False
        locator.evaluate.assert_not_awaited()
        frame.evaluate.assert_not_awaited()
