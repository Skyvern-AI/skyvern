"""Tests for extra HTTP header sanitization in browser_factory.py.

A malformed header name passed through extra_http_headers makes Chromium reject the
whole batch via Network.setExtraHTTPHeaders ("Invalid header name"), which fails browser
context creation outright (SKY-8929). sanitize_browser_headers drops the bad names so the
launch survives with the valid headers intact.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog

import skyvern.webeye.browser_factory as browser_factory
from skyvern.webeye.browser_artifacts import BrowserArtifacts
from skyvern.webeye.browser_factory import BrowserContextFactory, sanitize_browser_headers


class _FakeRequest:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self._headers = headers

    async def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _FakeRoute:
    def __init__(self) -> None:
        self.headers: dict[str, str] | None = None

    async def fallback(self, *, headers: dict[str, str] | None = None) -> None:
        self.headers = headers


class _FakeBrowserContext:
    def __init__(self, context_headers: dict[str, str] | None = None) -> None:
        self.context_headers = dict(context_headers or {})
        self._handler: Callable[[_FakeRoute, _FakeRequest], Awaitable[None]] | None = None
        self.route_calls = 0

    async def route(
        self,
        pattern: str,
        handler: Callable[[_FakeRoute, _FakeRequest], Awaitable[None]],
    ) -> None:
        assert pattern == "**/*"
        self.route_calls += 1
        self._handler = handler

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.context_headers = dict(headers)

    async def dispatch(self, url: str, headers: dict[str, str] | None = None) -> dict[str, str]:
        request_headers = {**self.context_headers, **(headers or {})}
        if self._handler is None:
            return request_headers

        route = _FakeRoute()
        await self._handler(route, _FakeRequest(url, request_headers))
        return route.headers if route.headers is not None else request_headers


class _FakeAgentFunction:
    def __init__(self, *, route_handlers_allowed: bool) -> None:
        self.route_handlers_allowed = route_handlers_allowed
        self.route_permission_checks = 0
        self.extension_setup_calls = 0
        self.header_route_origins: list[Any] = []

    def strip_proxy_session_extra_http_headers(
        self,
        headers: dict[str, str] | None,
    ) -> dict[str, str] | None:
        return {name: value for name, value in (headers or {}).items() if name != "X-Automation"}

    async def browser_context_route_handlers_allowed(self, **kwargs: Any) -> bool:
        self.route_permission_checks += 1
        return self.route_handlers_allowed

    async def should_apply_banked_cookies(self, organization_id: str | None) -> bool:
        return False

    async def setup_browser_context_extensions(self, **kwargs: Any) -> None:
        self.extension_setup_calls += 1

    def on_origin_scoped_headers_route_installed(self, browser_context: Any, target_origin: Any) -> None:
        self.header_route_origins.append(target_origin)


async def _factory_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    route_handlers_allowed: bool = True,
    target_url: str | None = "https://target.test/start",
) -> tuple[_FakeBrowserContext, dict[str, Any], _FakeAgentFunction]:
    captured_creator_kwargs: dict[str, Any] = {}
    contexts: list[_FakeBrowserContext] = []

    async def creator(playwright: Any, **kwargs: Any) -> tuple[_FakeBrowserContext, BrowserArtifacts, None]:
        captured_creator_kwargs.update(kwargs)
        context = _FakeBrowserContext(kwargs.get("extra_http_headers"))
        contexts.append(context)
        return context, BrowserArtifacts(), None

    agent_function = _FakeAgentFunction(route_handlers_allowed=route_handlers_allowed)

    class FakeApp:
        AGENT_FUNCTION = agent_function

    monkeypatch.setattr(browser_factory, "app", FakeApp())
    monkeypatch.setattr(browser_factory, "restore_session_cookies", AsyncMock())
    monkeypatch.setattr(browser_factory, "set_browser_console_log", lambda **kwargs: None)
    monkeypatch.setattr(browser_factory, "set_popup_video_listener", lambda **kwargs: None)
    monkeypatch.setattr(browser_factory, "set_download_file_listener", lambda **kwargs: None)
    monkeypatch.setattr(browser_factory, "set_dialog_handler", lambda **kwargs: None)
    BrowserContextFactory.register_type("test-header-scoping", creator)
    monkeypatch.setattr(browser_factory.settings, "BROWSER_TYPE", "test-header-scoping")

    await BrowserContextFactory.create_browser_context(
        playwright=object(),  # type: ignore[arg-type]
        url=target_url,
        extra_http_headers={
            "X-Test-Credential": "fake-token",
            "X-Automation": "automation-value",
        },
    )

    return contexts[0], captured_creator_kwargs, agent_function


class TestSanitizeBrowserHeaders:
    def test_none_passes_through(self) -> None:
        assert sanitize_browser_headers(None) is None

    def test_empty_returns_none(self) -> None:
        assert sanitize_browser_headers({}) is None

    def test_valid_headers_unchanged(self) -> None:
        headers = {"X-Custom-Header": "value", "Authorization": "Bearer abc", "Accept": "application/json"}
        assert sanitize_browser_headers(headers) == headers

    def test_token_special_characters_allowed(self) -> None:
        headers = {"X-Foo_Bar.Baz!#$%&'*+^`|~-": "ok"}
        assert sanitize_browser_headers(headers) == headers

    def test_drops_header_name_with_space(self) -> None:
        result = sanitize_browser_headers({"Invalid Header": "v", "Valid-Header": "keep"})
        assert result == {"Valid-Header": "keep"}

    def test_drops_header_name_with_colon_or_newline(self) -> None:
        result = sanitize_browser_headers({"Bad:Name": "v", "Bad\nName": "v", "Good-Name": "keep"})
        assert result == {"Good-Name": "keep"}

    def test_drops_empty_header_name(self) -> None:
        result = sanitize_browser_headers({"": "v", "Good": "keep"})
        assert result == {"Good": "keep"}

    def test_all_invalid_collapses_to_none(self) -> None:
        assert sanitize_browser_headers({"bad name": "v", "": "w"}) is None

    def test_drops_name_with_trailing_newline(self) -> None:
        # `$` matches before a trailing newline, so this must use fullmatch to be dropped.
        result = sanitize_browser_headers({"X-Custom\n": "v", "X-Ok": "keep"})
        assert result == {"X-Ok": "keep"}

    def test_drops_value_with_crlf(self) -> None:
        result = sanitize_browser_headers({"X-Bad": "ok\r\nInjected: evil", "X-Ok": "keep"})
        assert result == {"X-Ok": "keep"}

    def test_drops_value_with_newline_or_null(self) -> None:
        result = sanitize_browser_headers({"X-NL": "a\nb", "X-Null": "a\x00b", "X-Ok": "keep"})
        assert result == {"X-Ok": "keep"}


@pytest.mark.asyncio
async def test_intended_origin_carries_caller_header(monkeypatch: pytest.MonkeyPatch) -> None:
    context, creator_kwargs, agent_function = await _factory_context(monkeypatch)

    headers = await context.dispatch("https://target.test/api")

    assert headers["X-Test-Credential"] == "fake-token"
    assert creator_kwargs["extra_http_headers"] == {"X-Automation": "automation-value"}
    assert agent_function.route_permission_checks == 1


@pytest.mark.asyncio
async def test_cross_origin_subresource_omits_caller_header(monkeypatch: pytest.MonkeyPatch) -> None:
    context, creator_kwargs, _ = await _factory_context(monkeypatch)

    headers = await context.dispatch("https://assets.test/script.js")

    assert "X-Test-Credential" not in creator_kwargs["extra_http_headers"]
    assert "X-Test-Credential" not in headers


@pytest.mark.asyncio
async def test_cross_origin_redirect_omits_caller_header(monkeypatch: pytest.MonkeyPatch) -> None:
    context, _, _ = await _factory_context(monkeypatch)
    initial_headers = await context.dispatch("https://target.test/redirect")

    redirected_headers = await context.dispatch("https://redirect.test/final", headers=initial_headers)

    assert "X-Test-Credential" not in redirected_headers


@pytest.mark.asyncio
async def test_different_port_is_a_different_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    context, _, _ = await _factory_context(monkeypatch)

    headers = await context.dispatch("https://target.test:8443/api")

    assert "X-Test-Credential" not in headers


@pytest.mark.asyncio
async def test_lookalike_hostname_is_a_different_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    context, _, _ = await _factory_context(monkeypatch)

    headers = await context.dispatch("https://target.test.attacker.test/api")

    assert "X-Test-Credential" not in headers


@pytest.mark.asyncio
async def test_automation_headers_are_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    context, creator_kwargs, _ = await _factory_context(monkeypatch)

    headers = await context.dispatch("https://assets.test/script.js")

    assert headers["X-Automation"] == "automation-value"
    assert creator_kwargs["extra_http_headers"] == {"X-Automation": "automation-value"}


@pytest.mark.asyncio
async def test_missing_target_origin_omits_caller_header(monkeypatch: pytest.MonkeyPatch) -> None:
    context, creator_kwargs, _ = await _factory_context(monkeypatch, target_url=None)

    headers = await context.dispatch("https://target.test/api")

    assert "X-Test-Credential" not in headers
    assert creator_kwargs["extra_http_headers"] == {"X-Automation": "automation-value"}


@pytest.mark.asyncio
async def test_routes_not_permitted_omits_caller_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    with structlog.testing.capture_logs() as logs:
        context, creator_kwargs, agent_function = await _factory_context(
            monkeypatch,
            route_handlers_allowed=False,
        )

    headers = await context.dispatch("https://target.test/api")

    assert creator_kwargs["extra_http_headers"] == {"X-Automation": "automation-value"}
    assert "X-Test-Credential" not in headers
    assert headers["X-Automation"] == "automation-value"
    assert context.route_calls == 0
    assert agent_function.route_permission_checks == 1
    assert agent_function.extension_setup_calls == 1
    assert {
        "event": "Omitting caller HTTP headers because browser context route handlers are not permitted",
        "log_level": "warning",
    } in logs
