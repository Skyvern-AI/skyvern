from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer


class _Request:
    resource_type = "document"
    url = "https://public.test/start"

    class _Frame:
        parent_frame = None

    frame = _Frame()

    def is_navigation_request(self) -> bool:
        return True


class _Route:
    def __init__(self) -> None:
        self.outcome = ""

    async def fallback(self) -> None:
        self.outcome = "fallback"

    async def abort(self, reason: str) -> None:
        self.outcome = reason


class _BrowserContext:
    def __init__(self, browser: _Browser | None = None) -> None:
        self.handler = None
        self.unrouted = False
        self.pages = [SimpleNamespace(url="about:blank")]
        self.service_workers = []
        self.browser = browser or _Browser()
        self.closed = False

    async def route(self, _pattern: str, handler) -> None:
        self.handler = handler

    async def unroute(self, _pattern: str, handler) -> None:
        assert handler is self.handler
        self.unrouted = True

    async def cookies(self) -> list[dict[str, str]]:
        return []

    async def new_page(self) -> SimpleNamespace:
        page = SimpleNamespace(url="about:blank")
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self) -> None:
        self.new_context_kwargs: dict[str, str] | None = None
        self.candidate_context: _BrowserContext | None = None
        self.closed = False

    async def new_context(self, **kwargs: str) -> _BrowserContext:
        self.new_context_kwargs = kwargs
        self.candidate_context = _BrowserContext(self)
        return self.candidate_context

    async def close(self) -> None:
        self.closed = True


class _Page:
    url = "https://public.test/start"


@dataclass
class _BrowserState:
    browser_context: _BrowserContext
    page: SimpleNamespace | None = None
    pw: SimpleNamespace | None = None
    active_page: SimpleNamespace | None = None
    prefer_context_newest_when_unpinned: bool = False

    async def get_working_page(self) -> _Page | SimpleNamespace:
        if self.active_page is not None:
            if self.active_page in self.browser_context.pages:
                self.page = self.active_page
                return self.active_page
            self.active_page = None
        if self.prefer_context_newest_when_unpinned:
            self.page = self.browser_context.pages[-1]
            return self.page
        return self.page or _Page()

    async def set_working_page(self, page: _Page | SimpleNamespace | None) -> None:
        self.page = page

    async def set_active_page(self, page: SimpleNamespace) -> None:
        self.active_page = page
        self.page = page


class _AgentFunction:
    def __init__(self) -> None:
        self.active = False
        self.idle_waits = 0

    @asynccontextmanager
    async def copilot_candidate_network_guard(self, _browser_context, *, expected_origin: str):
        assert expected_origin == "https://public.test"
        self.active = True
        hops = [
            {
                "url": "https://public.test/start",
                "resource_type": "document",
                "resolved_public_ips": ["93.184.216.34"],
                "connected_peer_ip": "93.184.216.34",
                "enforcement_version": "copilot_candidate_preconnect_v1",
            }
        ]
        try:
            yield hops
        finally:
            self.active = False

    async def wait_for_copilot_candidate_network_idle(self, _browser_context) -> None:
        self.idle_waits += 1

    async def setup_browser_context_extensions(self, _browser_context, **_kwargs) -> None:
        return None


def _server() -> SkyvernOverlayMCPServer:
    return SkyvernOverlayMCPServer(
        None,
        {},
        {},
        frozenset(),
        lambda: SimpleNamespace(browser_session_id=None, organization_id="org"),
    )


