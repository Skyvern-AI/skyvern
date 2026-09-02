"""A remote browser can stop answering its control protocol while the page it hosts keeps running.

Every consumer then only sees its own deadline expire — analysis, capture and reload each look
like an ordinary slow page — so the retry ladders keep spending the run's step budget on a browser
that will never answer again (SKY-14033: ~15 minutes of retries, then a failure that blamed the
site). These tests pin the signal that separates the two cases and the point that acts on it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.exceptions import BrowserSessionDegraded, ScrapingFailed, SkyvernPageAnalysisTimeout
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.runs import RunEngine
from skyvern.webeye.browser_health import BrowserHealth, BrowserOperation
from skyvern.webeye.utils.page import SkyvernFrame
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task


def _strike(health: BrowserHealth, operation: BrowserOperation, times: int) -> None:
    for _ in range(times):
        health.record_timeout(operation)


class TestDegradedSignal:
    def test_one_stuck_operation_never_degrades(self) -> None:
        """The whole point of the second-kind requirement: a run against a heavy page can blow the
        5s analysis deadline over and over while the browser is perfectly healthy. Tripping on that
        would fail runs that recover today."""
        health = BrowserHealth()
        _strike(health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES * 3)

        assert not health.is_degraded

    def test_two_stuck_operations_past_the_strike_count_degrades(self) -> None:
        health = BrowserHealth()
        _strike(health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES - 1)
        health.record_timeout(BrowserOperation.SCREENSHOT)

        assert health.is_degraded
        assert health.describe_stuck_operations() == "evaluate, screenshot"

    def test_any_answered_operation_clears_the_tally(self) -> None:
        """A browser that answers anything is still usable, so the count is consecutive, not
        cumulative — otherwise a long run accumulates its way into a false positive."""
        health = BrowserHealth()
        _strike(health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        _strike(health, BrowserOperation.SCREENSHOT, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        assert health.is_degraded

        health.record_success()

        assert not health.is_degraded
        assert health.consecutive_timeouts == 0

    def test_a_recovered_operation_gives_back_only_its_own_strike(self) -> None:
        """A capture rescued over a second route proves the browser answers, but not that the
        operations still stuck have recovered; those strikes must survive the rescue."""
        health = BrowserHealth()
        _strike(health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        _strike(health, BrowserOperation.RELOAD, 1)
        health.record_timeout(BrowserOperation.SCREENSHOT)
        assert health.is_degraded

        health.record_recovery(BrowserOperation.SCREENSHOT)

        assert health.is_degraded, "evaluate and reload are still unanswered"
        assert health.stuck_operations == {BrowserOperation.EVALUATE, BrowserOperation.RELOAD}
        assert health.consecutive_timeouts == settings.BROWSER_DEGRADED_TIMEOUT_STRIKES + 1

    def test_a_recovery_for_an_operation_that_never_struck_gives_nothing_back(self) -> None:
        """The capture primitive raises the same exception for a non-timeout error, which records no
        strike; a rescue after that must not spend a strike another operation earned."""
        health = BrowserHealth()
        _strike(health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        _strike(health, BrowserOperation.RELOAD, 1)

        health.record_recovery(BrowserOperation.SCREENSHOT)

        assert health.consecutive_timeouts == settings.BROWSER_DEGRADED_TIMEOUT_STRIKES + 1
        assert health.stuck_operations == {BrowserOperation.EVALUATE, BrowserOperation.RELOAD}


class TestEvaluateRecordsBrowserHealth:
    """Without these the tally stays empty and the probe below can never fire."""

    @pytest.mark.asyncio
    async def test_analysis_timeout_is_recorded_against_the_run(self) -> None:
        context = SkyvernContext(task_id="tsk_health")
        skyvern_context.set(context)

        async def never_answers() -> object:
            await asyncio.sleep(10)
            return None

        with pytest.raises(SkyvernPageAnalysisTimeout):
            await SkyvernFrame._evaluate_expression(
                frame=MagicMock(),
                expression="() => 1",
                evaluate_expression=never_answers,
                timeout_ms=10,
            )

        assert context.browser_health.consecutive_timeouts == 1
        assert context.browser_health.stuck_operations == {BrowserOperation.EVALUATE}

    @pytest.mark.asyncio
    async def test_an_answered_evaluate_clears_the_tally(self) -> None:
        context = SkyvernContext(task_id="tsk_health")
        _strike(context.browser_health, BrowserOperation.SCREENSHOT, 3)
        skyvern_context.set(context)

        async def answers() -> object:
            return 7

        assert (
            await SkyvernFrame._evaluate_expression(
                frame=MagicMock(),
                expression="() => 7",
                evaluate_expression=answers,
                timeout_ms=30_000,
            )
            == 7
        )
        assert context.browser_health.consecutive_timeouts == 0

    @pytest.mark.asyncio
    async def test_navigation_recovery_success_clears_the_tally(self) -> None:
        context = SkyvernContext(task_id="tsk_health")
        _strike(context.browser_health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        context.browser_health.record_timeout(BrowserOperation.SCREENSHOT)
        assert context.browser_health.is_degraded
        skyvern_context.set(context)

        frame = AsyncMock()
        frame.evaluate = AsyncMock(
            side_effect=[
                RuntimeError("Execution context was destroyed, most likely because of a navigation."),
                None,
                7,
            ]
        )
        frame.wait_for_load_state = AsyncMock()

        assert await SkyvernFrame.evaluate(frame=frame, expression="() => 7", timeout_ms=30_000) == 7
        assert context.browser_health.consecutive_timeouts == 0
        assert not context.browser_health.stuck_operations


def _step_prompt_rig(monkeypatch: pytest.MonkeyPatch) -> tuple[ForgeAgent, dict, SkyvernContext]:
    now = datetime.now(UTC)
    task = make_task(now, make_organization(now), navigation_goal="Sign in", workflow_run_id="wr_health")
    step = make_step(now, task, step_id="step-health", status=StepStatus.running, order=3, output=None)

    agent = ForgeAgent()
    agent.async_operation_pool = MagicMock()
    monkeypatch.setattr("skyvern.forge.agent.resolve_transient_ui_capture_arm", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(return_value=False),
    )

    context = SkyvernContext(task_id=task.task_id, workflow_run_id=task.workflow_run_id)
    skyvern_context.set(context)

    kwargs = {
        "task": task,
        "step": step,
        "browser_state": make_browser_state()[0],
        "engine": RunEngine.skyvern_v1,
    }
    return agent, kwargs, context


class TestStepProbe:
    @pytest.mark.asyncio
    async def test_degraded_browser_fails_the_run_without_starting_another_scrape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, kwargs, context = _step_prompt_rig(monkeypatch)
        scrape = AsyncMock(side_effect=AssertionError("scrape ladder must not run on a dead browser"))
        monkeypatch.setattr(ForgeAgent, "_scrape_with_type", scrape)
        _strike(context.browser_health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        context.browser_health.record_timeout(BrowserOperation.SCREENSHOT)

        with pytest.raises(BrowserSessionDegraded):
            await agent.build_and_record_step_prompt(**kwargs)

        scrape.assert_not_called()

    @pytest.mark.asyncio
    async def test_healthy_browser_still_gets_the_full_retry_ladder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The RELOAD rung is what rescues a page whose own JS wedged the renderer. A run that has
        not proven its browser dead must keep every attempt it has today."""
        agent, kwargs, context = _step_prompt_rig(monkeypatch)
        scrape = AsyncMock(side_effect=ScrapingFailed(reason="still loading"))
        monkeypatch.setattr(ForgeAgent, "_scrape_with_type", scrape)
        _strike(context.browser_health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES * 2)

        with pytest.raises(ScrapingFailed):
            await agent.build_and_record_step_prompt(**kwargs)

        assert scrape.call_count == 3
