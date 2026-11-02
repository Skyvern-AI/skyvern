"""A remote browser can stop answering its control protocol while the page it hosts keeps running.

Every consumer then only sees its own deadline expire — analysis, capture and reload each look
like an ordinary slow page — so the retry ladders keep spending the run's step budget on a browser
that will never answer again (SKY-14033: ~15 minutes of retries, then a failure that blamed the
site). These tests pin the signal that separates the two cases and the point that acts on it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Frame, Page
from structlog.testing import capture_logs

from skyvern.cli.core import js_dispatch
from skyvern.config import settings
from skyvern.exceptions import BrowserSessionDegraded, ScrapingFailed, SkyvernPageAnalysisTimeout
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.runs import RunEngine
from skyvern.webeye import browser_health
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

    def test_a_strike_with_no_frame_says_so_instead_of_leaving_the_field_blank(self) -> None:
        health = BrowserHealth()
        health.record_timeout(BrowserOperation.EVALUATE)
        health.record_timeout(BrowserOperation.RELOAD)

        assert health.stall_fields()["stalled_frames"] == "not_a_frame_op, unknown"


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


def _write_cgroup_cpu_counters(
    directory: Path, *, wait_usec: int, usage_usec: int, throttled_usec: int, nr_throttled: int
) -> None:
    (directory / "cpu.pressure").write_text(
        f"some avg10=0.00 avg60=0.00 avg300=0.00 total={wait_usec}\n"
        f"full avg10=0.00 avg60=0.00 avg300=0.00 total={wait_usec // 2}\n"
    )
    (directory / "cpu.stat").write_text(
        f"usage_usec {usage_usec}\nuser_usec {usage_usec - 1000}\nsystem_usec 1000\n"
        f"core_sched.force_idle_usec 0\nnr_periods {nr_throttled * 4}\nnr_throttled {nr_throttled}\n"
        f"throttled_usec {throttled_usec}\nnr_bursts 0\nburst_usec 0\n"
    )


def _frame(*, parent: object) -> MagicMock:
    frame = MagicMock(spec=Frame)
    frame.parent_frame = parent
    return frame


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
    @pytest.mark.parametrize(
        ("cdp_session", "renderer_metrics", "renderer_os_cpu", "cgroup_membership", "container_fields"),
        [
            pytest.param(
                AsyncMock(side_effect=RuntimeError("target closed")),
                "session_error:RuntimeError",
                "process_info_error:RuntimeError",
                # A host process: /sys/fs/cgroup is the host's root cgroup, not this worker's.
                "0::/user.slice/user-1000.slice/session-1.scope\n",
                {},
                id="browser_refuses",
            ),
            pytest.param(
                # A renderer whose main thread takes no tasks never answers Performance.enable.
                AsyncMock(return_value=SimpleNamespace(send=lambda *_: asyncio.Event().wait())),
                "enable_timeout",
                "process_info_timeout",
                "0::/user.slice/user-1000.slice/session-1.scope\n",
                {},
                id="browser_hangs",
            ),
            pytest.param(
                AsyncMock(return_value=SimpleNamespace(send=lambda *_: asyncio.Event().wait())),
                "enable_timeout",
                "process_info_timeout",
                "0::/\n",
                {
                    "container_cpu_wait_seconds": 0.25,
                    "container_cpu_usage_seconds": 1.5,
                    "container_cpu_throttled_seconds": 0.4,
                    "container_cpu_throttled_periods": 7,
                },
                id="browser_hangs_in_its_own_cgroup",
            ),
        ],
    )
    async def test_renderer_diagnostics_that_fail_or_hang_leave_the_degraded_failure_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        cdp_session: AsyncMock,
        renderer_metrics: str,
        renderer_os_cpu: str,
        cgroup_membership: str,
        container_fields: dict[str, float],
    ) -> None:
        agent, kwargs, context = _step_prompt_rig(monkeypatch)
        monkeypatch.setattr(ForgeAgent, "_scrape_with_type", AsyncMock(side_effect=AssertionError("no scrape")))
        page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=cdp_session))
        kwargs["browser_state"].get_working_page = AsyncMock(return_value=page)
        # The browser-level session for the scoped OS reading fails the same way.
        kwargs["browser_state"].browser_context = SimpleNamespace(
            browser=SimpleNamespace(new_browser_cdp_session=cdp_session)
        )
        (tmp_path / "self_cgroup").write_text(cgroup_membership)
        monkeypatch.setattr(browser_health, "CGROUP_MEMBERSHIP_PATH", tmp_path / "self_cgroup")
        monkeypatch.setattr(browser_health, "CGROUP_CPU_PRESSURE_PATH", tmp_path / "cpu.pressure")
        monkeypatch.setattr(browser_health, "CGROUP_CPU_STAT_PATH", tmp_path / "cpu.stat")
        _write_cgroup_cpu_counters(
            tmp_path, wait_usec=2_000_000, usage_usec=9_000_000, throttled_usec=600_000, nr_throttled=3
        )
        for _ in range(settings.BROWSER_DEGRADED_TIMEOUT_STRIKES):
            context.browser_health.record_timeout(BrowserOperation.EVALUATE, frame_kind="child_frame")
        context.browser_health.record_timeout(BrowserOperation.SCREENSHOT)
        _write_cgroup_cpu_counters(
            tmp_path, wait_usec=2_250_000, usage_usec=10_500_000, throttled_usec=1_000_000, nr_throttled=10
        )

        with capture_logs() as logs, pytest.raises(BrowserSessionDegraded) as raised:
            await asyncio.wait_for(agent.build_and_record_step_prompt(**kwargs), timeout=5)

        assert raised.value.consecutive_timeouts == settings.BROWSER_DEGRADED_TIMEOUT_STRIKES + 1
        assert raised.value.stuck_operations == "evaluate, screenshot"
        [event] = [log for log in logs if log["event"].startswith("Browser session stopped responding")]
        assert event["renderer_metrics"] == renderer_metrics
        assert event["renderer_os_cpu"] == renderer_os_cpu
        # The stall is milliseconds long here, so no *_share field may appear.
        assert {key: value for key, value in event.items() if key.startswith("container_")} == container_fields
        assert event["stalled_frames"] == "child_frame, not_a_frame_op"
        assert event["stall_window_seconds"] >= 0 and event["worker_process_cpu_seconds"] >= 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("frame", "frame_kind"),
        [
            pytest.param(_frame(parent=MagicMock(spec=Frame)), "child_frame", id="child_frame"),
            pytest.param(_frame(parent=None), "main_frame", id="main_frame"),
            pytest.param(MagicMock(spec=Page), "main_frame", id="page"),
            pytest.param(object(), "unknown", id="not_a_frame"),
        ],
    )
    async def test_guard_log_names_the_kind_of_frame_each_stalled_operation_hit(
        self, monkeypatch: pytest.MonkeyPatch, frame: Any, frame_kind: str
    ) -> None:
        agent, kwargs, context = _step_prompt_rig(monkeypatch)
        monkeypatch.setattr(ForgeAgent, "_scrape_with_type", AsyncMock(side_effect=AssertionError("no scrape")))
        kwargs["browser_state"].get_working_page = AsyncMock(return_value=None)
        kwargs["browser_state"].browser_context = None

        async def never_answers() -> object:
            await asyncio.Event().wait()
            return None

        for _ in range(settings.BROWSER_DEGRADED_TIMEOUT_STRIKES - 1):
            with pytest.raises(SkyvernPageAnalysisTimeout):
                await SkyvernFrame._evaluate_expression(
                    frame=frame,
                    expression="() => 1",
                    evaluate_expression=never_answers,
                    timeout_ms=10,
                )
        # A driver error raised as the action deadline cancels the call never reaches the engine's own handler.
        js_dispatch.record_unreported_timeout(RuntimeError("Target closed"), frame)
        context.browser_health.record_timeout(BrowserOperation.SCREENSHOT)

        with capture_logs() as logs, pytest.raises(BrowserSessionDegraded):
            await agent.build_and_record_step_prompt(**kwargs)

        [event] = [log for log in logs if log["event"].startswith("Browser session stopped responding")]
        assert event["stalled_frames"] == ", ".join(sorted({frame_kind, "not_a_frame_op"}))

    @pytest.mark.asyncio
    async def test_a_cancel_during_diagnostics_stays_a_cancel_and_keeps_the_log_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, kwargs, context = _step_prompt_rig(monkeypatch)
        monkeypatch.setattr(ForgeAgent, "_scrape_with_type", AsyncMock(side_effect=AssertionError("no scrape")))
        collecting = asyncio.Event()

        async def get_working_page(**_: object) -> None:
            collecting.set()
            await asyncio.Event().wait()

        kwargs["browser_state"].get_working_page = get_working_page
        _strike(context.browser_health, BrowserOperation.EVALUATE, settings.BROWSER_DEGRADED_TIMEOUT_STRIKES)
        context.browser_health.record_timeout(BrowserOperation.SCREENSHOT)

        with capture_logs() as logs:
            step_prompt = asyncio.create_task(agent.build_and_record_step_prompt(**kwargs))
            await collecting.wait()
            step_prompt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await step_prompt

        [event] = [log for log in logs if log["event"].startswith("Browser session stopped responding")]
        assert event["renderer_metrics"] == "cancelled"
        assert event["stall_window_seconds"] >= 0 and event["worker_process_cpu_seconds"] >= 0

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