@pytest.mark.asyncio
async def test_candidate_guard_uses_disposable_service_worker_blocked_context_for_attached_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    browser_context = _BrowserContext()
    original_page = browser_context.pages[0]
    browser_state = _BrowserState(browser_context, page=original_page)

    ctx = SimpleNamespace(browser_session_id="inherited", organization_id="org")

    async def ensure(fresh_ctx):
        assert fresh_ctx.browser_session_id == "inherited"
        calls.append("ensure")
        return None

    async def resolve(_ctx):
        calls.append("resolve")
        return browser_state

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    close_session = AsyncMock()
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    monkeypatch.setattr(mcp_adapter.app, "PERSISTENT_SESSIONS_MANAGER", SimpleNamespace(close_session=close_session))

    server = _server()
    server._context_provider = lambda: ctx
    async with server.evidence_candidate_navigation_guard("https://public.test"):
        assert browser_state.browser_context is browser_context.browser.candidate_context
        assert browser_state.page is not original_page

    assert calls[:2] == ["ensure", "resolve"]
    assert ctx.browser_session_id == "inherited"
    assert browser_context.browser.new_context_kwargs == {"service_workers": "block"}
    assert browser_state.browser_context is browser_context
    assert browser_state.page is original_page
    assert browser_context.closed is False
    assert browser_context.browser.candidate_context is not None
    assert browser_context.browser.candidate_context.closed is True
    close_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_candidate_guard_isolates_non_pristine_attached_cdp_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_context = _BrowserContext()
    selected_page = SimpleNamespace(url="https://selected.test/")
    newest_page = SimpleNamespace(url="https://newest.test/")
    browser_context.pages = [selected_page, newest_page]
    browser_state = _BrowserState(
        browser_context,
        page=selected_page,
        active_page=selected_page,
        prefer_context_newest_when_unpinned=True,
    )

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return browser_state

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())

    async with _server().evidence_candidate_navigation_guard("https://public.test"):
        assert browser_state.browser_context is browser_context.browser.candidate_context
        assert browser_state.page is not selected_page
        assert browser_state.page is not newest_page

    assert browser_context.browser.new_context_kwargs == {"service_workers": "block"}
    assert browser_context.browser.candidate_context is not None
    assert browser_context.browser.candidate_context.closed is True
    assert browser_state.browser_context is browser_context
    assert await browser_state.get_working_page() is selected_page


@pytest.mark.asyncio
async def test_candidate_guard_supports_persistent_context_without_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent_context = _BrowserContext()
    persistent_context.browser = None
    original_page = SimpleNamespace(url="https://persistent.test/account")
    fallback_browser = _Browser()
    chromium = SimpleNamespace(launch=AsyncMock(return_value=fallback_browser))
    browser_state = _BrowserState(
        persistent_context,
        page=original_page,
        pw=SimpleNamespace(chromium=chromium),
    )

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return browser_state

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())

    async with _server().evidence_candidate_navigation_guard("https://public.test"):
        assert browser_state.browser_context is fallback_browser.candidate_context
        assert browser_state.page is not original_page

    chromium.launch.assert_awaited_once_with()
    assert fallback_browser.new_context_kwargs == {"service_workers": "block"}
    assert fallback_browser.closed is True
    assert persistent_context.closed is False
    assert browser_state.browser_context is persistent_context
    assert browser_state.page is original_page


@pytest.mark.asyncio
async def test_candidate_guard_closes_fallback_browser_when_context_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent_context = _BrowserContext()
    persistent_context.browser = None
    fallback_browser = _Browser()
    fallback_browser.new_context = AsyncMock(side_effect=RuntimeError("context creation failed"))
    browser_state = _BrowserState(
        persistent_context,
        pw=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=fallback_browser))),
    )

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return browser_state

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())

    with pytest.raises(RuntimeError, match="context creation failed"):
        async with _server().evidence_candidate_navigation_guard("https://public.test"):
            pass

    assert fallback_browser.closed is True
    assert browser_state.browser_context is persistent_context


@pytest.mark.asyncio
async def test_candidate_guard_closes_fallback_browser_when_candidate_context_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistent_context = _BrowserContext()
    persistent_context.browser = None
    candidate_context = _BrowserContext()
    candidate_context.close = AsyncMock(side_effect=RuntimeError("context close failed"))
    fallback_browser = _Browser()
    fallback_browser.new_context = AsyncMock(return_value=candidate_context)
    browser_state = _BrowserState(
        persistent_context,
        pw=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=fallback_browser))),
    )

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return browser_state

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())

    with pytest.raises(RuntimeError, match="context close failed"):
        async with _server().evidence_candidate_navigation_guard("https://public.test"):
            pass

    assert fallback_browser.closed is True
    assert browser_state.browser_context is persistent_context


@pytest.mark.asyncio
async def test_candidate_guard_isolates_inherited_page_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_context = _BrowserContext()
    browser_context.browser = None
    browser_context.pages = [SimpleNamespace(url="https://authenticated.test/account")]
    fallback_browser = _Browser()

    async def ensure(_ctx):
        return None

    original_page = SimpleNamespace(url="https://authenticated.test/account")
    browser_state = _BrowserState(
        browser_context,
        page=original_page,
        pw=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=fallback_browser))),
    )

    async def resolve(_ctx):
        return browser_state

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())

    async with _server().evidence_candidate_navigation_guard("https://public.test"):
        assert browser_state.browser_context is not browser_context
        assert all(page.url in {"", "about:blank"} for page in browser_state.browser_context.pages)

    assert browser_state.browser_context is browser_context
    assert browser_state.page is original_page
    assert fallback_browser.new_context_kwargs == {"service_workers": "block"}
    assert fallback_browser.closed is True


