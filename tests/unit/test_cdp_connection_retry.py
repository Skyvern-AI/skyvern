from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TimeoutError as PWTimeoutError

from skyvern.webeye.cdp_retry import connect_over_cdp_with_retry


def _make_playwright(side_effect):
    pw = MagicMock()
    pw.chromium.connect_over_cdp = AsyncMock(side_effect=side_effect)
    return pw


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        pw = _make_playwright(["browser"])
        result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 1

    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failure(self):
        pw = _make_playwright(
            [
                PWError("BrowserType.connect_over_cdp: connect ECONNREFUSED 10.0.0.1:9224"),
                "browser",
            ]
        )
        with patch("skyvern.webeye.cdp_retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_succeeds_after_two_transient_failures(self):
        pw = _make_playwright(
            [
                PWError("connect ECONNREFUSED"),
                PWTimeoutError("Timeout 30000ms exceeded."),
                "browser",
            ]
        )
        with patch("skyvern.webeye.cdp_retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_all_attempts_fail_raises(self):
        error = PWError("connect ECONNREFUSED 10.0.0.1:9224")
        pw = _make_playwright([error, error, error])
        with patch("skyvern.webeye.cdp_retry.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(PWError):
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert pw.chromium.connect_over_cdp.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self):
        pw = _make_playwright([PWError("net::ERR_NAME_NOT_RESOLVED")])
        with pytest.raises(PWError):
            await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert pw.chromium.connect_over_cdp.call_count == 1

    @pytest.mark.asyncio
    async def test_backoff_is_bounded(self):
        error = PWError("connect ECONNRESET")
        pw = _make_playwright([error, error, error])
        sleep_values = []

        async def track_sleep(seconds):
            sleep_values.append(seconds)

        with patch("skyvern.webeye.cdp_retry.asyncio.sleep", side_effect=track_sleep):
            with pytest.raises(PWError):
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert sleep_values == [1, 3]
