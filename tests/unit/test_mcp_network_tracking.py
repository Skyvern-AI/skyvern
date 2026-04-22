"""Tests for network tracking: request_id, resource_type, body capture, route/unroute, detail."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _make_state() -> SessionState:
    return SessionState(
        console_messages=deque(maxlen=1000),
        network_requests=deque(maxlen=1000),
        dialog_events=deque(maxlen=1000),
    )


def _make_page(raw: MagicMock | None = None) -> SimpleNamespace:
    if raw is None:
        raw = MagicMock()
        raw.on = MagicMock()
    return SimpleNamespace(page=raw)


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
    async def test_abort_route(self) -> None:
        state = _make_state()
        raw_page = AsyncMock()
        result = await do_network_route(raw_page, state, url_pattern="**/api/*", action="abort")
        assert result.url_pattern == "**/api/*"
        assert result.action == "abort"
        assert "**/api/*" in result.active_routes
        assert "**/api/*" in state.active_routes.get(id(raw_page), set())
        raw_page.route.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mock_route(self) -> None:
        state = _make_state()
        raw_page = AsyncMock()
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

    @pytest.mark.asyncio
    async def test_re_register_unroutes_first(self) -> None:
        state = _make_state()
        raw_page = AsyncMock()
        state.active_routes.setdefault(id(raw_page), set()).add("**/api/*")
        await do_network_route(raw_page, state, url_pattern="**/api/*", action="abort")
        raw_page.unroute.assert_awaited_once_with("**/api/*")
        raw_page.route.assert_awaited_once()


class TestDoNetworkUnroute:
    @pytest.mark.asyncio
    async def test_removes_active_route(self) -> None:
        state = _make_state()
        raw_page = AsyncMock()
        state.active_routes.setdefault(id(raw_page), set()).add("**/api/*")
        result = await do_network_unroute(raw_page, state, "**/api/*")
        assert result.removed is True
        assert "**/api/*" not in state.active_routes.get(id(raw_page), set())
        raw_page.unroute.assert_awaited_once_with("**/api/*")

    @pytest.mark.asyncio
    async def test_noop_for_unknown_pattern(self) -> None:
        state = _make_state()
        raw_page = AsyncMock()
        result = await do_network_unroute(raw_page, state, "**/nope/*")
        assert result.removed is False
        raw_page.unroute.assert_not_awaited()


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
        raw = MagicMock()
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
        raw = MagicMock()
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
