"""Tests for script review daily cap.

Validates that:
1. ALL script reviews (fallback + failure) are capped at SCRIPT_REVIEW_DAILY_CAP per wpid per day
2. Cap is keyed on wpid, not script_id (new script revision doesn't reset)
3. Cap resets on a new day
4. Counter only increments after a review actually runs (not on LockError)
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    from redis.exceptions import LockError
except ImportError:

    class LockError(Exception):  # type: ignore[no-redef]
        pass


from skyvern.forge.sdk.cache.factory import CacheFactory
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.services import script_review_cap


def _make_workflow(wpid: str = "wpid_test123") -> MagicMock:
    wf = MagicMock()
    wf.workflow_permanent_id = wpid
    wf.organization_id = "org_test"
    return wf


def _make_workflow_run(run_id: str = "wr_test1") -> MagicMock:
    wr = MagicMock()
    wr.workflow_run_id = run_id
    return wr


class TestScriptReviewCapModule:
    """Direct contract tests for skyvern.services.script_review_cap."""

    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        yield
        CacheFactory.set_cache(self._original_cache)

    def test_exported_surface_is_intentional(self) -> None:
        assert set(script_review_cap.__all__) == {
            "CapGetter",
            "ReviewerVersion",
            "check_and_increment_cap_v3",
            "get_script_review_cap",
            "increment_script_review_counter_v2",
            "is_script_review_cap_exceeded_v2",
            "is_script_review_cap_exceeded_v3",
            "try_increment_script_review_counter_v3",
            "v2_script_review_cap_key",
            "v3_script_review_cap_key",
        }

    def test_key_constructors_include_version_prefix_wpid_and_date(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        assert script_review_cap.v2_script_review_cap_key("wpid_abc") == (f"script_reviewer:daily_cap:wpid_abc:{today}")
        assert script_review_cap.v3_script_review_cap_key("wpid_abc") == (f"script_review_counter:v3:wpid_abc:{today}")

    def test_private_version_router_rejects_unknown_versions(self) -> None:
        assert "script_reviewer:daily_cap:" in script_review_cap._cap_key_for_version("wpid_abc", "v2")
        assert "script_review_counter:v3:" in script_review_cap._cap_key_for_version("wpid_abc", "v3")

        with pytest.raises(ValueError):
            script_review_cap._cap_key_for_version("wpid_abc", "bogus")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_direct_read_check_uses_injected_cap_getter(self) -> None:
        self.mock_cache.get = AsyncMock(return_value="4")
        cap_getter = AsyncMock(return_value=5)

        result = await script_review_cap.is_script_review_cap_exceeded_v2(
            workflow_permanent_id="wpid_abc",
            organization_id="org_1",
            cap_getter=cap_getter,
        )

        assert result is False
        cap_getter.assert_awaited_once_with("org_1")

    @pytest.mark.asyncio
    async def test_direct_read_check_at_cap_returns_true(self) -> None:
        self.mock_cache.get = AsyncMock(return_value="5")

        result = await script_review_cap.is_script_review_cap_exceeded_v3(
            workflow_permanent_id="wpid_abc",
            cap_getter=AsyncMock(return_value=5),
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_direct_read_check_cache_missing_honors_fail_mode(self) -> None:
        CacheFactory.set_cache(None)

        assert (
            await script_review_cap.is_script_review_cap_exceeded_v2(
                workflow_permanent_id="wpid_abc",
                fail_closed=False,
            )
            is False
        )
        assert (
            await script_review_cap.is_script_review_cap_exceeded_v2(
                workflow_permanent_id="wpid_abc",
                fail_closed=True,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_direct_read_check_cache_error_honors_fail_mode(self) -> None:
        self.mock_cache.get = AsyncMock(side_effect=RuntimeError("redis down"))

        assert (
            await script_review_cap.is_script_review_cap_exceeded_v3(
                workflow_permanent_id="wpid_abc",
                fail_closed=False,
            )
            is False
        )
        assert (
            await script_review_cap.is_script_review_cap_exceeded_v3(
                workflow_permanent_id="wpid_abc",
                fail_closed=True,
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_direct_v3_check_and_increment_acquires_slot_under_cap(self) -> None:
        self.mock_cache.get = AsyncMock(return_value="4")
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value=mock_lock)
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        result = await script_review_cap.check_and_increment_cap_v3(
            workflow_permanent_id="wpid_abc",
            organization_id="org_1",
            cap_getter=AsyncMock(return_value=5),
        )

        assert result == 5
        self.mock_cache.get_lock.assert_called_once_with("v3_cap:wpid_abc", blocking_timeout=2, timeout=5)
        self.mock_cache.set.assert_awaited_once()
        args, kwargs = self.mock_cache.set.call_args
        assert "script_review_counter:v3:wpid_abc" in args[0]
        assert args[1] == "5"
        assert kwargs.get("ex") == timedelta(hours=48)

    @pytest.mark.asyncio
    async def test_direct_v3_check_and_increment_returns_none_at_cap(self) -> None:
        self.mock_cache.get = AsyncMock(return_value="5")
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value=mock_lock)
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        result = await script_review_cap.check_and_increment_cap_v3(
            workflow_permanent_id="wpid_abc",
            cap_getter=AsyncMock(return_value=5),
        )

        assert result is None
        self.mock_cache.set.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_v3_check_and_increment_lock_error_fails_closed(self) -> None:
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(side_effect=script_review_cap._RedisLockError("busy"))
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        result = await script_review_cap.check_and_increment_cap_v3(
            workflow_permanent_id="wpid_abc",
            cap_getter=AsyncMock(return_value=5),
        )

        assert result is None
        self.mock_cache.get.assert_not_called()
        self.mock_cache.set.assert_not_called()


class TestCheckScriptReviewCap:
    """Tests for _check_script_review_cap."""

    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        self.service = WorkflowService()
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        yield
        CacheFactory.set_cache(self._original_cache)

    @pytest.mark.asyncio
    async def test_cap_not_reached_returns_false(self) -> None:
        """When counter is below cap, review should proceed."""
        self.mock_cache.get = AsyncMock(return_value="4")
        result = await self.service._check_script_review_cap("wpid_abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_cap_reached_returns_true(self) -> None:
        """When counter is at cap (5), review should be skipped."""
        self.mock_cache.get = AsyncMock(return_value="5")
        result = await self.service._check_script_review_cap("wpid_abc")
        assert result is True

    @pytest.mark.asyncio
    async def test_cap_exceeded_returns_true(self) -> None:
        """When counter is above cap, review should be skipped."""
        self.mock_cache.get = AsyncMock(return_value="10")
        result = await self.service._check_script_review_cap("wpid_abc")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_counter_returns_false(self) -> None:
        """When no counter exists (first review of the day), review should proceed."""
        self.mock_cache.get = AsyncMock(return_value=None)
        result = await self.service._check_script_review_cap("wpid_abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_cache_error_allows_review(self) -> None:
        """When cache raises an error, review should still proceed (fail open)."""
        self.mock_cache.get = AsyncMock(side_effect=Exception("Redis down"))
        result = await self.service._check_script_review_cap("wpid_abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_key_uses_wpid_not_script_id(self) -> None:
        """The cap key must use workflow_permanent_id, not script_id."""
        self.mock_cache.get = AsyncMock(return_value="5")
        wpid = "wpid_specific_workflow"
        await self.service._check_script_review_cap(wpid)

        call_args = self.mock_cache.get.call_args[0][0]
        assert wpid in call_args
        assert "script_reviewer:daily_cap:" in call_args

    @pytest.mark.asyncio
    async def test_key_includes_date(self) -> None:
        """The cap key must include the current date for daily reset."""
        self.mock_cache.get = AsyncMock(return_value=None)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await self.service._check_script_review_cap("wpid_abc")

        call_args = self.mock_cache.get.call_args[0][0]
        assert today in call_args


class TestIncrementScriptReviewCounter:
    """Tests for _increment_script_review_counter."""

    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        self.service = WorkflowService()
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        yield
        CacheFactory.set_cache(self._original_cache)

    @pytest.mark.asyncio
    async def test_first_increment(self) -> None:
        """First review of the day should set counter to 1."""
        self.mock_cache.get = AsyncMock(return_value=None)
        await self.service._increment_script_review_counter("wpid_abc")

        self.mock_cache.set.assert_called_once()
        args, kwargs = self.mock_cache.set.call_args
        assert args[1] == "1"
        assert kwargs.get("ex") == timedelta(hours=48)

    @pytest.mark.asyncio
    async def test_subsequent_increment(self) -> None:
        """Second review should increment counter from 1 to 2."""
        self.mock_cache.get = AsyncMock(return_value="1")
        await self.service._increment_script_review_counter("wpid_abc")

        args, kwargs = self.mock_cache.set.call_args
        assert args[1] == "2"

    @pytest.mark.asyncio
    async def test_cache_error_is_swallowed(self) -> None:
        """Cache errors should not propagate."""
        self.mock_cache.get = AsyncMock(side_effect=Exception("Redis down"))
        # Should not raise
        await self.service._increment_script_review_counter("wpid_abc")


class TestTriggerScriptReviewerCap:
    """Integration tests for _trigger_script_reviewer daily cap logic."""

    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        self.service = WorkflowService()
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        # Mock lock to be a no-op async context manager
        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(return_value=mock_lock)
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)
        # Mock is_script_pinned to return False (not pinned) so tests reach the cap logic
        self._pin_patcher = patch(
            "skyvern.forge.sdk.workflow.service.app.DATABASE.scripts.is_script_pinned",
            new_callable=AsyncMock,
            return_value=False,
        )
        self._pin_patcher.start()
        self._cap_patcher = patch(
            "skyvern.services.script_review_cap.get_script_review_cap",
            new_callable=AsyncMock,
            return_value=5,
        )
        self._cap_patcher.start()
        yield
        self._cap_patcher.stop()
        self._pin_patcher.stop()
        CacheFactory.set_cache(self._original_cache)

    @pytest.mark.asyncio
    async def test_failure_review_skipped_when_cap_exceeded(self) -> None:
        """Failure-triggered review should be skipped when daily cap is reached."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value="5")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            self.service._run_reviewer_locked.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_review_proceeds_when_under_cap(self) -> None:
        """Failure-triggered review should proceed when under the daily cap."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value="1")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_review_proceeds_when_under_cap(self) -> None:
        """AI fallback review (completed run) should proceed when under the daily cap."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value="1")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.completed
            )

            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_review_also_capped(self) -> None:
        """AI fallback reviews (completed runs) should also be capped."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value="5")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.completed
            )

            # Should be skipped — fallback reviews are now capped too
            self.service._run_reviewer_locked.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminated_review_also_capped(self) -> None:
        """Terminated run reviews should also be capped."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value="5")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.terminated
            )

            self.service._run_reviewer_locked.assert_not_called()

    @pytest.mark.asyncio
    async def test_cap_keyed_on_wpid_not_script_id(self) -> None:
        """New script revision (different script_id) for the same wpid should NOT reset the cap."""
        wpid = "wpid_same_workflow"
        workflow = _make_workflow(wpid=wpid)
        workflow_run = _make_workflow_run()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected_key = f"script_reviewer:daily_cap:{wpid}:{today}"

        async def mock_get(key: str) -> str | None:
            if key == expected_key:
                return "5"
            return None

        self.mock_cache.get = AsyncMock(side_effect=mock_get)

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_brand_new"
            ctx.script_id = "script_brand_new"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            self.service._run_reviewer_locked.assert_not_called()

    @pytest.mark.asyncio
    async def test_cap_resets_on_new_day(self) -> None:
        """Cap counter should be specific to the date, so a new day resets it."""
        wpid = "wpid_daily_reset"
        workflow = _make_workflow(wpid=wpid)
        workflow_run = _make_workflow_run()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected_key = f"script_reviewer:daily_cap:{wpid}:{today}"

        async def mock_get(key: str) -> str | None:
            if key == expected_key:
                return None
            return None

        self.mock_cache.get = AsyncMock(side_effect=mock_get)

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_review_increments_counter_after_success(self) -> None:
        """After a successful review, the counter should be incremented."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value=None)

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            set_calls = [c for c in self.mock_cache.set.call_args_list if "daily_cap" in c[0][0]]
            assert len(set_calls) == 1
            assert set_calls[0][0][1] == "1"

    @pytest.mark.asyncio
    async def test_fallback_review_also_increments_counter(self) -> None:
        """AI fallback reviews should also increment the counter."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value=None)

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.completed
            )

            set_calls = [c for c in self.mock_cache.set.call_args_list if "daily_cap" in c[0][0]]
            assert len(set_calls) == 1
            assert set_calls[0][0][1] == "1"

    @pytest.mark.asyncio
    async def test_lock_error_does_not_increment_counter(self) -> None:
        """When LockError occurs (lock contention), the counter should NOT be incremented."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value=None)

        mock_lock = AsyncMock()
        mock_lock.__aenter__ = AsyncMock(side_effect=LockError("Could not acquire lock"))
        mock_lock.__aexit__ = AsyncMock(return_value=False)
        self.mock_cache.get_lock = MagicMock(return_value=mock_lock)

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            self.service._run_reviewer_locked.assert_not_called()

            set_calls = [c for c in self.mock_cache.set.call_args_list if "daily_cap" in str(c)]
            assert len(set_calls) == 0


class TestGetScriptReviewCap:
    """Unit tests for script-review cap experimentation-provider integration."""

    @pytest.fixture(autouse=True)
    def setup(self) -> Iterator[None]:
        from skyvern.forge import app

        self.mock_provider = AsyncMock()
        # AppHolder proxies to _inst; set a mock ForgeApp so attribute access works
        self._mock_app = MagicMock()
        self._mock_app.EXPERIMENTATION_PROVIDER = self.mock_provider
        object.__setattr__(app, "_inst", self._mock_app)
        yield
        object.__setattr__(app, "_inst", None)

    @pytest.mark.asyncio
    async def test_no_organization_id_returns_default(self) -> None:
        assert await script_review_cap.get_script_review_cap(None) == 5

    @pytest.mark.asyncio
    async def test_no_provider_returns_default(self) -> None:
        self._mock_app.EXPERIMENTATION_PROVIDER = None
        assert await script_review_cap.get_script_review_cap("org_1") == 5

    @pytest.mark.asyncio
    async def test_valid_payload_returns_custom_cap(self) -> None:
        self.mock_provider.get_payload_cached = AsyncMock(return_value="20")
        assert await script_review_cap.get_script_review_cap("org_1") == 20

    @pytest.mark.asyncio
    async def test_invalid_payload_returns_default(self) -> None:
        self.mock_provider.get_payload_cached = AsyncMock(return_value="not-a-number")
        assert await script_review_cap.get_script_review_cap("org_1") == 5

    @pytest.mark.asyncio
    async def test_zero_returns_default(self) -> None:
        self.mock_provider.get_payload_cached = AsyncMock(return_value="0")
        assert await script_review_cap.get_script_review_cap("org_1") == 5

    @pytest.mark.asyncio
    async def test_negative_returns_default(self) -> None:
        self.mock_provider.get_payload_cached = AsyncMock(return_value="-5")
        assert await script_review_cap.get_script_review_cap("org_1") == 5

    @pytest.mark.asyncio
    async def test_provider_exception_returns_default(self) -> None:
        self.mock_provider.get_payload_cached = AsyncMock(side_effect=RuntimeError("network"))
        assert await script_review_cap.get_script_review_cap("org_1") == 5

    @pytest.mark.asyncio
    async def test_none_payload_returns_default(self) -> None:
        self.mock_provider.get_payload_cached = AsyncMock(return_value=None)
        assert await script_review_cap.get_script_review_cap("org_1") == 5
