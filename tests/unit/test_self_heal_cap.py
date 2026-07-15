from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.services import self_heal_cap


class TestSelfHealCap:
    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        yield
        CacheFactory.set_cache(self._original_cache)

    def test_self_heal_daily_cap_key_scopes_org_wpid_and_utc_day(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert (
            self_heal_cap.self_heal_daily_cap_key("wpid_abc", "org_1") == f"self_heal:daily_cap:org_1:wpid_abc:{today}"
        )
        assert self_heal_cap.self_heal_daily_cap_key("wpid_abc") == f"self_heal:daily_cap:global:wpid_abc:{today}"

    @pytest.mark.asyncio
    async def test_check_and_increment_self_heal_cap_acquires_slot_under_cap(self) -> None:
        self.mock_cache.get = AsyncMock(return_value="4")
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value=mock_lock)
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        result = await self_heal_cap.check_and_increment_self_heal_cap(
            workflow_permanent_id="wpid_abc",
            organization_id="org_1",
        )

        assert result == 5
        self.mock_cache.get_lock.assert_called_once_with("self_heal_cap:org_1:wpid_abc", blocking_timeout=2, timeout=5)
        self.mock_cache.set.assert_awaited_once()
        args, kwargs = self.mock_cache.set.call_args
        assert "self_heal:daily_cap:org_1:wpid_abc" in args[0]
        assert args[1] == "5"
        assert kwargs.get("ex") == timedelta(hours=48)

    @pytest.mark.asyncio
    async def test_check_and_increment_self_heal_cap_returns_none_at_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("skyvern.config.settings.SELF_HEAL_DAILY_CAP", 5, raising=False)
        self.mock_cache.get = AsyncMock(return_value="5")
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value=mock_lock)
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        result = await self_heal_cap.check_and_increment_self_heal_cap(workflow_permanent_id="wpid_abc")

        assert result is None
        self.mock_cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_and_increment_self_heal_cap_lock_error_fails_closed(self) -> None:
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(side_effect=self_heal_cap._RedisLockError("busy"))
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        result = await self_heal_cap.check_and_increment_self_heal_cap(
            workflow_permanent_id="wpid_abc",
            organization_id="org_1",
        )

        assert result is None
        self.mock_cache.get.assert_not_called()
        self.mock_cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_and_increment_self_heal_cap_unconfigured_cache_fails_open(self) -> None:
        CacheFactory.set_cache(None)

        result = await self_heal_cap.check_and_increment_self_heal_cap(workflow_permanent_id="wpid_abc")

        assert result == 1
