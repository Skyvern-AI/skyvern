"""Tests for verify_browser_session — local-mode short-circuit (SKY-8017)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.routes.streaming.verify import verify_browser_session
from skyvern.forge.sdk.schemas.persistent_browser_sessions import (
    PersistentBrowserSession,
    PersistentBrowserSessionStatus,
)
from skyvern.schemas.browser_session_timeouts import MAX_LIFETIME_SECONDS


def _make_session(
    *,
    browser_address: str | None,
    status: PersistentBrowserSessionStatus,
    started_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    timeout_minutes: int | None = None,
) -> PersistentBrowserSession:
    now = datetime.now(timezone.utc)
    return PersistentBrowserSession(
        persistent_browser_session_id="bs_test",
        organization_id="o_test",
        status=status,
        browser_address=browser_address,
        # The session worker writes both columns in one call, and readiness is keyed on the
        # upstream — a row with an address and no upstream is a shape nothing writes.
        upstream_cdp_url="ws://10.0.0.7:9223/devtools/browser/abc" if browser_address else None,
        created_at=now,
        modified_at=now,
        started_at=started_at,
        last_activity_at=last_activity_at,
        timeout_minutes=timeout_minutes,
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

    @pytest.mark.asyncio
    async def test_past_base_timeout_with_recent_activity_remains_verified(self) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session = _make_session(
            browser_address="ws://remote:9222",
            status=PersistentBrowserSessionStatus.running,
            started_at=now - timedelta(minutes=10),
            last_activity_at=now - timedelta(seconds=5),
            timeout_minutes=5,
        )
        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)

        with patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock:
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None

    @pytest.mark.asyncio
    async def test_recent_activity_cannot_extend_past_hard_cap(self) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        session = _make_session(
            browser_address="ws://remote:9222",
            status=PersistentBrowserSessionStatus.running,
            started_at=now - timedelta(seconds=MAX_LIFETIME_SECONDS + 60),
            last_activity_at=now,
            timeout_minutes=60,
        )
        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)

        with patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock:
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            result = await verify_browser_session("bs_test", "o_test")

        assert result is None


class TestVerifyBrowserSessionPerSessionTransport:
    @pytest.mark.asyncio
    async def test_cdp_transport_bypasses_missing_address_when_global_is_vnc(self) -> None:
        session = _make_session(
            browser_address=None,
            status=PersistentBrowserSessionStatus.running,
        )

        manager = MagicMock()
        manager.get_session = AsyncMock(return_value=session)
        manager.get_browser_address = AsyncMock(
            side_effect=AssertionError("must not wait for address when the session's transport is cdp"),
        )

        with (
            patch("skyvern.forge.sdk.routes.streaming.verify.app") as app_mock,
            patch("skyvern.forge.sdk.routes.streaming.verify.settings") as settings_mock,
        ):
            app_mock.PERSISTENT_SESSIONS_MANAGER = manager
            app_mock.AGENT_FUNCTION.resolve_stream_transport = AsyncMock(return_value="cdp")
            settings_mock.BROWSER_STREAMING_MODE = "vnc"
            result = await verify_browser_session("bs_test", "o_test")

        assert result is not None
        assert result.browser_address == ""
        manager.get_browser_address.assert_not_called()
