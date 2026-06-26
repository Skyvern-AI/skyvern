"""Tests for verify_browser_session — local-mode short-circuit (SKY-8017)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.routes.streaming.verify import verify_browser_session
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
)


def _make_session(*, browser_address: str | None, status: PersistentBrowserSessionStatus) -> PersistentBrowserSession:
    now = datetime.now(timezone.utc)
    return PersistentBrowserSession(
        persistent_browser_session_id="bs_test",
        organization_id="o_test",
        status=status,
        browser_address=browser_address,
        created_at=now,
        modified_at=now,
    )


class TestVerifyBrowserSessionLocalShortCircuit:
    @pytest.mark.asyncio
    async def test_short_circuits_in_cdp_mode_with_no_address(self) -> None:
        # Local CDP mode: browser runs in-process, browser_address is never
        # populated. Verify must not block on the address wait.
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait for address in cdp mode"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "cdp"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == ""
        assert result.persistent_browser_session_id == "bs_test"
        manager.get_browser_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_checks_address_readiness_in_non_cdp_mode(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address_if_ready = AsyncMock(return_value="ws://remote:9222")
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not enter blocking address wait"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "local"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == "ws://remote:9222"
        manager.get_browser_address_if_ready.assert_awaited_once_with(session_id="bs_test", organization_id="o_test")
        manager.get_browser_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocks_until_address_ready_in_non_local_non_cdp_mode(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(return_value="ws://remote:9222")
        manager.get_browser_address_if_ready = AsyncMock(
            side_effect=AssertionError("must not use non-blocking address check in production"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "production"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == "ws://remote:9222"
        manager.get_browser_address.assert_awaited_once_with(session_id="bs_test", organization_id="o_test")
        manager.get_browser_address_if_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_address_not_ready_in_non_cdp_mode(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address_if_ready = AsyncMock(return_value=None)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not enter blocking address wait"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            settings_mock.ENV = "local"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is None
        manager.get_browser_address_if_ready.assert_awaited_once_with(session_id="bs_test", organization_id="o_test")
        manager.get_browser_address.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_existing_address_without_polling(self) -> None:
        session = _make_session(
            browser_address="ws://remote:9222",
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait when address present"),
        )

        with patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock:
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == "ws://remote:9222"
