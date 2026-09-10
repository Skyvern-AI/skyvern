from __future__ import annotations

import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from playwright._impl._errors import Error as PlaywrightError

from skyvern.exceptions import SkyvernPageAnalysisTimeout
from skyvern.webeye.utils.page import (
    _DOM_UTILS_MISSING_RESULT_KEY,
    JS_FUNCTION_DEFS,
    SkyvernFrame,
    _is_navigation_context_lost,
    _wait_for_navigation_settle,
    with_dom_utils,
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
        assert _is_navigation_context_lost("Page.evaluate: ReferenceError: scrollToXY is not defined") is False

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
    async def test_navigation_recovery_cannot_outlive_the_original_evaluate_deadline(self) -> None:
        """Recovery shares evaluate's deadline rather than minting four fresh attempts."""
        frame = AsyncMock()
        frame.evaluate = AsyncMock(side_effect=[_context_destroyed_error(), None, 42])

        async def settles_after_the_evaluate_budget(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(0.1)

        frame.wait_for_load_state = settles_after_the_evaluate_budget

        with pytest.raises(SkyvernPageAnalysisTimeout, match="Skyvern timed out trying to analyze the page"):
            await SkyvernFrame.evaluate(frame=frame, expression="() => 42", timeout_ms=50)

        # No re-injection or retry may start after the original evaluate budget has elapsed.
        assert frame.evaluate.await_count == 1

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
    @pytest.mark.parametrize("error_type", [PlaywrightError, RuntimeError])
    async def test_reference_error_propagates_without_recovery(self, error_type: type[Exception]) -> None:
        frame = AsyncMock()
        source_error = error_type("Page.evaluate: ReferenceError: pageOwnedMissingValue is not defined")
        frame.evaluate = AsyncMock(side_effect=source_error)
        frame.wait_for_load_state = AsyncMock()

        with pytest.raises(error_type) as exc_info:
            await SkyvernFrame.evaluate(frame=frame, expression="() => getScrollXY()", timeout_ms=30000)

        assert exc_info.value is source_error
        frame.evaluate.assert_awaited_once_with(expression="() => getScrollXY()", arg=None)
        frame.wait_for_load_state.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_bootstrap_injection_is_not_evaluated_twice_per_attempt(self) -> None:
        """SKY-13012: recovering the domUtils.js bootstrap must not re-inject it before retrying it."""
        frame = AsyncMock()
        frame.evaluate = AsyncMock(side_effect=[_context_destroyed_error(), "injected"])
        frame.wait_for_load_state = AsyncMock()

        result = await SkyvernFrame.evaluate(frame=frame, expression=JS_FUNCTION_DEFS, timeout_ms=30000)

        assert result == "injected"
        assert frame.evaluate.await_count == 2


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
    async def test_missing_helpers_propagates_without_recovery(self) -> None:
        frame = AsyncMock()
        frame.evaluate = AsyncMock(return_value=None)
        frame.wait_for_load_state = AsyncMock()
        locator = AsyncMock()
        locator.count = AsyncMock(return_value=1)
        source_error = PlaywrightError("Locator.evaluate: ReferenceError: isElementVisible is not defined")
        locator.evaluate = AsyncMock(side_effect=source_error)

        with pytest.raises(PlaywrightError) as exc_info:
            await SkyvernFrame(frame).get_element_visible(locator)

        assert exc_info.value is source_error
        locator.evaluate.assert_awaited_once_with(
            with_dom_utils(
                "(element) => isElementVisible(element) && !isHidden(element)", ("isElementVisible", "isHidden")
            )
        )
        frame.evaluate.assert_not_awaited()
        frame.wait_for_load_state.assert_not_awaited()

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
        locator.evaluate.assert_awaited_once_with(
            with_dom_utils(
                "(element) => isElementVisible(element) && !isHidden(element)", ("isElementVisible", "isHidden")
            )
        )
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures",
    [
        [asyncio.TimeoutError()],
        [_context_destroyed_error(), asyncio.TimeoutError()],
        [_context_destroyed_error(), None, asyncio.TimeoutError()],
    ],
)
async def test_large_helper_expression_is_bounded_in_timeout_logs(failures, monkeypatch):
    from skyvern.webeye.utils import page as page_utils

    warning = MagicMock()
    monkeypatch.setattr(page_utils.LOG, "warning", warning)
    frame = AsyncMock()
    frame.evaluate.side_effect = failures
    expression = "x" * 135000
    with pytest.raises(SkyvernPageAnalysisTimeout):
        await SkyvernFrame.evaluate(frame=frame, expression=expression)
    logged = [call.kwargs["expression"] for call in warning.call_args_list if "expression" in call.kwargs]
    assert logged
    assert all(value == expression[:200] for value in logged)


@pytest.mark.asyncio
async def test_builtin_operation_ships_bundle_only_after_guard_reports_missing_helpers() -> None:
    frame = AsyncMock()
    frame.evaluate.side_effect = [
        {_DOM_UTILS_MISSING_RESULT_KEY: True},
        None,
        [11, 22],
    ]

    assert await SkyvernFrame(frame).get_scroll_x_y() == [11, 22]

    calls = frame.evaluate.await_args_list
    guarded_expression = calls[0].kwargs["expression"]
    assert JS_FUNCTION_DEFS not in guarded_expression
    assert calls[1].kwargs == {"expression": JS_FUNCTION_DEFS, "arg": None}
    assert calls[2].kwargs["expression"] == guarded_expression


@pytest.mark.asyncio
async def test_builtin_operation_warm_path_uses_one_small_dispatch() -> None:
    frame = AsyncMock()
    frame.evaluate.return_value = [11, 22]

    assert await SkyvernFrame(frame).get_scroll_x_y() == [11, 22]

    frame.evaluate.assert_awaited_once()
    assert JS_FUNCTION_DEFS not in frame.evaluate.await_args.kwargs["expression"]


@pytest.mark.asyncio
async def test_cold_locator_bootstraps_through_locator_evaluation() -> None:
    frame = AsyncMock()
    locator = AsyncMock()
    locator.count.return_value = 1
    locator.evaluate.side_effect = [
        {_DOM_UTILS_MISSING_RESULT_KEY: True},
        None,
        True,
    ]

    assert await SkyvernFrame(frame).get_element_visible(locator) is True

    assert locator.evaluate.await_count == 3
    assert JS_FUNCTION_DEFS not in locator.evaluate.await_args_list[0].args[0]
    assert JS_FUNCTION_DEFS in locator.evaluate.await_args_list[1].args[0]
    assert locator.evaluate.await_args_list[2].args[0] == locator.evaluate.await_args_list[0].args[0]
    frame.evaluate.assert_not_awaited()
