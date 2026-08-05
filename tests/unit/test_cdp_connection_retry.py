from __future__ import annotations

import socket
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TimeoutError as PWTimeoutError

from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.browser_errors import BrowserCdpConnectionError
from skyvern.webeye.cdp_retry import _resolve_retry_budget, connect_over_cdp_with_retry, is_cdp_connection_error


class _FakeEngineError(Exception):
    pass


class _FakeEngineRetryable(_FakeEngineError):
    pass


def _fake_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="fake",
        start_driver=cast(Any, lambda: None),
        error_type=_FakeEngineError,
        timeout_error_type=_FakeEngineError,
        metadata=BrowserEngineMetadata(name="fake"),
        selection_reason="test-fake",
        retryable_error_types=(_FakeEngineRetryable,),
    )


def _make_playwright(side_effect):
    pw = MagicMock()
    pw.chromium.connect_over_cdp = AsyncMock(side_effect=side_effect)
    return pw


def _set_budget(monkeypatch: pytest.MonkeyPatch, attempts: int, backoff: list[float]) -> None:
    monkeypatch.setattr(settings, "CDP_CONNECT_RETRY_ATTEMPTS", attempts)
    monkeypatch.setattr(settings, "CDP_CONNECT_RETRY_BACKOFF_SECONDS", list(backoff))


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_validates_browser_address_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ENV", "prod")
        pw = _make_playwright(["browser"])

        with pytest.raises(BlockedHost):
            await connect_over_cdp_with_retry(pw, "http://10.0.0.5:9224")

        pw.chromium.connect_over_cdp.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_resolution_failure_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_budget(monkeypatch, attempts=3, backoff=[1, 2])
        pw = _make_playwright(["browser"])
        resolver = MagicMock(
            side_effect=[
                OSError("synthetic resolver failure"),
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))],
            ]
        )
        monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolver)

        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(pw, "wss://cdp.example.test")

        assert result == "browser"
        assert resolver.call_count == 2
        pw.chromium.connect_over_cdp.assert_awaited_once()
        mock_sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_blocked_host_fails_closed_without_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pw = _make_playwright(["browser"])
        resolver = MagicMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))])
        monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolver)

        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(BlockedHost) as excinfo:
                await connect_over_cdp_with_retry(pw, "wss://blocked.example.test")

        assert type(excinfo.value) is BlockedHost
        resolver.assert_called_once_with("blocked.example.test", None, type=socket.SOCK_STREAM)
        pw.chromium.connect_over_cdp.assert_not_awaited()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        pw = _make_playwright(["browser"])
        result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)
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
            result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)
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
                validate_browser_address=False,
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
            result = await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)
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
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)
        assert pw.chromium.connect_over_cdp.call_count == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self):
        pw = _make_playwright([PWError("net::ERR_NAME_NOT_RESOLVED")])
        with pytest.raises(PWError):
            await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)
        assert pw.chromium.connect_over_cdp.call_count == 1

    @pytest.mark.asyncio
    async def test_final_attempt_safe_error_is_classified_without_leaking_url(self, monkeypatch):
        _set_budget(monkeypatch, attempts=3, backoff=[1, 3])
        synthetic_key = "SYNTHETIC_KEY_DO_NOT_USE"
        raw_address = f"wss://cdp.example.test/devtools/browser/SYNTHETIC_SESSION?api_key={synthetic_key}"
        error = _FakeEngineRetryable(f"connect failed: {raw_address}")
        pw = _make_playwright([error, error, error])
        selection = _fake_selection()

        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock):
            with pytest.raises(BrowserCdpConnectionError) as excinfo:
                await connect_over_cdp_with_retry(
                    pw,
                    raw_address,
                    log_browser_address="remote-cdp-vendor:cdp.example.test",
                    selection=selection,
                    validate_browser_address=False,
                )

        message = str(excinfo.value)
        assert raw_address not in message
        assert synthetic_key not in message
        assert "SYNTHETIC_SESSION" not in message
        assert "/devtools/browser/" not in message
        assert "remote-cdp-vendor:cdp.example.test" in message
        assert "_FakeEngineRetryable" in message
        assert is_cdp_connection_error(excinfo.value)
        assert is_cdp_connection_error(excinfo.value, selection)
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
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)

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
                await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)
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
    async def test_selected_engine_retryable_error_is_retried(self, monkeypatch):
        """A selected engine's own retryable-CDP class (foreign to Playwright) is recognized via the
        selection and retried, then recovered — proving the retry loop forwards ``selection`` to the
        classifier instead of relying on Playwright's hardcoded identities."""
        _set_budget(monkeypatch, attempts=3, backoff=[1, 2])
        pw = _make_playwright([_FakeEngineRetryable("transient CDP disconnect"), "browser"])
        with patch("skyvern.webeye.cdp_retry._sleep", new_callable=AsyncMock) as mock_sleep:
            result = await connect_over_cdp_with_retry(
                pw,
                "http://10.0.0.1:9224",
                selection=_fake_selection(),
                validate_browser_address=False,
            )
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_selected_engine_foreign_error_is_not_retried(self):
        """Under a non-stock selection a foreign Playwright error is not a recognized connection
        error, so it is raised immediately (never swallowed by a blind retry)."""
        pw = _make_playwright([PWTimeoutError("Timeout 30000ms exceeded.")])
        with pytest.raises(PWTimeoutError):
            await connect_over_cdp_with_retry(
                pw,
                "http://10.0.0.1:9224",
                selection=_fake_selection(),
                validate_browser_address=False,
            )
        assert pw.chromium.connect_over_cdp.call_count == 1

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
