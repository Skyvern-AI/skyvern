"""Tests for failure-triggered script review daily cap (SKY-8334).

Validates that:
1. Failure reviews are capped at 3 per wpid per day
2. AI fallback reviews (non-failure) are NOT capped
3. Cap is keyed on wpid, not script_id (new script revision doesn't reset)
4. Cap resets on a new day
"""

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


def _make_workflow(wpid: str = "wpid_test123") -> MagicMock:
    wf = MagicMock()
    wf.workflow_permanent_id = wpid
    wf.organization_id = "org_test"
    return wf


def _make_workflow_run(run_id: str = "wr_test1") -> MagicMock:
    wr = MagicMock()
    wr.workflow_run_id = run_id
    return wr


class TestCheckFailureReviewCap:
    """Tests for _check_failure_review_cap."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.service = WorkflowService()
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        yield  # type: ignore[misc]
        CacheFactory.set_cache(self._original_cache)

    @pytest.mark.asyncio
    async def test_cap_not_reached_returns_false(self) -> None:
        """When counter is below 3, review should proceed."""
        self.mock_cache.get = AsyncMock(return_value="2")
        result = await self.service._check_failure_review_cap("wpid_abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_cap_reached_returns_true(self) -> None:
        """When counter is at 3 or above, review should be skipped."""
        self.mock_cache.get = AsyncMock(return_value="3")
        result = await self.service._check_failure_review_cap("wpid_abc")
        assert result is True

    @pytest.mark.asyncio
    async def test_cap_exceeded_returns_true(self) -> None:
        """When counter is above 3, review should be skipped."""
        self.mock_cache.get = AsyncMock(return_value="5")
        result = await self.service._check_failure_review_cap("wpid_abc")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_counter_returns_false(self) -> None:
        """When no counter exists (first review of the day), review should proceed."""
        self.mock_cache.get = AsyncMock(return_value=None)
        result = await self.service._check_failure_review_cap("wpid_abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_cache_error_allows_review(self) -> None:
        """When cache raises an error, review should still proceed (fail open)."""
        self.mock_cache.get = AsyncMock(side_effect=Exception("Redis down"))
        result = await self.service._check_failure_review_cap("wpid_abc")
        assert result is False

    @pytest.mark.asyncio
    async def test_key_uses_wpid_not_script_id(self) -> None:
        """The cap key must use workflow_permanent_id, not script_id."""
        self.mock_cache.get = AsyncMock(return_value="3")
        wpid = "wpid_specific_workflow"
        await self.service._check_failure_review_cap(wpid)

        call_args = self.mock_cache.get.call_args[0][0]
        assert wpid in call_args
        assert "script_reviewer:failure_cap:" in call_args

    @pytest.mark.asyncio
    async def test_key_includes_date(self) -> None:
        """The cap key must include the current date for daily reset."""
        self.mock_cache.get = AsyncMock(return_value=None)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await self.service._check_failure_review_cap("wpid_abc")

        call_args = self.mock_cache.get.call_args[0][0]
        assert today in call_args


class TestIncrementFailureReviewCounter:
    """Tests for _increment_failure_review_counter."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.service = WorkflowService()
        self.mock_cache = AsyncMock()
        self._original_cache = CacheFactory.get_cache()
        CacheFactory.set_cache(self.mock_cache)
        yield  # type: ignore[misc]
        CacheFactory.set_cache(self._original_cache)

    @pytest.mark.asyncio
    async def test_first_increment(self) -> None:
        """First review of the day should set counter to 1."""
        self.mock_cache.get = AsyncMock(return_value=None)
        await self.service._increment_failure_review_counter("wpid_abc")

        self.mock_cache.set.assert_called_once()
        args, kwargs = self.mock_cache.set.call_args
        assert args[1] == "1"
        assert kwargs.get("ex") == timedelta(hours=48)

    @pytest.mark.asyncio
    async def test_subsequent_increment(self) -> None:
        """Second review should increment counter from 1 to 2."""
        self.mock_cache.get = AsyncMock(return_value="1")
        await self.service._increment_failure_review_counter("wpid_abc")

        args, kwargs = self.mock_cache.set.call_args
        assert args[1] == "2"

    @pytest.mark.asyncio
    async def test_cache_error_is_swallowed(self) -> None:
        """Cache errors should not propagate."""
        self.mock_cache.get = AsyncMock(side_effect=Exception("Redis down"))
        # Should not raise
        await self.service._increment_failure_review_counter("wpid_abc")


