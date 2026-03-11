"""Tests for the copilot session registry in session_manager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.cli.core.result import BrowserContext
from skyvern.cli.core.session_manager import (
    SessionState,
    _api_key_hash,
    _copilot_sessions,
    register_copilot_session,
    resolve_browser,
    unregister_copilot_session,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    _copilot_sessions.clear()
    yield
    _copilot_sessions.clear()


def _make_state(session_id: str, api_key: str = "test-key") -> SessionState:
    browser = MagicMock()
    ctx = BrowserContext(mode="cloud_session", session_id=session_id)
    return SessionState(
        browser=browser,
        context=ctx,
        api_key_hash=_api_key_hash(api_key),
    )


@pytest.mark.asyncio
async def test_registry_fallback_returns_registered_session():
    state = _make_state("sess-123", api_key="my-key")
    register_copilot_session("sess-123", state)

    with (
        patch("skyvern.cli.core.session_manager.get_skyvern") as mock_get_skyvern,
        patch("skyvern.cli.core.session_manager.get_active_api_key", return_value="my-key"),
    ):
        mock_skyvern = MagicMock()
        mock_skyvern.connect_to_cloud_browser_session = AsyncMock()
        mock_get_skyvern.return_value = mock_skyvern

        browser, ctx = await resolve_browser(session_id="sess-123")

        assert browser is state.browser
        assert ctx is state.context
        mock_skyvern.connect_to_cloud_browser_session.assert_not_called()


@pytest.mark.asyncio
async def test_registry_api_key_mismatch_falls_through():
    state = _make_state("sess-456", api_key="key-a")
    register_copilot_session("sess-456", state)

    with (
        patch("skyvern.cli.core.session_manager.get_skyvern") as mock_get_skyvern,
        patch("skyvern.cli.core.session_manager.get_active_api_key", return_value="key-b"),
    ):
        mock_browser = MagicMock()
        mock_skyvern = MagicMock()
        mock_skyvern.connect_to_cloud_browser_session = AsyncMock(return_value=mock_browser)
        mock_get_skyvern.return_value = mock_skyvern

        browser, ctx = await resolve_browser(session_id="sess-456")

        mock_skyvern.connect_to_cloud_browser_session.assert_called_once_with("sess-456")
        assert browser is mock_browser


def test_lifecycle_register_and_unregister():
    state = _make_state("sess-789")
    register_copilot_session("sess-789", state)
    assert "sess-789" in _copilot_sessions
    assert _copilot_sessions["sess-789"] is state

    unregister_copilot_session("sess-789")
    assert "sess-789" not in _copilot_sessions
