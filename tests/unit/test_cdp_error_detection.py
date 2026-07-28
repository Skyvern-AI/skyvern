from __future__ import annotations

from typing import Any, cast

import pytest
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError

from skyvern.webeye.browser_engine import (
    REGISTRY,
    STOCK_ENGINE_NAME,
    BrowserEngineMetadata,
    BrowserEngineSelection,
)
from skyvern.webeye.cdp_retry import is_cdp_connection_error


class _FakeEngineError(Exception):
    """Base native error for a hypothetical non-stock engine (foreign to Playwright)."""


class _FakeEngineTimeout(_FakeEngineError):
    pass


class _FakeEngineTargetClosed(_FakeEngineError):
    pass


class _FakeEngineCdpConnection(_FakeEngineError):
    pass


class _FakeEngineRetryable(_FakeEngineCdpConnection):
    pass


def _stock_selection() -> BrowserEngineSelection:
    return REGISTRY.get(STOCK_ENGINE_NAME).select(selection_reason="test-stock")


def _rich_fake_selection() -> BrowserEngineSelection:
    """A non-stock selection that binds the richer CDP/target-closed/retryable families #14003 added."""
    return BrowserEngineSelection(
        name="fake",
        start_driver=cast(Any, lambda: None),
        error_type=_FakeEngineError,
        timeout_error_type=_FakeEngineTimeout,
        metadata=BrowserEngineMetadata(name="fake"),
        selection_reason="test-fake",
        target_closed_error_types=(_FakeEngineTargetClosed,),
        cdp_connection_error_types=(_FakeEngineCdpConnection,),
        retryable_error_types=(_FakeEngineRetryable,),
    )


class TestConnectionErrorDetection:
    @pytest.mark.parametrize(
        "exc",
        [
            PWTimeoutError("Timeout 30000ms exceeded."),
            PWTimeoutError("Timeout 120000ms exceeded."),
            PWTargetClosedError("Target page, context or browser has been closed"),
            ConnectionRefusedError("connect ECONNREFUSED 10.0.36.234:9224"),
            ConnectionResetError("connect ECONNRESET"),
            PWError("BrowserType.connect_over_cdp: connect ECONNREFUSED 10.0.36.234:9224"),
            PWError("Browser closed."),
        ],
        ids=lambda e: type(e).__name__ + ": " + str(e)[:50],
    )
    def test_connection_errors_detected(self, exc: Exception):
        assert is_cdp_connection_error(exc), f"Expected connection error: {exc!r}"

    @pytest.mark.parametrize(
        "exc",
        [
            PWError("Navigation timeout exceeded"),
            ValueError("Element not found"),
            RuntimeError("LLM response parsing failed"),
            PWError("net::ERR_NAME_NOT_RESOLVED"),
            PWError("Page crashed"),
        ],
        ids=lambda e: type(e).__name__ + ": " + str(e)[:50],
    )
    def test_app_errors_not_detected(self, exc: Exception):
        assert not is_cdp_connection_error(exc), f"Expected app error NOT to match: {exc!r}"


class TestSelectionAwareDetection:
    """A ``selection`` keys the retry decision off the run's selected-engine error families instead
    of stock Playwright's hardcoded classes, while an absent/None selection is byte-for-byte the
    stock path and the engine-neutral transport floor applies under both."""

    _STOCK_CASES = [
        PWTimeoutError("Timeout 30000ms exceeded."),
        PWTargetClosedError("Target page, context or browser has been closed"),
        ConnectionRefusedError("connect ECONNREFUSED 10.0.36.234:9224"),
        ConnectionResetError("connect ECONNRESET"),
        PWError("BrowserType.connect_over_cdp: connect ECONNREFUSED 10.0.36.234:9224"),
        PWError("Browser closed."),
        PWError("net::ERR_NAME_NOT_RESOLVED"),
        PWError("Navigation timeout exceeded"),
        ValueError("Element not found"),
    ]

    @pytest.mark.parametrize("exc", _STOCK_CASES, ids=lambda e: type(e).__name__ + ": " + str(e)[:40])
    def test_stock_selection_matches_no_selection(self, exc: Exception) -> None:
        # Passing the resolved stock-Playwright selection must never diverge from the legacy None path.
        assert is_cdp_connection_error(exc, _stock_selection()) == is_cdp_connection_error(exc)

    def test_selected_engine_native_families_are_retryable(self) -> None:
        selection = _rich_fake_selection()
        for exc in (
            _FakeEngineRetryable("transient disconnect"),
            _FakeEngineCdpConnection("cdp transport failure"),
            _FakeEngineTargetClosed("target closed"),
            _FakeEngineTimeout("deadline exceeded"),
        ):
            assert is_cdp_connection_error(exc, selection), f"Expected retryable for {exc!r}"

    def test_selected_engine_generic_base_error_is_not_retried(self) -> None:
        # A recognized-but-generic engine error (no transport signal) is not a connection error.
        assert not is_cdp_connection_error(_FakeEngineError("element detached"), _rich_fake_selection())

    def test_selected_engine_base_error_with_transport_substring_is_retried(self) -> None:
        assert is_cdp_connection_error(_FakeEngineError("connect ECONNREFUSED 10.0.0.1:9222"), _rich_fake_selection())

    def test_foreign_playwright_error_is_isolated_under_non_stock_selection(self) -> None:
        # A stock-Playwright error reaching a non-stock run must NOT be swallowed as retryable.
        selection = _rich_fake_selection()
        assert not is_cdp_connection_error(PWTimeoutError("Timeout 30000ms exceeded."), selection)
        assert not is_cdp_connection_error(PWTargetClosedError("Target closed"), selection)

    def test_stdlib_transport_errors_retry_regardless_of_selection(self) -> None:
        selection = _rich_fake_selection()
        for exc in (ConnectionRefusedError("ECONNREFUSED"), ConnectionResetError("ECONNRESET"), TimeoutError("late")):
            assert is_cdp_connection_error(exc, selection), f"Expected transport floor for {exc!r}"