class TestTriggerScriptReviewerFailureCap:
    """Integration tests for _trigger_script_reviewer failure cap logic."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
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
            "skyvern.forge.sdk.workflow.service.app.DATABASE.is_script_pinned",
            new_callable=AsyncMock,
            return_value=False,
        )
        self._pin_patcher.start()
        yield  # type: ignore[misc]
        self._pin_patcher.stop()
        CacheFactory.set_cache(self._original_cache)

    @pytest.mark.asyncio
    async def test_failure_review_skipped_when_cap_exceeded(self) -> None:
        """Failure-triggered review should be skipped when daily cap is reached."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        # Cap already at 3
        self.mock_cache.get = AsyncMock(return_value="3")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            # Mock _run_reviewer_locked to track if it gets called
            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            # Should NOT have called _run_reviewer_locked
            self.service._run_reviewer_locked.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_review_proceeds_when_under_cap(self) -> None:
        """Failure-triggered review should proceed when under the daily cap."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        # Counter at 1, under cap
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

            # Should have called _run_reviewer_locked
            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_review_not_capped(self) -> None:
        """AI fallback reviews (completed runs) should NEVER be capped."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        # Even with counter at 100, fallback reviews should proceed
        self.mock_cache.get = AsyncMock(return_value="100")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            # completed status = fallback review, NOT failure review
            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.completed
            )

            # Should have called _run_reviewer_locked despite high counter
            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminated_review_not_capped(self) -> None:
        """Terminated run reviews should NOT be capped (only failed runs are capped)."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        self.mock_cache.get = AsyncMock(return_value="100")

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            ctx = MagicMock()
            ctx.script_revision_id = "rev_1"
            ctx.script_id = "script_1"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.terminated
            )

            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_cap_keyed_on_wpid_not_script_id(self) -> None:
        """New script revision (different script_id) for the same wpid should NOT reset the cap."""
        wpid = "wpid_same_workflow"
        workflow = _make_workflow(wpid=wpid)
        workflow_run = _make_workflow_run()

        # Simulate cap at 3 for this wpid
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected_key = f"script_reviewer:failure_cap:{wpid}:{today}"

        async def mock_get(key: str) -> str | None:
            if key == expected_key:
                return "3"
            return None

        self.mock_cache.get = AsyncMock(side_effect=mock_get)

        with patch("skyvern.forge.sdk.core.skyvern_context.current") as mock_ctx:
            # Even with a brand new script_id/revision, the cap should still apply
            ctx = MagicMock()
            ctx.script_revision_id = "rev_brand_new"
            ctx.script_id = "script_brand_new"
            mock_ctx.return_value = ctx

            self.service._run_reviewer_locked = AsyncMock()

            await self.service._trigger_script_reviewer(
                workflow, workflow_run, pre_finally_status=WorkflowRunStatus.failed
            )

            # Should be skipped because the wpid cap is at 3
            self.service._run_reviewer_locked.assert_not_called()

    @pytest.mark.asyncio
    async def test_cap_resets_on_new_day(self) -> None:
        """Cap counter should be specific to the date, so a new day resets it."""
        wpid = "wpid_daily_reset"
        workflow = _make_workflow(wpid=wpid)
        workflow_run = _make_workflow_run()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        expected_key = f"script_reviewer:failure_cap:{wpid}:{today}"

        # Counter is None for today (even though yesterday may have been 3+)
        async def mock_get(key: str) -> str | None:
            if key == expected_key:
                return None  # No counter for today
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

            # Should proceed because today's counter is 0
            self.service._run_reviewer_locked.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_review_increments_counter_after_success(self) -> None:
        """After a successful failure review, the counter should be incremented."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        # Start at 0
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

            # Counter should have been set to "1"
            set_calls = [c for c in self.mock_cache.set.call_args_list if "failure_cap" in c[0][0]]
            assert len(set_calls) == 1
            assert set_calls[0][0][1] == "1"

    @pytest.mark.asyncio
    async def test_fallback_review_does_not_increment_counter(self) -> None:
        """AI fallback reviews should NOT increment the failure counter."""
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

            # No failure_cap set calls
            set_calls = [c for c in self.mock_cache.set.call_args_list if "failure_cap" in str(c)]
            assert len(set_calls) == 0

    @pytest.mark.asyncio
    async def test_lock_error_does_not_increment_counter(self) -> None:
        """When LockError occurs (lock contention), the counter should NOT be incremented."""
        workflow = _make_workflow()
        workflow_run = _make_workflow_run()

        # Counter at 0, under cap
        self.mock_cache.get = AsyncMock(return_value=None)

        # Make the lock raise LockError to simulate contention
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

            # Review should NOT have run (lock contention)
            self.service._run_reviewer_locked.assert_not_called()

            # Counter should NOT have been incremented
            set_calls = [c for c in self.mock_cache.set.call_args_list if "failure_cap" in str(c)]
            assert len(set_calls) == 0
