"""Tests for network tracking: request_id, resource_type, body capture, route/unroute, detail."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as SdkError

from skyvern.cli.core.browser_ops import (
    do_network_request_detail,
    do_network_requests,
    do_network_route,
    do_network_unroute,
)
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import SessionState
from skyvern.cli.mcp_tools.inspection import (
    _capture_body,
    _register_hooks_on_page,
    _safe_headers,
    _should_capture_body,
    skyvern_network_request_detail,
    skyvern_network_requests,
    skyvern_network_route,
    skyvern_network_unroute,
)
from skyvern.webeye import http_interception as interception
from skyvern.webeye.cdp_errors import CdpError, CdpProtocolError, CdpTimeoutError
from skyvern.webeye.cdp_fetch import Request, Route, Settlement
from skyvern.webeye.http_interception import _SdkSession, http_interception
from tests.unit._mcp_browser_fakes import make_cdp_page as _make_cdp_page
from tests.unit._mcp_browser_fakes import make_page as _make_page
from tests.unit._mcp_browser_fakes import make_session_state as _make_state
from tests.unit.scoped_asyncio import ScopedAsyncio


def _patch(monkeypatch: pytest.MonkeyPatch, state: SessionState) -> None:
    raw = MagicMock()
    raw.on = MagicMock()

    async def fake_get_page(**kwargs):
        return _make_page(raw), BrowserContext(mode="local")

    monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_page", fake_get_page)
    monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_current_session", lambda: state)
    monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", False)


def _network_entry(
    request_id: int = 0,
    url: str = "https://a.com",
    method: str = "GET",
    status: int = 200,
    resource_type: str = "fetch",
    content_type: str = "application/json",
    tab_id: str = "0",
) -> dict:
    return {
        "request_id": request_id,
        "url": url,
        "method": method,
        "status": status,
        "content_type": content_type,
        "resource_type": resource_type,
        "tab_id": tab_id,
        "timing_ms": 0,
        "response_size": 0,
        "response_headers": {"content-type": content_type},
        "page_url": "",
    }


# --- _safe_headers ---


class TestSafeHeaders:
    def test_filters_to_allowlist(self) -> None:
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer secret",
            "cookie": "session=abc",
            "set-cookie": "x=y",
            "cache-control": "max-age=300",
            "x-request-id": "abc123",
        }
        filtered = _safe_headers(headers)
        assert "content-type" in filtered
        assert "cache-control" in filtered
        assert "x-request-id" in filtered
        assert "authorization" not in filtered
        assert "cookie" not in filtered
        assert "set-cookie" not in filtered

    def test_empty_headers(self) -> None:
        assert _safe_headers({}) == {}


# --- _should_capture_body ---


class TestShouldCaptureBody:
    def test_json_captured(self) -> None:
        assert _should_capture_body("application/json", None) is True

    def test_html_captured(self) -> None:
        assert _should_capture_body("text/html; charset=utf-8", None) is True

    def test_image_skipped(self) -> None:
        assert _should_capture_body("image/png", None) is False

    def test_empty_content_type(self) -> None:
        assert _should_capture_body("", None) is False

    def test_oversized_skipped(self) -> None:
        assert _should_capture_body("application/json", "500000") is False

    def test_reasonable_size_captured(self) -> None:
        assert _should_capture_body("application/json", "1024") is True


# --- _capture_body ---


class TestCaptureBody:
    @pytest.mark.asyncio
    async def test_stores_body(self) -> None:
        state = _make_state()
        state.network_requests.append({"request_id": 42, "url": "https://example.com"})
        response = AsyncMock()
        response.body = AsyncMock(return_value=b'{"key": "value"}')

        await _capture_body(response, 42, state)
        assert 42 in state._body_store
        assert state._body_store[42] == '{"key": "value"}'

    @pytest.mark.asyncio
    async def test_fifo_eviction(self) -> None:
        state = _make_state()
        # Fill to capacity with matching network_requests entries
        for i in range(100):
            state._body_store[i] = f"body-{i}"
            state.network_requests.append({"request_id": i, "url": f"https://example.com/{i}"})
        state.network_requests.append({"request_id": 999, "url": "https://example.com/999"})

        response = AsyncMock()
        response.body = AsyncMock(return_value=b"new body")
        await _capture_body(response, 999, state)

        assert 999 in state._body_store
        assert 0 not in state._body_store  # oldest evicted
        assert len(state._body_store) == 100

    @pytest.mark.asyncio
    async def test_truncates_large_body(self) -> None:
        state = _make_state()
        state.network_requests.append({"request_id": 1, "url": "https://example.com"})
        response = AsyncMock()
        response.body = AsyncMock(return_value=b"x" * 300_000)

        await _capture_body(response, 1, state)
        body = state._body_store[1]
        assert body.endswith("...[truncated]")
        assert len(body) < 300_000

    @pytest.mark.asyncio
    async def test_skips_write_after_clear(self) -> None:
        """Body capture tasks that finish after clear should not write stale data."""
        state = _make_state()
        # request_id not in network_requests (simulates post-clear)
        response = AsyncMock()
        response.body = AsyncMock(return_value=b"stale data")

        await _capture_body(response, 42, state)
        assert 42 not in state._body_store


# --- _on_response hook ---


class TestOnResponseHook:
    def test_assigns_request_id_and_resource_type(self) -> None:
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        raw.url = "https://example.com"

        _register_hooks_on_page(state, raw)

        # Extract the _on_response handler
        on_response = None
        for call in raw.on.call_args_list:
            if call.args[0] == "response":
                on_response = call.args[1]
                break
        assert on_response is not None

        # Simulate a response
        response = MagicMock()
        response.url = "https://api.com/data"
        response.request.method = "GET"
        response.request.resource_type = "fetch"
        response.request.timing = {"responseEnd": 42.5}
        response.status = 200
        response.headers = {"content-type": "image/png", "content-length": "5000"}

        on_response(response)

        assert len(state.network_requests) == 1
        entry = state.network_requests[0]
        assert entry["request_id"] == 0
        assert entry["resource_type"] == "fetch"
        assert entry["response_headers"] == {"content-type": "image/png", "content-length": "5000"}

    def test_auto_increments_request_id(self) -> None:
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        raw.url = "https://example.com"

        _register_hooks_on_page(state, raw)

        on_response = None
        for call in raw.on.call_args_list:
            if call.args[0] == "response":
                on_response = call.args[1]
                break

        for i in range(3):
            response = MagicMock()
            response.url = f"https://api.com/{i}"
            response.request.method = "GET"
            response.request.resource_type = "xhr"
            response.request.timing = {}
            response.status = 200
            response.headers = {"content-type": "image/png"}
            on_response(response)

        assert [e["request_id"] for e in state.network_requests] == [0, 1, 2]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stateless", [False, True], ids=["stateful", "stateless"])
    async def test_json_body_is_fetched_only_when_a_tool_can_read_it(
        self, monkeypatch: pytest.MonkeyPatch, stateless: bool
    ) -> None:
        """Stateless HTTP (hosted MCP inside the API process) disables every body reader, so a CDP body fetch there
        is only event-loop work; API stall dumps caught the loop inside these fetches."""
        monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", stateless)
        state = _make_state()
        raw = MagicMock()
        raw.on = MagicMock()
        raw.url = "https://example.com"
        _register_hooks_on_page(state, raw)
        on_response = next(call.args[1] for call in raw.on.call_args_list if call.args[0] == "response")

        response = MagicMock()
        response.url = "https://api.com/data"
        response.request.method = "GET"
        response.request.resource_type = "xhr"
        response.request.timing = {}
        response.status = 200
        response.headers = {"content-type": "application/json"}
        response.body = AsyncMock(return_value=b'{"ok": true}')
        on_response(response)
        await asyncio.gather(*state._pending_tasks)

        assert len(state.network_requests) == 1
        if stateless:
            response.body.assert_not_awaited()
        else:
            assert state.get_response_body(0) == '{"ok": true}'


# --- do_network_requests (browser_ops) ---


class TestDoNetworkRequests:
    def test_no_filters(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0, "https://a.com"))
        state.network_requests.append(_network_entry(1, "https://b.com"))
        result = do_network_requests(state)
        assert result.count == 2

    def test_filter_by_resource_type(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0, resource_type="fetch"))
        state.network_requests.append(_network_entry(1, resource_type="image"))
        result = do_network_requests(state, resource_type="fetch")
        assert result.count == 1
        assert result.requests[0]["request_id"] == 0

    def test_filter_by_url_pattern(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0, url="https://api.com/v1/data"))
        state.network_requests.append(_network_entry(1, url="https://cdn.com/image.png"))
        result = do_network_requests(state, url_pattern="api")
        assert result.count == 1

    def test_invalid_regex_returns_error(self) -> None:
        state = _make_state()
        result = do_network_requests(state, url_pattern="[invalid")
        assert result.error is not None
        assert result.error["code"] == "INVALID_INPUT"

    def test_strips_response_headers_from_list(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0))
        result = do_network_requests(state)
        assert "response_headers" not in result.requests[0]

    def test_combined_filters(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0, method="GET", status=200, resource_type="fetch"))
        state.network_requests.append(_network_entry(1, method="POST", status=200, resource_type="fetch"))
        state.network_requests.append(_network_entry(2, method="GET", status=404, resource_type="fetch"))
        result = do_network_requests(state, method="GET", status_code=200)
        assert result.count == 1
        assert result.requests[0]["request_id"] == 0


# --- do_network_request_detail (browser_ops) ---


class TestDoNetworkRequestDetail:
    def test_found(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(42))
        state._body_store[42] = '{"key": "value"}'
        result = do_network_request_detail(state, 42)
        assert result.found is True
        assert result.request is not None
        assert result.request["request_id"] == 42
        assert result.body == '{"key": "value"}'

    def test_not_found(self) -> None:
        state = _make_state()
        result = do_network_request_detail(state, 999)
        assert result.found is False
        assert result.request is None

    def test_no_body(self) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(10))
        result = do_network_request_detail(state, 10)
        assert result.found is True
        assert result.body is None


# --- do_network_route / do_network_unroute (browser_ops) ---


class TestDoNetworkRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "expected_error", "settlement"),
        [(None, None, None)]
        + [
            (
                SdkError("CDPSession.send: Protocol error (Fetch.fulfillRequest): Invalid InterceptionId."),
                None,
                Settlement.CLOSED,
            ),
            (
                SdkError("CDPSession.send: Protocol error (Fetch.fulfillRequest): Session with given id not found."),
                None,
                Settlement.CLOSED,
            ),
            (
                SdkError("CDPSession.send: Protocol error (Fetch.fulfillRequest): Invalid parameters"),
                CdpProtocolError,
                Settlement.PENDING,
            ),
            (SdkError("CDPSession.send: Invalid InterceptionId."), CdpError, Settlement.UNKNOWN),
            (
                SdkError("CDPSession.send: Protocol error (Fetch.failRequest): Invalid InterceptionId."),
                CdpError,
                Settlement.UNKNOWN,
            ),
            (RuntimeError("acknowledgment lost"), CdpError, Settlement.UNKNOWN),
            (TimeoutError(), CdpTimeoutError, Settlement.UNKNOWN),
            (asyncio.CancelledError(), asyncio.CancelledError, Settlement.UNKNOWN),
        ],
    )
    async def test_abort_route(self, error, expected_error, settlement) -> None:
        if error is not None:
            await _case_sdk_terminal_rejection_preserves_settlement_ownership(error, expected_error, settlement)
            return
        state = _make_state()
        raw_page = _make_cdp_page()
        result = await do_network_route(raw_page, state, url_pattern="**/api/*", action="abort")
        assert result.url_pattern == "**/api/*"
        assert result.action == "abort"
        assert "**/api/*" in result.active_routes
        assert "**/api/*" in state.active_routes.get(id(raw_page), set())
        raw_page.route.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cache_case",
        [
            None,
            "enable_rejected",
            "enable_unknown",
            "enable_cancelled",
            "network_rejected",
            "network_unknown",
            "network_cancelled",
            "disable_rejected",
            "restore_rejected",
            "cancel_close",
            "restore_and_detach_timeout",
            "restore_protocol_detach_timeout",
            "detach_timeout",
            "cancel_restore_detach_timeout",
        ],
    )
    async def test_mock_route(self, cache_case, monkeypatch) -> None:
        if cache_case is not None:
            await _case_sdk_cache_lifecycle(cache_case, monkeypatch)
            return
        state = _make_state()
        raw_page = _make_cdp_page()
        result = await do_network_route(
            raw_page,
            state,
            url_pattern="**/data",
            action="mock",
            mock_status=201,
            mock_body='{"ok": true}',
            mock_content_type="application/json",
        )
        assert result.action == "mock"
        assert "**/data" in state.active_routes.get(id(raw_page), set())


class TestDoNetworkUnroute:
    @pytest.mark.asyncio
    async def test_removes_active_route(self) -> None:
        state = _make_state()
        raw_page = _make_cdp_page()
        state.active_routes.setdefault(id(raw_page), set()).add("**/api/*")
        result = await do_network_unroute(raw_page, state, "**/api/*")
        assert result.removed is True
        assert "**/api/*" not in state.active_routes.get(id(raw_page), set())
        raw_page.unroute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noop_for_unknown_pattern(self) -> None:
        state = _make_state()
        raw_page = AsyncMock()
        result = await do_network_unroute(raw_page, state, "**/nope/*")
        assert result.removed is False
        raw_page.unroute.assert_not_awaited()


async def _case_sdk_cache_lifecycle(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    page = _make_cdp_page()
    session = await page.context.new_cdp_session(page)
    commands = []
    cache = {"disabled": False, "detached": False}
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(method, params):
        commands.append((method, dict(params or {})))
        if method == "Network.enable":
            if case == "network_rejected":
                raise SdkError(f"CDPSession.send: Protocol error ({method}): Invalid parameters")
            if case == "network_unknown":
                raise RuntimeError("synthetic acknowledgment loss")
            if case == "network_cancelled":
                raise asyncio.CancelledError
        if method == "Network.setCacheDisabled":
            if not params["cacheDisabled"] and case == "restore_rejected":
                raise SdkError(f"CDPSession.send: Protocol error ({method}): Invalid parameters")
            if not params["cacheDisabled"] and case == "cancel_close":
                entered.set()
                await release.wait()
            cache["disabled"] = params["cacheDisabled"]
        if method == "Fetch.enable":
            if case == "enable_rejected":
                raise SdkError(f"CDPSession.send: Protocol error ({method}): Invalid parameters")
            if case == "enable_unknown":
                raise RuntimeError("synthetic acknowledgment loss")
            if case == "enable_cancelled":
                raise asyncio.CancelledError
        if method == "Fetch.disable" and case == "disable_rejected":
            raise SdkError(f"CDPSession.send: Protocol error ({method}): Invalid parameters")
        return {}

    async def detach():
        cache.update(disabled=False, detached=True)

    session.send.side_effect = command
    session.detach.side_effect = detach
    adapter = _SdkSession(session, page)
    closing = None
    if case.endswith("_timeout"):
        second = _make_cdp_page()
        second.context = page.context
        completed = []
        next_session = MagicMock(send=AsyncMock(return_value={}), detach=AsyncMock())

        async def detach_second():
            completed.append(second)

        next_session.detach.side_effect = detach_second
        page.context.new_cdp_session.side_effect = [session, next_session]
        monkeypatch.setattr(interception, "asyncio", ScopedAsyncio(timeout=lambda _: asyncio.timeout(0.02)))
        failure = CdpProtocolError("Network.setCacheDisabled", -32602, "restoration rejected")

        async def stalled_command(method, params):
            if method == "Network.setCacheDisabled" and not params["cacheDisabled"]:
                entered.set()
                if case == "restore_protocol_detach_timeout":
                    raise failure
                if case != "detach_timeout":
                    await release.wait()
            return {}

        async def stalled_detach():
            await release.wait()

        session.send.side_effect = stalled_command
        session.detach.side_effect = stalled_detach
        await do_network_route(page, _make_state(), url_pattern="**/first", action="abort")
        await do_network_route(second, _make_state(), url_pattern="**/second", action="abort")
        owner = http_interception(page.context)
        second_binding = owner._pages[second]
        cancelling = case == "cancel_restore_detach_timeout"
        closing = asyncio.create_task(owner._pages[page].session.detach() if cancelling else owner.close())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if cancelling:
                closing.cancel()
            done, _ = await asyncio.wait([closing], timeout=0.3)
            assert done, "Expired restoration deadline left detach and the next page pending"
            result = (await asyncio.gather(closing, return_exceptions=True))[0]
            if cancelling:
                assert isinstance(result, asyncio.CancelledError) and closing.cancelled()
                release.set()
                await owner.close()
            elif case == "restore_protocol_detach_timeout":
                assert result is failure
            else:
                assert isinstance(result, (TimeoutError, CdpTimeoutError))
            assert owner._closed and second not in owner._pages and completed == [second]
            assert second_binding.session not in owner._router._sessions
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 1)
        return
    try:
        if case.startswith("network_"):
            expected = {
                "network_rejected": CdpProtocolError,
                "network_unknown": CdpError,
                "network_cancelled": asyncio.CancelledError,
            }[case]
            with pytest.raises(expected):
                await adapter.send("Fetch.enable", {})
            assert [method for method, _ in commands] == ["Network.enable"]
            assert not cache["disabled"] and not adapter._cache_disabled
            return
        if case == "cancel_close":
            await do_network_route(page, _make_state(), url_pattern="**/api", action="abort")
            owner = http_interception(page.context)
            closing = asyncio.create_task(owner.close())
            await asyncio.wait_for(entered.wait(), 1)
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done() and cache["disabled"] and not cache["detached"]
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert owner._closed and not owner._pages and not owner._router._sessions
            assert not cache["disabled"] and cache["detached"]
            return
        if case in {"enable_rejected", "enable_unknown", "enable_cancelled"}:
            expected = {
                "enable_rejected": CdpProtocolError,
                "enable_unknown": CdpError,
                "enable_cancelled": asyncio.CancelledError,
            }[case]
            with pytest.raises(expected):
                await adapter.send("Fetch.enable", {})
            assert cache["disabled"] == adapter._cache_disabled == (case != "enable_rejected")
        else:
            await adapter.send("Fetch.enable", {})
            assert cache["disabled"] and adapter._cache_disabled
            if case == "disable_rejected":
                with pytest.raises(CdpProtocolError):
                    await adapter.send("Fetch.disable")
                assert cache["disabled"] and adapter._cache_disabled
            else:
                with pytest.raises(CdpProtocolError) as caught:
                    await adapter.detach()
                assert caught.value.method == "Network.setCacheDisabled"
                assert cache["detached"] and not cache["disabled"] and not adapter._cache_disabled
        methods = [method for method, _ in commands if method.startswith("Fetch.")]
        assert methods == (["Fetch.enable", "Fetch.disable"] if case == "disable_rejected" else ["Fetch.enable"])
    finally:
        release.set()
        if closing is not None:
            await asyncio.wait_for(asyncio.gather(closing, return_exceptions=True), 1)
        if not cache["detached"]:
            await adapter.detach()


async def _case_sdk_terminal_rejection_preserves_settlement_ownership(
    error: BaseException, expected_error: type[BaseException] | None, settlement: Settlement
) -> None:
    page = _make_cdp_page()
    session = await page.context.new_cdp_session(page)
    commands = {}

    async def rejected(method, params):
        request_id = params["requestId"]
        assert request_id not in commands, "A terminal request was sent twice"
        commands[request_id] = (method, params["body"])
        raise error

    session.send.side_effect = rejected
    route = Route(_SdkSession(session, page), "paused", Request({}))
    if expected_error is None:
        await route.fulfill(body="mocked")
    else:
        with pytest.raises(expected_error) as caught:
            await route.fulfill(body="mocked")
        assert type(caught.value) is expected_error
    assert route._state is settlement
    assert route.settled == (settlement is Settlement.CLOSED)
    assert route.can_settle == (settlement is Settlement.PENDING)
    assert commands == {"paused": ("Fetch.fulfillRequest", "bW9ja2Vk")}


# --- MCP tool: skyvern_network_requests (refactored) ---


class TestNetworkRequestsMCP:
    @pytest.mark.asyncio
    async def test_resource_type_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0, resource_type="fetch"))
        state.network_requests.append(_network_entry(1, resource_type="image"))
        _patch(monkeypatch, state)

        result = await skyvern_network_requests(resource_type="fetch")
        assert result["ok"] is True
        assert result["data"]["count"] == 1

    @pytest.mark.asyncio
    async def test_clear_with_filter_uses_request_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(0, resource_type="fetch"))
        state.network_requests.append(_network_entry(1, resource_type="image"))
        _patch(monkeypatch, state)

        result = await skyvern_network_requests(resource_type="fetch", clear=True)
        assert result["data"]["count"] == 1
        # Only the image entry should remain
        assert len(state.network_requests) == 1
        assert state.network_requests[0]["resource_type"] == "image"


# --- MCP tool: skyvern_network_request_detail ---


class TestNetworkRequestDetailMCP:
    @pytest.mark.asyncio
    async def test_returns_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        state.network_requests.append(_network_entry(5))
        state._body_store[5] = "hello body"
        _patch(monkeypatch, state)

        result = await skyvern_network_request_detail(request_id=5)
        assert result["ok"] is True
        assert result["data"]["body"] == "hello body"
        assert result["data"]["body_available"] is True
        assert result["data"]["request"]["request_id"] == 5

    @pytest.mark.asyncio
    async def test_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        _patch(monkeypatch, state)

        result = await skyvern_network_request_detail(request_id=999)
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


# --- MCP tool: skyvern_network_route ---


class TestNetworkRouteMCP:
    @pytest.mark.asyncio
    async def test_abort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        raw = _make_cdp_page()
        raw.on = MagicMock()
        raw.route = AsyncMock()
        raw.unroute = AsyncMock()

        async def fake_get_page(**kwargs):
            return SimpleNamespace(page=raw), BrowserContext(mode="local")

        monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_page", fake_get_page)
        monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_current_session", lambda: state)
        monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", False)

        result = await skyvern_network_route(url_pattern="**/ads/*", action="abort")
        assert result["ok"] is True
        assert "**/ads/*" in result["data"]["active_routes"]


# --- MCP tool: skyvern_network_unroute ---


class TestNetworkUnrouteMCP:
    @pytest.mark.asyncio
    async def test_remove(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = _make_state()
        raw = _make_cdp_page()
        raw.on = MagicMock()
        raw.route = AsyncMock()
        raw.unroute = AsyncMock()
        state.active_routes.setdefault(id(raw), set()).add("**/ads/*")

        async def fake_get_page(**kwargs):
            return SimpleNamespace(page=raw), BrowserContext(mode="local")

        monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_page", fake_get_page)
        monkeypatch.setattr("skyvern.cli.mcp_tools.inspection.get_current_session", lambda: state)
        monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", False)

        result = await skyvern_network_unroute(url_pattern="**/ads/*")
        assert result["ok"] is True
        assert result["data"]["removed"] is True


# --- Stateless HTTP mode ---


class TestStatelessMode:
    @pytest.mark.asyncio
    async def test_new_tools_error_in_stateless(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, _make_state())
        monkeypatch.setattr("skyvern.cli.core.session_manager._stateless_http_mode", True)

        for tool in (skyvern_network_request_detail, skyvern_network_route, skyvern_network_unroute):
            # Provide required arguments
            if tool is skyvern_network_request_detail:
                result = await tool(request_id=0)
            elif tool is skyvern_network_route:
                result = await tool(url_pattern="**/*")
            else:
                result = await tool(url_pattern="**/*")
            assert result["ok"] is False, f"{tool.__name__} should error in stateless mode"
