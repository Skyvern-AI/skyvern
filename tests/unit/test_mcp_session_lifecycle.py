from __future__ import annotations

from types import SimpleNamespace
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
    client_mod._api_key_override.set(None)
    client_mod._global_skyvern_instance = None
    client_mod._api_key_clients.clear()

    session_manager._current_session.set(None)
    session_manager._global_session = None
    session_manager.set_stateless_http_mode(False)
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


def test_get_skyvern_reuses_override_instance_per_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    created_keys: list[str] = []

    class FakeSkyvern:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created_keys.append(kwargs["api_key"])

        @classmethod
        def local(cls) -> FakeSkyvern:
            return cls(api_key="local")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(client_mod, "Skyvern", FakeSkyvern)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_API_KEY", None)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_BASE_URL", None)

    token = client_mod.set_api_key_override("sk_key_a")
    try:
        first = client_mod.get_skyvern()
        client_mod._skyvern_instance.set(None)
        second = client_mod.get_skyvern()
    finally:
        client_mod.reset_api_key_override(token)

    assert first is second
    assert created_keys == ["sk_key_a"]


def test_get_skyvern_override_client_cache_uses_lru_eviction(monkeypatch: pytest.MonkeyPatch) -> None:
    created_keys: list[str] = []

    class FakeSkyvern:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created_keys.append(kwargs["api_key"])

        @classmethod
        def local(cls) -> FakeSkyvern:
            return cls(api_key="local")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(client_mod, "Skyvern", FakeSkyvern)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_API_KEY", None)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_BASE_URL", None)
    monkeypatch.setattr(client_mod, "_API_KEY_CLIENT_CACHE_MAX", 2)

    for key in ("sk_key_a", "sk_key_b"):
        token = client_mod.set_api_key_override(key)
        try:
            client_mod.get_skyvern()
        finally:
            client_mod.reset_api_key_override(token)

    # Touch key_a so key_b becomes least-recently-used.
    token = client_mod.set_api_key_override("sk_key_a")
    try:
        client_mod._skyvern_instance.set(None)
        client_mod.get_skyvern()
    finally:
        client_mod.reset_api_key_override(token)

    # Adding key_c should evict key_b.
    token = client_mod.set_api_key_override("sk_key_c")
    try:
        client_mod.get_skyvern()
    finally:
        client_mod.reset_api_key_override(token)

    assert list(client_mod._api_key_clients.keys()) == [
        client_mod._cache_key("sk_key_a"),
        client_mod._cache_key("sk_key_c"),
    ]
    # key_a, key_b, key_c were created exactly once each.
    assert created_keys == ["sk_key_a", "sk_key_b", "sk_key_c"]


def test_get_skyvern_override_cache_closes_evicted_client(monkeypatch: pytest.MonkeyPatch) -> None:
    closed_keys: list[str] = []

    class FakeSkyvern:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.api_key = kwargs["api_key"]

        @classmethod
        def local(cls) -> FakeSkyvern:
            return cls(api_key="local")

        async def aclose(self) -> None:
            closed_keys.append(self.api_key)

    monkeypatch.setattr(client_mod, "Skyvern", FakeSkyvern)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_API_KEY", None)
    monkeypatch.setattr(client_mod.settings, "SKYVERN_BASE_URL", None)
    monkeypatch.setattr(client_mod, "_API_KEY_CLIENT_CACHE_MAX", 1)

    for key in ("sk_key_a", "sk_key_b"):
        token = client_mod.set_api_key_override(key)
        try:
            client_mod.get_skyvern()
        finally:
            client_mod.reset_api_key_override(token)

    assert list(client_mod._api_key_clients.keys()) == [client_mod._cache_key("sk_key_b")]
    assert closed_keys == ["sk_key_a"]


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


def test_get_current_session_stateless_mode_ignores_global_state() -> None:
    global_state = session_manager.SessionState(
        browser=MagicMock(),
        context=BrowserContext(mode="cloud_session", session_id="pbs_999"),
    )
    session_manager._global_session = global_state
    session_manager._current_session.set(None)

    session_manager.set_stateless_http_mode(True)
    try:
        recovered = session_manager.get_current_session()
    finally:
        session_manager.set_stateless_http_mode(False)

    assert recovered is not global_state
    assert recovered.browser is None
    assert recovered.context is None


def test_set_current_session_stateless_mode_does_not_override_global_state() -> None:
    global_state = session_manager.SessionState(
        browser=MagicMock(),
        context=BrowserContext(mode="cloud_session", session_id="pbs_global"),
    )
    session_manager._global_session = global_state
    replacement = session_manager.SessionState(
        browser=MagicMock(),
        context=BrowserContext(mode="cloud_session", session_id="pbs_request"),
    )

    session_manager.set_stateless_http_mode(True)
    try:
        session_manager.set_current_session(replacement)
    finally:
        session_manager.set_stateless_http_mode(False)

    assert session_manager._global_session is global_state
    assert session_manager._current_session.get() is replacement


