from __future__ import annotations

import pytest
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TargetClosedError as PWTargetClosedError
from playwright._impl._errors import TimeoutError as PWTimeoutError

from skyvern.webeye.cdp_retry import _is_cdp_connection_error


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
        assert _is_cdp_connection_error(exc), f"Expected connection error: {exc!r}"

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
        assert not _is_cdp_connection_error(exc), f"Expected app error NOT to match: {exc!r}"