@pytest.mark.asyncio
async def test_candidate_guard_uses_terminal_agent_function_route_without_overlay_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_context = _BrowserContext()

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context)

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())

    agent_function = _AgentFunction()
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", agent_function)

    async with _server().evidence_candidate_navigation_guard("https://public.test"):
        assert agent_function.active is True
        assert browser_context.handler is None

    assert agent_function.idle_waits == 1


@pytest.mark.asyncio
async def test_candidate_guard_clears_state_when_network_context_entry_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_context = _BrowserContext()
    server = _server()

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context)

    class FailingAgentFunction(_AgentFunction):
        @asynccontextmanager
        async def copilot_candidate_network_guard(self, _browser_context, *, expected_origin: str):
            raise RuntimeError("entry failed")
            yield []

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", FailingAgentFunction())

    with pytest.raises(RuntimeError, match="entry failed"):
        async with server.evidence_candidate_navigation_guard("https://public.test"):
            pass

    assert server._evidence_candidate_origin is None
    assert server._evidence_candidate_guarded_hops is None


@pytest.mark.asyncio
async def test_candidate_guard_clears_state_when_idle_wait_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_context = _BrowserContext()
    server = _server()

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context)

    agent_function = _AgentFunction()

    async def fail_idle(_browser_context) -> None:
        raise TimeoutError("idle timeout")

    agent_function.wait_for_copilot_candidate_network_idle = fail_idle
    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", agent_function)

    with pytest.raises(TimeoutError, match="idle timeout"):
        async with server.evidence_candidate_navigation_guard("https://public.test"):
            pass

    assert server._evidence_candidate_origin is None
    assert server._evidence_candidate_guarded_hops is None


@pytest.mark.asyncio
async def test_candidate_browser_url_uses_last_enforced_document_not_subresource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_context = _BrowserContext()
    server = _server()
    agent_function = _AgentFunction()

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context)

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", agent_function)

    async with server.evidence_candidate_navigation_guard("https://public.test") as hops:
        hops.append(
            {
                "url": "https://public.test/favicon.ico",
                "resource_type": "image",
                "resolved_public_ips": ["93.184.216.34"],
                "connected_peer_ip": "93.184.216.34",
                "enforcement_version": "copilot_candidate_preconnect_v1",
            }
        )
        assert await server.evidence_candidate_browser_url() == "https://public.test/start"


@pytest.mark.asyncio
async def test_candidate_browser_url_rejects_fragment_not_in_enforced_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_context = _BrowserContext()
    server = _server()
    agent_function = _AgentFunction()

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context, page=SimpleNamespace(url="https://public.test/start#after"))

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", agent_function)

    async with server.evidence_candidate_navigation_guard("https://public.test"):
        with pytest.raises(RuntimeError, match="candidate_browser_url_not_peer_verified"):
            await server.evidence_candidate_browser_url()


@pytest.mark.asyncio
async def test_candidate_guard_rejects_non_exact_origin_before_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    browser_context = _BrowserContext()

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context)

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    with pytest.raises(ValueError, match="exact HTTPS origin"):
        async with _server().evidence_candidate_navigation_guard("https://public.test/path"):
            pass


@pytest.mark.asyncio
async def test_real_adapter_internal_call_drains_candidate_network_before_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_context = _BrowserContext()
    agent_function = _AgentFunction()
    server = _server()
    server._context_provider = lambda: SimpleNamespace(browser_session_id="session", organization_id="org")
    server._client = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(structured_content={"ok": True}, is_error=False, content=[]))
    )

    async def ensure(_ctx):
        return None

    async def resolve(_ctx):
        return _BrowserState(browser_context)

    @asynccontextmanager
    async def browser_scope(_ctx):
        yield

    monkeypatch.setattr(mcp_adapter, "ensure_browser_session", ensure)
    monkeypatch.setattr(mcp_adapter, "resolve_browser_state_for_context", resolve)
    monkeypatch.setattr(mcp_adapter, "mcp_browser_context", browser_scope)
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", agent_function)

    async with server.evidence_candidate_navigation_guard("https://public.test"):
        result = await server.call_internal_tool("skyvern_navigate", {"url": "https://public.test/start"})

    assert result["ok"] is True
    assert agent_function.idle_waits == 2