@pytest.mark.asyncio
async def test_resolve_browser_reuses_matching_cloud_session(monkeypatch: pytest.MonkeyPatch) -> None:
    current_browser = MagicMock()
    current_state = session_manager.SessionState(
        browser=current_browser,
        context=BrowserContext(mode="cloud_session", session_id="pbs_123"),
        api_key_hash=session_manager._api_key_hash(client_mod.get_active_api_key()),
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
async def test_resolve_browser_does_not_reuse_session_for_different_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_browser = MagicMock()
    session_manager.set_current_session(
        session_manager.SessionState(
            browser=current_browser,
            context=BrowserContext(mode="cloud_session", session_id="pbs_123"),
            api_key_hash=session_manager._api_key_hash("sk_key_a"),
        )
    )

    replacement_browser = MagicMock()
    fake_skyvern = MagicMock()
    fake_skyvern.connect_to_cloud_browser_session = AsyncMock(return_value=replacement_browser)
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)

    token = client_mod.set_api_key_override("sk_key_b")
    try:
        browser, ctx = await session_manager.resolve_browser(session_id="pbs_123")
    finally:
        client_mod.reset_api_key_override(token)

    assert browser is replacement_browser
    assert ctx.session_id == "pbs_123"
    fake_skyvern.connect_to_cloud_browser_session.assert_awaited_once_with("pbs_123")


@pytest.mark.asyncio
async def test_resolve_browser_stateless_mode_does_not_write_global_session(monkeypatch: pytest.MonkeyPatch) -> None:
    global_state = session_manager.SessionState(
        browser=MagicMock(),
        context=BrowserContext(mode="cloud_session", session_id="pbs_global"),
    )
    session_manager._global_session = global_state

    replacement_browser = MagicMock()
    fake_skyvern = MagicMock()
    fake_skyvern.connect_to_cloud_browser_session = AsyncMock(return_value=replacement_browser)
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: fake_skyvern)

    session_manager.set_stateless_http_mode(True)
    try:
        browser, ctx = await session_manager.resolve_browser(session_id="pbs_123")
    finally:
        session_manager.set_stateless_http_mode(False)

    assert browser is replacement_browser
    assert ctx.session_id == "pbs_123"
    assert session_manager._global_session is global_state


@pytest.mark.asyncio
async def test_resolve_browser_blocks_implicit_session_in_stateless_mode() -> None:
    session_manager.set_current_session(
        session_manager.SessionState(
            browser=MagicMock(),
            context=BrowserContext(mode="cloud_session", session_id="pbs_123"),
            api_key_hash=session_manager._api_key_hash("sk_key_a"),
        )
    )
    session_manager.set_stateless_http_mode(True)
    try:
        with pytest.raises(session_manager.BrowserNotAvailableError):
            await session_manager.resolve_browser()
    finally:
        session_manager.set_stateless_http_mode(False)


@pytest.mark.asyncio
async def test_resolve_browser_raises_for_invalid_matching_state(monkeypatch: pytest.MonkeyPatch) -> None:
    session_manager.set_current_session(
        session_manager.SessionState(
            browser=None,
            context=BrowserContext(mode="cloud_session", session_id="pbs_123"),
        )
    )

    monkeypatch.setattr(session_manager, "_matches_current", lambda *args, **kwargs: True)
    monkeypatch.setattr(session_manager, "get_skyvern", lambda: MagicMock())

    with pytest.raises(RuntimeError, match="Expected active browser and context"):
        await session_manager.resolve_browser(session_id="pbs_123")


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

    result = await mcp_session.skyvern_browser_session_close(session_id="pbs_456")

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

    result = await mcp_session.skyvern_browser_session_close(session_id="pbs_dual")

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

    result = await mcp_session.skyvern_browser_session_close(session_id="pbs_999")

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


# ---------------------------------------------------------------------------
# Tests for stateless HTTP mode session creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_create_stateless_mode_returns_session_without_persisting_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager.set_stateless_http_mode(True)
    fake_skyvern = MagicMock()
    fake_skyvern.create_browser_session = AsyncMock(return_value=SimpleNamespace(browser_session_id="pbs_abc"))
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)
    do_session_create = AsyncMock()
    monkeypatch.setattr(mcp_session, "do_session_create", do_session_create)

    try:
        result = await mcp_session.skyvern_browser_session_create(timeout=45)
    finally:
        session_manager.set_stateless_http_mode(False)

    assert result["ok"] is True
    assert result["data"] == {"session_id": "pbs_abc", "timeout_minutes": 45}
    do_session_create.assert_not_awaited()
    assert mcp_session.get_current_session().browser is None
    assert mcp_session.get_current_session().context is None


@pytest.mark.asyncio
async def test_session_create_stateless_mode_rejects_local() -> None:
    session_manager.set_stateless_http_mode(True)
    try:
        result = await mcp_session.skyvern_browser_session_create(local=True)
    finally:
        session_manager.set_stateless_http_mode(False)

    assert result["ok"] is False
    assert result["error"]["code"] == mcp_session.ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_session_create_persists_active_api_key_hash_in_session_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_skyvern = MagicMock()
    monkeypatch.setattr(mcp_session, "get_skyvern", lambda: fake_skyvern)

    fake_browser = MagicMock()
    do_session_create = AsyncMock(
        return_value=(
            fake_browser,
            SimpleNamespace(local=False, session_id="pbs_123", timeout_minutes=60, headless=False),
        )
    )
    monkeypatch.setattr(mcp_session, "do_session_create", do_session_create)

    token = client_mod.set_api_key_override("sk_key_create")
    try:
        result = await mcp_session.skyvern_browser_session_create(timeout=60)
    finally:
        client_mod.reset_api_key_override(token)

    assert result["ok"] is True
    current = mcp_session.get_current_session()
    assert current.browser is fake_browser
    assert current.context == BrowserContext(mode="cloud_session", session_id="pbs_123")
    assert current.api_key_hash == session_manager._api_key_hash("sk_key_create")
    assert current.api_key_hash != "sk_key_create"
