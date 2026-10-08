from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.webeye.real_browser_state import RealBrowserState


class _FakePage:
    def __init__(self, context: _BrowserContext, url: str = "about:blank") -> None:
        self.context = context
        self.url = url
        self.handlers: dict[str, list] = {}
        self.closed = False
        self.video = None

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    def crash(self) -> None:
        for handler in self.handlers.get("crash", []):
            handler(self)


class _BrowserContext:
    def __init__(self, browser: _Browser | None = None) -> None:
        self.pages = [_FakePage(self)]
        self.service_workers = []
        self.browser = browser or _Browser()
        self.closed = False

    def on(self, _event: str, _handler) -> None:
        return None

    async def new_page(self) -> _FakePage:
        page = _FakePage(self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self) -> None:
        self.closed = False
        self.new_context_kwargs: dict[str, str] | None = None
        self.candidate_context: _BrowserContext | None = None

    async def new_context(self, **kwargs: str) -> _BrowserContext:
        self.new_context_kwargs = kwargs
        self.candidate_context = _BrowserContext(self)
        return self.candidate_context

    def is_connected(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True


class _AgentFunction:
    async def setup_browser_context_extensions(self, _browser_context, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_candidate_context_swap_arms_crash_reaping_on_the_candidate_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    original = _BrowserContext()
    original_page = original.pages[0]
    state = RealBrowserState(pw=SimpleNamespace(), browser_context=original, page=original_page)

    async with mcp_adapter.service_worker_blocked_context(state, organization_id="org") as candidate:
        candidate_page = candidate.pages[-1]
        assert candidate_page in state._crash_listener_pages
        candidate_page.crash()
        await asyncio.gather(*list(state._detached_teardown_tasks))
        assert candidate_page.closed is True


@pytest.mark.asyncio
async def test_candidate_context_restore_keeps_crash_reaping_on_the_original_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    original = _BrowserContext()
    original_page = original.pages[0]
    state = RealBrowserState(pw=SimpleNamespace(), browser_context=original, page=original_page)

    async with mcp_adapter.service_worker_blocked_context(state, organization_id="org"):
        pass

    assert state.browser_context is original
    assert original_page in state._crash_listener_pages
    original_page.crash()
    await asyncio.gather(*list(state._detached_teardown_tasks))
    assert original_page.closed is True


def _persistent_state(fallback_browser: _Browser) -> tuple[RealBrowserState, _BrowserContext, _FakePage, AsyncMock]:
    persistent = _BrowserContext()
    persistent.browser = None
    original_page = persistent.pages[0]
    launch = AsyncMock(return_value=fallback_browser)
    state = RealBrowserState(
        pw=SimpleNamespace(chromium=SimpleNamespace(launch=launch)), browser_context=persistent, page=original_page
    )
    return state, persistent, original_page, launch


@pytest.mark.asyncio
async def test_candidate_context_blocks_service_workers_and_restores_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    original = _BrowserContext()
    original_page = original.pages[0]
    state = RealBrowserState(pw=SimpleNamespace(), browser_context=original, page=original_page)

    async with mcp_adapter.service_worker_blocked_context(state, organization_id="org") as candidate:
        assert candidate is original.browser.candidate_context
        assert state.browser_context is candidate
        assert await state.get_working_page() is not original_page

    assert original.browser.new_context_kwargs == {"service_workers": "block"}
    assert candidate.closed is True
    assert original.closed is False
    assert state.browser_context is original
    assert await state.get_working_page() is original_page


@pytest.mark.asyncio
async def test_candidate_context_launches_and_closes_a_fallback_browser_for_a_persistent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    fallback = _Browser()
    state, persistent, original_page, launch = _persistent_state(fallback)

    async with mcp_adapter.service_worker_blocked_context(state, organization_id="org") as candidate:
        assert candidate is fallback.candidate_context

    launch.assert_awaited_once_with()
    assert fallback.new_context_kwargs == {"service_workers": "block"}
    assert fallback.closed is True
    assert persistent.closed is False
    assert state.browser_context is persistent
    assert await state.get_working_page() is original_page


@pytest.mark.asyncio
async def test_candidate_context_closes_the_fallback_browser_when_context_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    fallback = _Browser()
    fallback.new_context = AsyncMock(side_effect=RuntimeError("context creation failed"))
    state, persistent, _, _ = _persistent_state(fallback)

    with pytest.raises(RuntimeError, match="context creation failed"):
        async with mcp_adapter.service_worker_blocked_context(state, organization_id="org"):
            pass

    assert fallback.closed is True
    assert state.browser_context is persistent


@pytest.mark.asyncio
async def test_candidate_context_closes_the_fallback_browser_when_candidate_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_adapter.app, "AGENT_FUNCTION", _AgentFunction())
    fallback = _Browser()
    candidate = _BrowserContext(fallback)
    candidate.close = AsyncMock(side_effect=RuntimeError("context close failed"))
    fallback.new_context = AsyncMock(return_value=candidate)
    state, persistent, _, _ = _persistent_state(fallback)

    with pytest.raises(RuntimeError, match="context close failed"):
        async with mcp_adapter.service_worker_blocked_context(state, organization_id="org"):
            pass

    assert fallback.closed is True
    assert state.browser_context is persistent
