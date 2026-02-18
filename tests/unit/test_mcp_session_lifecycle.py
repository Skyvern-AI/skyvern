from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.cli.core import client as client_mod
from skyvern.cli.core import session_manager
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_ops import SessionCloseResult
from skyvern.cli.mcp_tools import session as mcp_session


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    client_mod._skyvern_instance.set(None)
    client_mod._global_skyvern_instance = None

    session_manager._current_session.set(None)
    session_manager._global_session = None
    mcp_session.set_current_session(mcp_session.SessionState())


def test_get_skyvern_reuses_global_instance_across_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []

    class FakeSkyvern:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created.append(self)

        @classmethod
        def local(cls) -> FakeSkyvern:
            return cls()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(client_mod, "Skyvern", FakeSkyvern)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_API_KEY", None)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_BASE_URL", None)

    first = client_mod.get_skyvern()
    client_mod._skyvern_instance.set(None)  # Simulate a new async context.
    second = client_mod.get_skyvern()

    assert first is second
    assert len(created) == 1


@pytest.mark.asyncio
async def test_close_skyvern_closes_singleton() -> None:
    fake = MagicMock()
    fake.aclose = AsyncMock()

    client_mod._skyvern_instance.set(fake)
    client_mod._global_skyvern_instance = fake

    await client_mod.close_skyvern()

    fake.aclose.assert_awaited_once()
    assert client_mod._skyvern_instance.get() is None
    assert client_mod._global_skyvern_instance is None


def test_get_current_session_falls_back_to_global_state() -> None:
    state = session_manager.SessionState(
        browser=MagicMock(),
        context=BrowserContext(mode="cloud_session", session_id="pbs_123"),
    )
    session_manager.set_current_session(state)

    session_manager._current_session.set(None)  # Simulate a new async context.
    recovered = session_manager.get_current_session()

    assert recovered is state


@pytest.mark.asyncio
async def test_resolve_browser_reuses_matching_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    current_browser = MagicMock()
    current_state = session_manager.SessionState(
        browser=current_browser,
        context=BrowserContext(mode="cloud_session", session_id="pbs_123"),
    )
    session_manager.set_current_session(current_state)

    fake_skyvern = MagicMock()
    fake_skyvern.connect_to_cloud_browser_session = AsyncMock()
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)

    browser, ctx = await session_manager.resolve_browser(session_id="pbs_123")

    assert browser is current_browser
    assert ctx.session_id == "pbs_123"
    fake_skyvern.connect_to_cloud_browser_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_close_with_matching_session_id_closes_browser_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    current_browser = MagicMock()
    current_browser.close = AsyncMock()
    mcp_session.set_current_session(
        mcp_session.SessionState(
            browser=current_browser,
            context=BrowserContext(mode="cloud_session", session_id="pbs_456"),
        )
    )

    fake_skyvern = MagicMock()
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)

    do_session_close = AsyncMock(return_value=SessionCloseResult(session_id="pbs_456", closed=True))
    monkeypatch.setattr(mcp_session, "do_session_close", do_session_close)

    result = await mcp_session.skyvern_session_close(session_id="pbs_456")

    assert result["ok"] is True
    assert result["data"] == {"session_id": "pbs_456", "closed": True}
    current_browser.close.assert_awaited_once()
    do_session_close.assert_awaited_once_with(fake_skyvern, "pbs_456")
    assert mcp_session.get_current_session().browser is None


