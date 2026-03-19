import importlib.util
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.workflow.schedules import compute_previous_fire_time

_skip_no_local_schedules = pytest.mark.skipif(
    importlib.util.find_spec("scripts.run_local_schedules") is None,
    reason="scripts.run_local_schedules not available (cloud-only)",
)


class TestComputePreviousFireTime:
    def test_returns_past_time(self) -> None:
        """compute_previous_fire_time should return a time before now."""
        result = compute_previous_fire_time("*/5 * * * *", "UTC")
        assert result < datetime.now(UTC)

    def test_returns_utc(self) -> None:
        result = compute_previous_fire_time("0 * * * *", "UTC")
        assert result.tzinfo is not None

    def test_respects_timezone(self) -> None:
        """Results for different timezones may differ."""
        utc_result = compute_previous_fire_time("0 0 * * *", "UTC")
        tokyo_result = compute_previous_fire_time("0 0 * * *", "Asia/Tokyo")
        # Both are valid datetimes in UTC; they differ because midnight
        # in Tokyo and midnight in UTC are 9 hours apart.
        assert utc_result != tokyo_result

    def test_every_5_min_within_5_minutes(self) -> None:
        """For a */5 cron, the previous fire time should be within 5 minutes of now."""
        result = compute_previous_fire_time("*/5 * * * *", "UTC")
        now = datetime.now(UTC)
        assert now - result <= timedelta(minutes=5)


@_skip_no_local_schedules
class TestShouldFire:
    @pytest.mark.asyncio
    async def test_true_when_no_recent_run(self) -> None:
        """should_fire returns True when has_schedule_fired_since returns False."""
        schedule = SimpleNamespace(
            workflow_schedule_id="ws_test",
            cron_expression="*/5 * * * *",
            timezone="UTC",
        )
        fake_db = SimpleNamespace(
            has_schedule_fired_since=AsyncMock(return_value=False),
        )

        with patch("scripts.run_local_schedules.app") as mock_app:
            mock_app.DATABASE = fake_db
            from scripts.run_local_schedules import should_fire

            result = await should_fire(schedule)

        assert result is True
        fake_db.has_schedule_fired_since.assert_called_once()

    @pytest.mark.asyncio
    async def test_false_when_run_exists_in_window(self) -> None:
        """should_fire returns False when a run already exists for this window."""
        schedule = SimpleNamespace(
            workflow_schedule_id="ws_test",
            cron_expression="*/5 * * * *",
            timezone="UTC",
        )
        fake_db = SimpleNamespace(
            has_schedule_fired_since=AsyncMock(return_value=True),
        )

        with patch("scripts.run_local_schedules.app") as mock_app:
            mock_app.DATABASE = fake_db
            from scripts.run_local_schedules import should_fire

            result = await should_fire(schedule)

        assert result is False


@_skip_no_local_schedules
class TestFireScheduleAuthCheck:
    @pytest.mark.asyncio
    async def test_skips_without_auth_token(self) -> None:
        """fire_schedule raises RuntimeError when org has no valid auth token."""
        schedule = SimpleNamespace(
            workflow_schedule_id="ws_no_auth",
            organization_id="org_no_auth",
            workflow_permanent_id="wpid_no_auth",
            cron_expression="*/5 * * * *",
            timezone="UTC",
            parameters=None,
        )
        fake_db = SimpleNamespace(
            get_valid_org_auth_token=AsyncMock(return_value=None),
        )

        with patch("scripts.run_local_schedules.app") as mock_app:
            mock_app.DATABASE = fake_db
            from scripts.run_local_schedules import fire_schedule

            with pytest.raises(RuntimeError, match="No valid auth token"):
                await fire_schedule(schedule)

        fake_db.get_valid_org_auth_token.assert_called_once_with(
            organization_id="org_no_auth",
            token_type="api",
        )


@_skip_no_local_schedules
class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_fire(self) -> None:
        """poll_once with dry_run=True should not call fire_schedule."""
        schedule = SimpleNamespace(
            workflow_schedule_id="ws_dry",
            workflow_permanent_id="wpid_dry",
            cron_expression="*/5 * * * *",
            timezone="UTC",
            organization_id="org_dry",
            parameters=None,
        )
        fake_db = SimpleNamespace(
            get_all_enabled_schedules=AsyncMock(return_value=[schedule]),
            has_schedule_fired_since=AsyncMock(return_value=False),
        )

        with (
            patch("scripts.run_local_schedules.app") as mock_app,
            patch("scripts.run_local_schedules.fire_schedule") as mock_fire,
        ):
            mock_app.DATABASE = fake_db
            from scripts.run_local_schedules import poll_once

            result = await poll_once(dry_run=True)

        assert result == []
        mock_fire.assert_not_called()
