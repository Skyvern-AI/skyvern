from __future__ import annotations

import asyncio
import errno
import socket
import traceback
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit, urlunsplit

import pytest
import websockets
from playwright._impl._errors import Error as PWError
from playwright._impl._errors import TimeoutError as PWTimeoutError
from websockets.exceptions import InvalidMessage, InvalidStatus

from skyvern.config import settings
from skyvern.exceptions import BlockedHost
from skyvern.webeye import browser_acquisition_sample as sample_mod
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.browser_errors import BrowserCdpAcquisitionError, BrowserCdpConnectionError
from skyvern.webeye.cdp_retry import (
    _connect_over_relay,
    _is_retryable_pinned_connect_error,
    _PinnedCdpRelay,
    _resolve_retry_budget,
    connect_over_cdp_with_retry,
    is_cdp_connection_error,
)


class _FakeEngineError(Exception):
    pass


class _FakeEngineRetryable(_FakeEngineError):
    pass


class _FakeEngineTimeout(_FakeEngineError):
    pass


def _fake_selection() -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name="fake",
        start_driver=cast(Any, lambda: None),
        error_type=_FakeEngineError,
        timeout_error_type=_FakeEngineTimeout,
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
    async def test_cancellation_is_not_wrapped_as_acquisition_failure(self) -> None:
        pw = _make_playwright(asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await connect_over_cdp_with_retry(
                pw,
                "wss://cdp.example.test",
                validate_browser_address=False,
                retry=False,
            )

        pw.chromium.connect_over_cdp.assert_awaited_once()

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
        resolutions: list[tuple[str, int | None, int]] = []
        resolution_results: list[OSError | list[tuple[int, int, int, str, tuple[str, int]]]] = [
            OSError("synthetic resolver failure"),
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.4.4", 0)),
            ],
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.0.0.1", 0)),
            ],
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("9.9.9.9", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("149.112.112.112", 0)),
            ],
        ]
        discoveries: list[tuple[str, float, tuple[str, ...] | None, dict[str, str] | None]] = []
        pinned_dials: list[tuple[str, dict[str, Any]]] = []
        dials: list[tuple[str, dict[str, str] | None]] = []
        dial_timeouts: list[float | None] = []
        sleeps: list[float] = []

        def resolver(host: str, port: int | None, *, type: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            resolutions.append((host, port, type))
            result = resolution_results.pop(0)
            if isinstance(result, OSError):
                raise result
            return result

        async def resolve_websocket_url(
            endpoint_url: str,
            *,
            timeout_ms: float,
            resolved_ips: tuple[str, ...] | None = None,
            headers: dict[str, str] | None = None,
        ) -> str:
            discoveries.append((endpoint_url, timeout_ms, resolved_ips, headers))
            suffix = len(discoveries)
            return f"wss://socket-{suffix}.example.test/devtools/browser/fake-{suffix}"

        async def connect_over_cdp(
            address: str, *, headers: dict[str, str] | None = None, timeout: float | None = None
        ) -> str:
            dials.append((address, headers))
            dial_timeouts.append(timeout)
            if len(dials) == 1:
                raise PWTimeoutError("synthetic candidate timeout")
            return "browser"

        async def open_pinned(uri: str, **kwargs: Any) -> Any:
            pinned_dials.append((uri, kwargs))
            return object()

        async def connect_over_relay(
            playwright: Any,
            upstream: Any,
            *,
            timeout_ms: float,
        ) -> str:
            return await playwright.chromium.connect_over_cdp("ws://127.0.0.1/relay", timeout=timeout_ms)

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        pw = MagicMock()
        pw.chromium.connect_over_cdp = connect_over_cdp
        monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolver)
        monkeypatch.setattr("skyvern.webeye.cdp_retry.resolve_websocket_url", resolve_websocket_url, raising=False)
        monkeypatch.setattr("skyvern.webeye.cdp_retry.open_first_pinned_websocket", open_pinned)
        monkeypatch.setattr("skyvern.webeye.cdp_retry._connect_over_relay", connect_over_relay)
        monkeypatch.setattr("skyvern.webeye.cdp_retry._sleep", sleep)

        result = await connect_over_cdp_with_retry(
            pw,
            "https://cdp.example.test",
            headers={"Host": "localhost", "X-Test": "value"},
        )

        assert result == "browser"
        assert resolutions == [
            ("cdp.example.test", None, socket.SOCK_STREAM),
            ("cdp.example.test", None, socket.SOCK_STREAM),
            ("socket-1.example.test", None, socket.SOCK_STREAM),
            ("socket-2.example.test", None, socket.SOCK_STREAM),
        ]
        assert [
            (endpoint, resolved_ips, connect_headers) for endpoint, _, resolved_ips, connect_headers in discoveries
        ] == [
            (
                "https://cdp.example.test",
                ("8.8.8.8", "8.8.4.4"),
                {"Host": "localhost", "X-Test": "value"},
            ),
            (
                "https://cdp.example.test",
                ("8.8.8.8", "8.8.4.4"),
                {"Host": "localhost", "X-Test": "value"},
            ),
        ]
        assert all(30_000 < timeout_ms <= 180_000 for _, timeout_ms, _, _ in discoveries)
        assert [(uri, kwargs["connect_hosts"]) for uri, kwargs in pinned_dials] == [
            ("wss://socket-1.example.test/devtools/browser/fake-1", ("1.1.1.1", "1.0.0.1")),
            ("wss://socket-2.example.test/devtools/browser/fake-2", ("149.112.112.112", "9.9.9.9")),
        ]
        assert all(
            kwargs["server_hostname"] == f"socket-{index}.example.test"
            for index, (_, kwargs) in enumerate(pinned_dials, 1)
        )
        assert all(kwargs["headers"] == {"Host": "localhost", "X-Test": "value"} for _, kwargs in pinned_dials)
        assert dials == [("ws://127.0.0.1/relay", None)] * 2
        assert all(timeout is not None and 30_000 < timeout <= 180_000 for timeout in dial_timeouts)
        assert sleeps == [1, 2]

        direct_resolutions = [
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.4.4", 0)),
            ]
        ]
        direct_dials: list[tuple[str, dict[str, str] | None, float | None]] = []

        def direct_resolver(
            host: str, port: int | None, *, type: int
        ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            assert (host, port, type) == ("direct.example.test", None, socket.SOCK_STREAM)
            return direct_resolutions.pop(0)

        async def direct_websocket_url(
            endpoint_url: str,
            *,
            timeout_ms: float,
            resolved_ips: tuple[str, ...] | None = None,
            headers: dict[str, str] | None = None,
        ) -> str:
            return endpoint_url

        async def direct_connect(
            address: str, *, headers: dict[str, str] | None = None, timeout: float | None = None
        ) -> str:
            direct_dials.append((address, headers, timeout))
            if len(direct_dials) < 2:
                raise PWTimeoutError("synthetic candidate timeout")
            return "direct-browser"

        _set_budget(monkeypatch, attempts=2, backoff=[0])
        monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", direct_resolver)
        monkeypatch.setattr("skyvern.webeye.cdp_retry.resolve_websocket_url", direct_websocket_url)
        pw.chromium.connect_over_cdp = direct_connect

        direct_result = await connect_over_cdp_with_retry(pw, "wss://direct.example.test/devtools/browser/fake")

        assert direct_result == "direct-browser"
        assert [(address, headers) for address, headers, _ in direct_dials] == [
            ("ws://127.0.0.1/relay", None),
            ("ws://127.0.0.1/relay", None),
        ]
        assert [(uri, kwargs["connect_hosts"]) for uri, kwargs in pinned_dials[-2:]] == [
            ("wss://direct.example.test/devtools/browser/fake", ("8.8.8.8", "8.8.4.4")),
            ("wss://direct.example.test/devtools/browser/fake", ("8.8.4.4", "8.8.8.8")),
        ]
        assert all(timeout is not None and 30_000 < timeout <= 180_000 for _, _, timeout in direct_dials)

        address_connects = 0

        async def address_open(*args: Any, **kwargs: Any) -> Any:
            nonlocal address_connects
            address_connects += 1
            if address_connects == 1:
                raise OSError(errno.ENETUNREACH, "synthetic unreachable address")
            return object()

        _set_budget(monkeypatch, attempts=2, backoff=[0])
        monkeypatch.setattr(
            "skyvern.webeye.cdp_retry._validate_browser_address_host",
            AsyncMock(return_value=("192.0.2.1", "192.0.2.2")),
        )
        monkeypatch.setattr("skyvern.webeye.cdp_retry.open_first_pinned_websocket", address_open)
        pw.chromium.connect_over_cdp = AsyncMock(return_value="address-browser")

        address_result = await connect_over_cdp_with_retry(
            pw,
            "wss://address-error.example.test/devtools/browser/fake",
        )

        assert address_result == "address-browser"
        assert address_connects == 2
        assert not _is_retryable_pinned_connect_error(OSError(errno.EADDRNOTAVAIL, "synthetic local failure"))
        assert not _is_retryable_pinned_connect_error(OSError(errno.EAFNOSUPPORT, "synthetic local failure"))

        handshake_connects = 0

        async def handshake_open(*args: Any, **kwargs: Any) -> Any:
            nonlocal handshake_connects
            handshake_connects += 1
            if handshake_connects == 1:
                try:
                    raise EOFError("synthetic handshake EOF")
                except EOFError as cause:
                    raise InvalidMessage("synthetic invalid handshake") from cause
            return object()

        monkeypatch.setattr("skyvern.webeye.cdp_retry.open_first_pinned_websocket", handshake_open)
        pw.chromium.connect_over_cdp = AsyncMock(return_value="handshake-browser")

        handshake_result = await connect_over_cdp_with_retry(
            pw,
            "wss://handshake-error.example.test/devtools/browser/fake",
            selection=_fake_selection(),
        )

        assert handshake_result == "handshake-browser"
        assert handshake_connects == 2

        async def echo(connection: websockets.ServerConnection) -> None:
            async for message in connection:
                await connection.send(message)

        async with websockets.serve(echo, "127.0.0.1", 0) as upstream_server:
            upstream_port = upstream_server.sockets[0].getsockname()[1]
            upstream = await websockets.connect(f"ws://127.0.0.1:{upstream_port}")
            relay = _PinnedCdpRelay(upstream)
            relay_url = await relay.start()
            relay_parts = urlsplit(relay_url)
            wrong_path = urlunsplit((relay_parts.scheme, relay_parts.netloc, "/wrong", "", ""))

            assert relay_parts.path not in ("", "/")
            with pytest.raises(InvalidStatus):
                await websockets.connect(wrong_path)
            with pytest.raises(InvalidStatus):
                await websockets.connect(relay_url, origin="http://attacker.example")

            accepted = await websockets.connect(relay_url)
            await accepted.send("relay-ready")
            assert await accepted.recv() == "relay-ready"
            with pytest.raises(InvalidStatus):
                await websockets.connect(relay_url)

            await accepted.close()
            await relay.close()

        class StartupFailureUpstream:
            def __init__(self) -> None:
                self.closed = False

            async def close(self) -> None:
                self.closed = True

        async def fail_relay_start(*args: Any, **kwargs: Any) -> None:
            raise OSError("synthetic relay bind failure")

        startup_failure_upstream = StartupFailureUpstream()
        monkeypatch.setattr("skyvern.webeye.cdp_retry.websockets.serve", fail_relay_start)
        with pytest.raises(OSError, match="synthetic relay bind failure"):
            await _connect_over_relay(MagicMock(), cast(Any, startup_failure_upstream), timeout_ms=1_000)
        assert startup_failure_upstream.closed

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

        late_attach: asyncio.Task[None] | None = None
        late_attached = False

        async def upstream(connection: websockets.ServerConnection) -> None:
            await connection.wait_closed()

        async with websockets.serve(upstream, "127.0.0.1", 0) as upstream_server:
            upstream_port = upstream_server.sockets[0].getsockname()[1]

            async def delayed_attach(address: str) -> None:
                nonlocal late_attached
                await asyncio.sleep(0.1)
                try:
                    connection = await websockets.connect(address)
                except OSError:
                    return
                late_attached = True
                await connection.close()

            async def ignores_cancellation(
                address: str, *, headers: dict[str, str] | None = None, timeout: float | None = None
            ) -> str:
                nonlocal late_attach
                assert address.startswith("ws://127.0.0.1:")
                assert timeout is not None and 0 < timeout < 50
                late_attach = asyncio.create_task(delayed_attach(address))
                await asyncio.Event().wait()
                return "unreachable"

            pw.chromium.connect_over_cdp = ignores_cancellation
            monkeypatch.setattr(
                "skyvern.webeye.cdp_retry._validate_browser_address_host",
                AsyncMock(return_value=("127.0.0.1",)),
            )
            monkeypatch.setattr(
                "skyvern.webeye.cdp_retry.DEFAULT_PLAYWRIGHT_LAUNCH_TIMEOUT_IN_MILLISECONDS",
                50,
            )
            with pytest.raises(TimeoutError):
                await connect_over_cdp_with_retry(
                    pw,
                    f"ws://late.example.test:{upstream_port}/devtools/browser/fake",
                    retry=False,
                )
            assert late_attach is not None
            await late_attach

        assert not late_attached

    @pytest.mark.asyncio
    async def test_native_non_retryable_connect_failure_is_an_acquisition_fact(self):
        raw_address = "wss://secret@cdp.example.test/devtools/browser/session?token=hidden"
        native_error = PWError(f"connect failed at {raw_address}; authorization: bare-secret")
        pw = _make_playwright([native_error])

        with pytest.raises(BrowserCdpAcquisitionError) as excinfo:
            await connect_over_cdp_with_retry(pw, "http://10.0.0.1:9224", validate_browser_address=False)

        assert pw.chromium.connect_over_cdp.call_count == 1
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True
        assert "secret" not in str(excinfo.value)
        assert "token=hidden" not in str(excinfo.value)
        assert "cdp.example.test" not in str(excinfo.value)
        assert "bare-secret" not in str(excinfo.value)
        assert str(excinfo.value) == "Browser driver failed during CDP connect to [remote browser endpoint]."
        rendered = "".join(traceback.format_exception(excinfo.value))
        assert "bare-secret" not in rendered
        assert "token=hidden" not in rendered
        assert "cdp.example.test" not in rendered
        assert not is_cdp_connection_error(excinfo.value)

    @pytest.mark.asyncio
    async def test_selected_engine_native_connect_failure_is_an_acquisition_fact(self):
        native_error = _FakeEngineError("selected engine connect failed")
        pw = _make_playwright([native_error])

        with pytest.raises(BrowserCdpAcquisitionError) as excinfo:
            await connect_over_cdp_with_retry(
                pw,
                "http://10.0.0.1:9224",
                selection=_fake_selection(),
                validate_browser_address=False,
            )

        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True
        assert pw.chromium.connect_over_cdp.call_count == 1

    @pytest.mark.asyncio
    async def test_selected_engine_does_not_claim_a_foreign_native_connect_failure(self):
        native_error = PWError("foreign driver connect failed")
        pw = _make_playwright([native_error])

        with pytest.raises(PWError) as excinfo:
            await connect_over_cdp_with_retry(
                pw,
                "http://10.0.0.1:9224",
                selection=_fake_selection(),
                validate_browser_address=False,
            )

        assert excinfo.value is native_error
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
                await connect_over_cdp_with_retry(pw, "http://127.0.0.1:9222", validate_browser_address=False)
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
            result = await connect_over_cdp_with_retry(
                pw,
                "http://127.0.0.1:9222",
                validate_browser_address=False,
            )
        assert result == "browser"
        assert pw.chromium.connect_over_cdp.call_count == 5
        assert [call.args[0] for call in mock_sleep.call_args_list] == [1, 2, 3, 4]


class TestFirstTryAcquisitionRecording:
    """connect_over_cdp_with_retry records CDP-connect attempts into an open acquisition sample so
    a downstream first-try ratio distinguishes a clean connect from a retried one; no-op otherwise."""

    @pytest.mark.asyncio
    async def test_records_successful_attempt_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_budget(monkeypatch, attempts=6, backoff=[0.0])
        monkeypatch.setattr(sample_mod, "_current_sample", sample_mod.contextvars.ContextVar("t", default=None))
        monkeypatch.setattr("skyvern.webeye.cdp_retry._sleep", AsyncMock())
        pw = _make_playwright([ConnectionRefusedError("refused"), ConnectionRefusedError("refused"), MagicMock()])
        with sample_mod.browser_acquisition_scope("create"):
            await connect_over_cdp_with_retry(pw, "wss://cdp.example.test", validate_browser_address=False)
            sample = sample_mod.current_browser_acquisition_sample()
            assert sample is not None and sample.cdp_connect_attempts == 3

    @pytest.mark.asyncio
    async def test_cancellation_mid_retry_preserves_attempt_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_budget(monkeypatch, attempts=6, backoff=[0.0])
        monkeypatch.setattr(sample_mod, "_current_sample", sample_mod.contextvars.ContextVar("t", default=None))
        monkeypatch.setattr("skyvern.webeye.cdp_retry._sleep", AsyncMock())
        pw = _make_playwright(
            [ConnectionRefusedError("refused"), ConnectionRefusedError("refused"), asyncio.CancelledError()]
        )
        with sample_mod.browser_acquisition_scope("create"):
            with pytest.raises(asyncio.CancelledError):
                await connect_over_cdp_with_retry(pw, "wss://cdp.example.test", validate_browser_address=False)
            sample = sample_mod.current_browser_acquisition_sample()
            assert sample is not None and sample.cdp_connect_attempts == 3

    @pytest.mark.asyncio
    async def test_no_scope_is_silent_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_budget(monkeypatch, attempts=2, backoff=[0.0])
        monkeypatch.setattr(sample_mod, "_current_sample", sample_mod.contextvars.ContextVar("t", default=None))
        await connect_over_cdp_with_retry(
            _make_playwright(MagicMock()), "wss://cdp.example.test", validate_browser_address=False
        )
        assert sample_mod.current_browser_acquisition_sample() is None