@pytest.mark.asyncio
async def test_session_close_chains_exceptions_when_both_api_and_browser_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both do_session_close (API) and browser.close() raise, the browser
    exception should chain the API exception via __cause__ so neither is lost."""
    current_browser = MagicMock()
    browser_error = RuntimeError("browser close failed")
    current_browser.close = AsyncMock(side_effect=browser_error)
    mcp_session.set_current_session(
        mcp_session.SessionState(
            browser=current_browser,
            context=BrowserContext(mode="cloud_session", session_id="pbs_dual"),
        )
    )

    fake_skyvern = MagicMock()
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)

    api_error = ConnectionError("API close failed")
    do_session_close = AsyncMock(side_effect=api_error)
    monkeypatch.setattr(mcp_session, "do_session_close", do_session_close)

    result = await mcp_session.skyvern_session_close(session_id="pbs_dual")

    # The outer exception handler catches and returns an error result
    assert result["ok"] is False
    assert "browser close failed" in result["error"]["message"]
    # Session state is cleaned up regardless
    assert mcp_session.get_current_session().browser is None


@pytest.mark.asyncio
async def test_session_close_matching_context_without_browser_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_session.set_current_session(
        mcp_session.SessionState(
            browser=None,
            context=BrowserContext(mode="cloud_session", session_id="pbs_999"),
        )
    )

    fake_skyvern = MagicMock()
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)

    do_session_close = AsyncMock(return_value=SessionCloseResult(session_id="pbs_999", closed=True))
    monkeypatch.setattr(mcp_session, "do_session_close", do_session_close)

    result = await mcp_session.skyvern_session_close(session_id="pbs_999")

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_session.ErrorCode.SDK_ERROR
    assert "Expected active browser for matching cloud session" in result["error"]["message"]
    do_session_close.assert_awaited_once_with(fake_skyvern, "pbs_999")
    assert mcp_session.get_current_session().context is None


# ---------------------------------------------------------------------------
# Tests for close_current_session() — cloud session API cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_current_session_calls_api_close_for_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """close_current_session() should call do_session_close for cloud sessions
    and clear _browser_session_id to avoid a duplicate API call from browser.close()."""
    browser = MagicMock()
    browser.close = AsyncMock()
    browser._browser_session_id = "pbs_api"
    session_manager.set_current_session(
        session_manager.SessionState(
            browser=browser,
            context=BrowserContext(mode="cloud_session", session_id="pbs_api"),
        )
    )

    fake_skyvern = MagicMock()
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)

    do_session_close = AsyncMock(return_value=SessionCloseResult(session_id="pbs_api", closed=True))
    monkeypatch.setattr("skyvern.cli.core.session_ops.do_session_close", do_session_close)

    await session_manager.close_current_session()

    do_session_close.assert_awaited_once_with(fake_skyvern, "pbs_api")
    browser.close.assert_awaited_once()
    # _browser_session_id should be cleared to prevent redundant API call
    assert browser._browser_session_id is None
    assert session_manager.get_current_session().browser is None


@pytest.mark.asyncio
async def test_close_current_session_skips_api_close_for_local_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """close_current_session() should NOT call do_session_close for local sessions."""
    browser = MagicMock()
    browser.close = AsyncMock()
    session_manager.set_current_session(
        session_manager.SessionState(
            browser=browser,
            context=BrowserContext(mode="local"),
        )
    )

    do_session_close = AsyncMock()
    monkeypatch.setattr("skyvern.cli.core.session_ops.do_session_close", do_session_close)

    await session_manager.close_current_session()

    do_session_close.assert_not_awaited()
    browser.close.assert_awaited_once()
    assert session_manager.get_current_session().browser is None


@pytest.mark.asyncio
async def test_close_current_session_still_closes_browser_when_api_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When do_session_close raises, browser.close() should still run and state should be cleared."""
    browser = MagicMock()
    browser.close = AsyncMock()
    browser._browser_session_id = "pbs_fail"
    session_manager.set_current_session(
        session_manager.SessionState(
            browser=browser,
            context=BrowserContext(mode="cloud_session", session_id="pbs_fail"),
        )
    )

    fake_skyvern = MagicMock()
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)

    do_session_close = AsyncMock(side_effect=ConnectionError("API unreachable"))
    monkeypatch.setattr("skyvern.cli.core.session_ops.do_session_close", do_session_close)

    await session_manager.close_current_session()

    do_session_close.assert_awaited_once_with(fake_skyvern, "pbs_fail")
    # browser.close() should still be called despite API failure
    browser.close.assert_awaited_once()
    # _browser_session_id should NOT be cleared (API close failed, let browser.close() try)
    assert browser._browser_session_id == "pbs_fail"
    assert session_manager.get_current_session().browser is None
