from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TimeoutError as PWTimeoutError

from skyvern.config import settings
from skyvern.webeye.cdp_retry import _resolve_retry_budget, connect_over_cdp_with_retry


def _make_playwright(side_effect):
    pw = MagicMock()
    pw.chromium.connect_over_cdp = AsyncMock(side_effect=side_effect)
    return pw


def _set_budget(monkeypatch: pytest.MonkeyPatch, attempts: int, backoff: list[float]) -> None:
    monkeypatch.setattr(settings, "CDP_CONNECT_RETRY_ATTEMPTS", attempts)
    monkeypatch.setattr(settings, "CDP_CONNECT_RETRY_BACKOFF_SECONDS", list(backoff))


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
    async def test_all_attempts_fail_raises(self, monkeypatch):
        _set_budget(monkeypatch, attempts=3, backoff=[1, 3])
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
    async def test_final_attempt_raised_exception_does_not_leak_url_when_label_provided(self, monkeypatch):
        """When log_browser_address is set, the final-attempt exception is a plain
        RuntimeError whose message contains only the safe label and the original
        error class name — never the raw browser_address."""
        _set_budget(monkeypatch, attempts=3, backoff=[1, 3])
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
    async def test_final_attempt_unchanged_when_no_label(self, monkeypatch):
        """When log_browser_address is not provided, behavior is unchanged — the original
        Playwright exception (with its full URL) bubbles up as today."""
        _set_budget(monkeypatch, attempts=3, backoff=[1, 3])
        underlying_msg = "BrowserType.connect_over_cdp: connect ECONNREFUSED 10.0.0.1:9224"
        error = PWError(underlying_msg)
        pw = _make_playwright([error, error, error])

        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock):
            with pytest.raises(PWError) as excinfo:
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")

        assert str(excinfo.value) == underlying_msg

    @pytest.mark.asyncio
    async def test_backoff_is_bounded(self, monkeypatch):
        _set_budget(monkeypatch, attempts=3, backoff=[1, 3])
        error = PWError("connect ECONNRESET")
        pw = _make_playwright([error, error, error])
        sleep_values = []

        async def track_sleep(seconds):
            sleep_values.append(seconds)

        with patch("skyvern.webeye.cdp_retry._sleep", side_effect=track_sleep):
            with pytest.raises(PWError):
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224")
        assert sleep_values == [1, 3]

    @pytest.mark.asyncio
    async def test_backoff_schedule_clamps_to_last_entry(self, monkeypatch):
        """When attempts exceed the backoff schedule length, the last backoff value repeats."""
        _set_budget(monkeypatch, attempts=5, backoff=[1, 2])
        error = PWError("connect ECONNREFUSED 127.0.0.1:9222")
        pw = _make_playwright([error, error, error, error, error])
        sleep_values = []

        async def track_sleep(seconds):
            sleep_values.append(seconds)

        with patch("skyvern.webeye.cdp_retry._sleep", side_effect=track_sleep):
            with pytest.raises(PWError):
                await connect_over_cdp_with_retry(pw, "http://127.0.0.1:9222")
        assert sleep_values == [1, 2, 2, 2]


class TestRetryBudget:
    def test_default_budget_extends_total_wait_to_roughly_15s(self):
        """A slow-to-bind local CDP port gets ~15s of reconnect headroom by default."""
        attempts, backoff = _resolve_retry_budget()
        sleeps_between_attempts = [backoff[min(i, len(backoff) - 1)] for i in range(attempts - 1)]
        assert attempts >= 6
        assert sum(sleeps_between_attempts) >= 15

    def test_invalid_attempts_falls_back_to_settings_field_default(self, monkeypatch):
        """An invalid runtime override falls back to the settings field default (the robust
        ~15s budget), never to a smaller hardcoded value that could reintroduce slow-bind ECONNREFUSED."""
        monkeypatch.setattr(settings, "CDP_CONNECT_RETRY_ATTEMPTS", 0)
        attempts, _ = _resolve_retry_budget()
        assert attempts == 6

    def test_invalid_backoff_falls_back_to_settings_field_default(self, monkeypatch):
        monkeypatch.setattr(settings, "CDP_CONNECT_RETRY_BACKOFF_SECONDS", [])
        _, backoff = _resolve_retry_budget()
        assert backoff == (1, 2, 3, 4, 5)

    @pytest.mark.asyncio
    async def test_slow_to_bind_local_port_reconnects_on_a_later_attempt(self, monkeypatch):
        """Stealth Chromium that is slow to bind 127.0.0.1:9222 is reconnected once the
        port comes up on a later attempt, instead of surfacing ECONNREFUSED to the caller."""
        _set_budget(monkeypatch, attempts=6, backoff=[1, 2, 3, 4, 5])
        refused = PWError("BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9222")
        # Port binds on the 5th attempt (after 4 refusals while the browser cold-starts).
        pw = _make_playwright([refused, refused, refused, refused, "browser"])
        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(pw, "http://127.0.0.1:9222")
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 5
        assert [call.args[0] for call in mock_sleep.call_args_list] == [1, 2, 3, 4]
