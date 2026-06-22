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
        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_retry_logs_use_redacted_browser_address_when_provided(self):
        secret_address = "wss://cdp.vendor.test/devtools/browser/SECRET?token=ABC"
        pw = _make_playwright(
            [
                PWError("BrowserType.connect_over_cdp: connect ECONNREFUSED cdp.vendor.test"),
                "browser",
            ]
        )
        with (
            patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock),
            patch("skyvern.webeye.cdp_retry.LOG") as mock_log,
        ):
            result = await connect_over_cdp_with_retry(
                pw,
                secret_address,
                log_browser_address="remote-cdp-vendor:cdp.vendor.test",
            )
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_args.args[0] == secret_address
        warning_values = [str(v) for call in mock_log.warning.call_args_list for v in call.kwargs.values()]
        info_values = [str(v) for call in mock_log.info.call_args_list for v in call.kwargs.values()]
        logged = " ".join(warning_values + info_values)
        assert "remote-cdp-vendor:cdp.vendor.test" in logged
        assert "SECRET" not in logged
        assert "token=ABC" not in logged

    @pytest.mark.asyncio
    async def test_succeeds_after_two_transient_failures(self):
        pw = _make_playwright(
            [
                PWError("connect ECONNREFUSED"),
                PWTimeoutError("Timeout 30000ms exceeded."),
                "browser",
            ]
        )
        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 3
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_all_attempts_fail_raises(self):
        error = PWError("connect ECONNREFUSED 10.0.0.1:9224")
        pw = _make_playwright([error, error, error])
        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock):
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
    async def test_final_attempt_raised_exception_does_not_leak_url_when_label_provided(self):
        """When log_browser_address is set, the final-attempt exception is a plain
        RuntimeError whose message contains only the safe label and the original
        error class name — never the raw browser_address."""
        secret_address = "wss://cdp.vendor.test/devtools/browser/SECRET?token=ABC"
        underlying_msg = (
            "BrowserType.connect_over_cdp: connect ECONNREFUSED wss://cdp.vendor.test/devtools/browser/SECRET?token=ABC"
        )
        error = PWError(underlying_msg)
        pw = _make_playwright([error, error, error])

        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError) as excinfo:
                await connect_over_cdp_with_retry(
                    pw,
                    secret_address,
                    log_browser_address="remote-cdp-vendor:cdp.vendor.test",
                )

        message = str(excinfo.value)
        assert "SECRET" not in message
        assert "token=ABC" not in message
        assert "/devtools/browser/" not in message
        assert "remote-cdp-vendor:cdp.vendor.test" in message
        # Original error class name preserved for log debuggability.
        assert "PWError" in message or "Error" in message
        # Chain suppressed so __cause__ cannot leak the URL either.
        assert excinfo.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_final_attempt_unchanged_when_no_label(self):
        """When log_browser_address is not provided, behavior is unchanged — the original
        Playwright exception (with its full URL) bubbles up as today."""
        underlying_msg = "BrowserType.connect_over_cdp: connect ECONNREFUSED 10.0.0.1:9224"
        error = PWError(underlying_msg)
        pw = _make_playwright([error, error, error])

        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock):
            with pytest.raises(PWError) as excinfo:
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")

        assert str(excinfo.value) == underlying_msg

    @pytest.mark.asyncio
    async def test_backoff_is_bounded(self):
        error = PWError("connect ECONNRESET")
        pw = _make_playwright([error, error, error])
        sleep_values = []

        async def track_sleep(seconds):
            sleep_values.append(seconds)

        with patch("skyvern.webeye.cdp_retry._sleep", side_effect=track_sleep):
            with pytest.raises(PWError):
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert sleep_values == [1, 3]
