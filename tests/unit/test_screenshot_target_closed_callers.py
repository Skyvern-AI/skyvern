"""A closed browser target must not surface as an ERROR at any frame above the screenshot helper.

``ScreenshotTargetClosed`` marks an expected race: run teardown, whole-browser death, or the
site/user closing the tab (an OAuth popup cancelling itself) while a capture is in flight. The
helper classifies it and logs info, but each caller frame decided severity independently, so
suppressing one frame just moved the ERROR-with-traceback — and the auto-filed Error Tracking
issue — up to the next one. ``agent_step`` now passes it through as an expected terminal failure
alongside ``ScrapingFailed`` / ``MissingBrowserStatePage``, and ``execute_step`` fails the task
cleanly at warning. Task teardown, the action handler, complete-action verification and the SDK
route (``test_run_sdk_action_failures.py``) were the frames left over from that first pass. These
tests pin every frame, with controls so unrelated failures still ERROR.

``MissingBrowserStatePage`` is the same class of condition -- the browser context is gone, so no
page exists to scrape -- and it needs the same pass-through. ``scrape_website`` laundered it into
``ScrapingFailed``, which the step's scrape ladder treats as retryable, so a closed context spent
every rung of ``SCRAPE_TYPE_ORDER`` re-discovering that the browser was still gone before failing.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright._impl._errors import TargetClosedError

import skyvern.forge.agent as agent_mod
import skyvern.services.task_v2_service as task_v2_mod
import skyvern.webeye.actions.handler as handler_mod
import skyvern.webeye.scraper.scraper as scraper_mod
from skyvern.exceptions import (
    BrowserStateDiagnostic,
    FailedToTakeScreenshot,
    MissingBrowserStatePage,
    ScrapingFailed,
    ScreenshotTargetClosed,
)
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.runs import RunEngine
from skyvern.webeye.actions.actions import ActionType, ClickAction, CompleteAction
from skyvern.webeye.real_browser_manager import RealBrowserManager
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task
from tests.unit.test_agent_step_characterization import make_agent_step_rig


@asynccontextmanager
async def _noop_async_context(*args: Any, **kwargs: Any) -> AsyncIterator[None]:
    yield


def _agent_rig(
    monkeypatch: pytest.MonkeyPatch, screenshot_error: Exception, html_error: Exception | None = None
) -> tuple[MagicMock, dict]:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="step-ss", status=StepStatus.running, order=0, output=None)

    browser_state, _, page = make_browser_state()
    browser_state.get_working_page = AsyncMock(return_value=page)
    browser_state.take_post_action_screenshot = AsyncMock(side_effect=screenshot_error)

    frame = MagicMock()
    frame.safe_wait_for_animation_end = AsyncMock()
    frame.get_scroll_x_y = AsyncMock(return_value=(0, 0))
    # A genuinely closed target fails HTML capture too, so the closed-target cases pass html_error.
    frame.get_content = AsyncMock(side_effect=html_error) if html_error else AsyncMock(return_value="<html></html>")
    monkeypatch.setattr(agent_mod.SkyvernFrame, "create_instance", AsyncMock(return_value=frame))
    monkeypatch.setattr(agent_mod.app.ARTIFACT_MANAGER, "accumulate_action_html_to_archive", MagicMock())

    log = MagicMock()
    monkeypatch.setattr(agent_mod, "LOG", log)
    skyvern_context.set(SkyvernContext(task_id=task.task_id, organization_id=task.organization_id))

    kwargs = {
        "task": task,
        "step": step,
        "browser_state": browser_state,
        "engine": RunEngine.skyvern_v1,
        "action": MagicMock(action_type="click"),
    }
    return log, kwargs


class TestRecordArtifactsAfterActionTargetClosed:
    @pytest.mark.asyncio
    async def test_closed_target_is_logged_without_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, kwargs = _agent_rig(monkeypatch, ScreenshotTargetClosed(error_message="Page is closed"))

        await ForgeAgent.record_artifacts_after_action(MagicMock(), **kwargs)

        assert not any("screenshot" in str(call.args[0]).lower() for call in log.error.call_args_list), (
            f"closed target still logged ERROR: {log.error.call_args_list}"
        )
        assert log.info.called

    @pytest.mark.asyncio
    async def test_closed_target_skips_html_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The closed target must not fall through into HTML capture and re-raise as an ERROR there."""
        log, kwargs = _agent_rig(
            monkeypatch,
            ScreenshotTargetClosed(error_message="Page is closed"),
            html_error=TargetClosedError("Target page, context or browser has been closed"),
        )

        await ForgeAgent.record_artifacts_after_action(MagicMock(), **kwargs)

        kwargs["browser_state"].take_post_action_screenshot.assert_awaited_once()
        log.exception.assert_not_called()
        log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_screenshot_failures_still_record_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the closed-target case short-circuits; other failures still attempt HTML capture."""
        log, kwargs = _agent_rig(monkeypatch, FailedToTakeScreenshot(error_message="Target crashed"))

        await ForgeAgent.record_artifacts_after_action(MagicMock(), **kwargs)

        assert any("Failed to record screenshot after action" in str(call.args[0]) for call in log.error.call_args_list)
        log.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_screenshot_failures_still_log_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, kwargs = _agent_rig(monkeypatch, FailedToTakeScreenshot(error_message="Target crashed"))

        await ForgeAgent.record_artifacts_after_action(MagicMock(), **kwargs)

        assert any("Failed to record screenshot after action" in str(call.args[0]) for call in log.error.call_args_list)


class TestScrapeWebsiteTargetClosed:
    @pytest.mark.asyncio
    async def test_closed_target_reraises_without_error_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = ScreenshotTargetClosed(error_message="Page.screenshot: Target page, context or browser has been closed")
        monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", AsyncMock(side_effect=error))
        log = MagicMock()
        monkeypatch.setattr(scraper_mod, "LOG", log)

        with pytest.raises(ScreenshotTargetClosed) as exc_info:
            await scraper_mod.scrape_website(
                browser_state=MagicMock(),
                url="https://example.test/apply",
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )

        assert exc_info.value is error
        log.error.assert_not_called()
        log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_scrape_failures_log_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(scraper_mod, "build_scraping_failed_reason", AsyncMock(return_value="reason"))
        log = MagicMock()
        monkeypatch.setattr(scraper_mod, "LOG", log)

        with pytest.raises(Exception):
            await scraper_mod.scrape_website(
                browser_state=MagicMock(),
                url="https://example.test/apply",
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )

        assert any("Scraping failed after max retries" in str(call.args[0]) for call in log.warning.call_args_list)
        log.error.assert_not_called()


class TestScrapeRetryLoopTargetClosed:
    """The terminal scrape attempt re-raises and logs at warning; its callers own the error record."""

    def _rig(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> tuple[ForgeAgent, MagicMock, dict]:
        now = datetime.now(UTC)
        organization = make_organization(now)
        task = make_task(now, organization)
        step = make_step(now, task, step_id="step-loop", status=StepStatus.running, order=0, output=None)
        browser_state, _, _ = make_browser_state()

        agent = ForgeAgent()
        monkeypatch.setattr(agent, "_scrape_with_type", AsyncMock(side_effect=error))
        monkeypatch.setattr(agent_mod, "SCRAPE_TYPE_ORDER", [MagicMock()])
        monkeypatch.setattr(
            agent_mod.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False)
        )
        log = MagicMock()
        monkeypatch.setattr(agent_mod, "LOG", log)
        skyvern_context.set(SkyvernContext(task_id=task.task_id, organization_id=task.organization_id))

        return agent, log, {"task": task, "step": step, "browser_state": browser_state, "engine": RunEngine.skyvern_v1}

    @pytest.mark.asyncio
    async def test_final_attempt_warns_on_closed_target(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = ScreenshotTargetClosed(error_message="Page is closed")
        agent, log, kwargs = self._rig(monkeypatch, error)

        with pytest.raises(ScreenshotTargetClosed):
            await agent.build_and_record_step_prompt(**kwargs)

        assert not any("All scrape attempts failed" in str(call.args[0]) for call in log.error.call_args_list)
        assert any("browser target closed" in str(call.args[0]).lower() for call in log.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_final_attempt_warns_on_other_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, log, kwargs = self._rig(monkeypatch, FailedToTakeScreenshot(error_message="Target crashed"))

        with pytest.raises(FailedToTakeScreenshot):
            await agent.build_and_record_step_prompt(**kwargs)

        assert any("All scrape attempts failed" in str(call.args[0]) for call in log.warning.call_args_list)
        assert not any("All scrape attempts failed" in str(call.args[0]) for call in log.error.call_args_list)


def _browser_gone() -> MissingBrowserStatePage:
    """A missing page whose browser state already observed an unrecovered disconnect."""
    now = datetime.now(UTC)
    return MissingBrowserStatePage(
        task_id="tsk_gone",
        diagnostic=BrowserStateDiagnostic(
            reason="browser_context_close_event",
            disconnect_observed_at=now,
            event="browser_context_close",
            observation_source="browser_event",
        ),
        detected_at=now,
    )


class TestMissingBrowserStatePagePassThrough:
    """A closed browser context is terminal: ``check_and_fix_state`` only rebuilds when
    ``browser_context`` is ``None`` (a closed context is not), and ``_reopen_lost_working_page``
    bails on a disconnected context. Every further scrape attempt is guaranteed to fail the same
    way, so the scraper must surface the condition instead of relabelling it a scrape failure.
    """

    @pytest.mark.asyncio
    async def test_scrape_website_reraises_missing_page_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = _browser_gone()
        monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", AsyncMock(side_effect=error))
        monkeypatch.setattr(scraper_mod, "build_scraping_failed_reason", AsyncMock(return_value="reason"))

        with pytest.raises(MissingBrowserStatePage) as exc_info:
            await scraper_mod.scrape_website(
                browser_state=MagicMock(),
                url="https://example.test/apply",
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )

        assert exc_info.value is error

    @pytest.mark.asyncio
    async def test_scrape_website_retries_are_not_spent_on_a_gone_browser(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``max_retries`` is 0 in production, but a caller passing a budget must not spend it either."""
        scrape = AsyncMock(side_effect=_browser_gone())
        monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", scrape)
        monkeypatch.setattr(scraper_mod, "build_scraping_failed_reason", AsyncMock(return_value="reason"))

        with pytest.raises(MissingBrowserStatePage):
            await scraper_mod.scrape_website(
                browser_state=MagicMock(),
                url="https://example.test/apply",
                cleanup_element_tree=AsyncMock(),
                max_retries=3,
            )

        assert scrape.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_page_without_an_observed_disconnect_keeps_its_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: no disconnect was observed, so the page may yet reopen -- the browser is not
        provably gone and the caller's ladder still deserves its attempts."""
        scrape = AsyncMock(side_effect=MissingBrowserStatePage(task_id="tsk_maybe"))
        monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", scrape)
        monkeypatch.setattr(scraper_mod, "build_scraping_failed_reason", AsyncMock(return_value="reason"))

        with pytest.raises(ScrapingFailed):
            await scraper_mod.scrape_website(
                browser_state=MagicMock(),
                url="https://example.test/apply",
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )

    def _ladder_rig(self, monkeypatch: pytest.MonkeyPatch, scrape_error: Exception) -> tuple[ForgeAgent, list, dict]:
        now = datetime.now(UTC)
        organization = make_organization(now)
        task = make_task(now, organization)
        step = make_step(now, task, step_id="step-gone", status=StepStatus.running, order=0, output=None)
        browser_state, _, _ = make_browser_state()

        monkeypatch.setattr(scraper_mod, "scrape_web_unsafe", AsyncMock(side_effect=scrape_error))
        monkeypatch.setattr(scraper_mod, "build_scraping_failed_reason", AsyncMock(return_value="reason"))
        monkeypatch.setattr(
            agent_mod.app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", AsyncMock(return_value=False)
        )
        skyvern_context.set(SkyvernContext(task_id=task.task_id, organization_id=task.organization_id))

        # Drive the real scraper so the ladder sees the exception type scrape_website actually raises.
        attempts: list[object] = []

        async def scrape_with_type(*args: Any, **kwargs: Any) -> Any:
            attempts.append(kwargs.get("scrape_type"))
            return await scraper_mod.scrape_website(
                browser_state=browser_state,
                url=task.url,
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )

        agent = ForgeAgent()
        monkeypatch.setattr(agent, "_scrape_with_type", scrape_with_type)

        return (
            agent,
            attempts,
            {"task": task, "step": step, "browser_state": browser_state, "engine": RunEngine.skyvern_v1},
        )

    @pytest.mark.asyncio
    async def test_ladder_stops_at_the_first_rung_when_the_browser_is_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent, attempts, kwargs = self._ladder_rig(monkeypatch, _browser_gone())

        with pytest.raises(MissingBrowserStatePage):
            await agent.build_and_record_step_prompt(**kwargs)

        assert len(attempts) == 1, f"a closed browser context still cost {len(attempts)} scrape attempts"

    @pytest.mark.asyncio
    async def test_ladder_still_spends_every_rung_on_a_real_scrape_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: a site-caused failure is still worth retrying with the next strategy."""
        agent, attempts, kwargs = self._ladder_rig(monkeypatch, RuntimeError("boom"))

        with pytest.raises(ScrapingFailed):
            await agent.build_and_record_step_prompt(**kwargs)

        assert len(attempts) == len(agent_mod.SCRAPE_TYPE_ORDER)


class TestAgentStepPassThrough:
    """``agent_step`` must treat a closed target as an expected terminal failure, the way it already
    treats ``ScrapingFailed`` / ``MissingBrowserStatePage`` — otherwise its generic handler re-files
    the same exception and traceback one frame up.
    """

    @pytest.mark.asyncio
    async def test_closed_target_passes_through_without_log_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rig = make_agent_step_rig(monkeypatch)
        monkeypatch.setattr(
            rig.agent,
            "build_and_record_step_prompt",
            AsyncMock(side_effect=ScreenshotTargetClosed(error_message="Page is closed")),
        )
        log = MagicMock()
        monkeypatch.setattr(agent_mod, "LOG", log)

        with pytest.raises(ScreenshotTargetClosed):
            await rig.run()

        log.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_failures_still_log_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rig = make_agent_step_rig(monkeypatch)
        monkeypatch.setattr(rig.agent, "build_and_record_step_prompt", AsyncMock(side_effect=RuntimeError("boom")))
        log = MagicMock()
        monkeypatch.setattr(agent_mod, "LOG", log)

        await rig.run()

        log.exception.assert_called()


class TestCleanUpTaskTargetClosed:
    """The final screenshot is best-effort teardown, taken while the browser is being torn down.
    The narrow handler only named Playwright's ``TargetClosedError``, but the screenshot helper
    stopped letting that through -- it classifies and re-raises ``ScreenshotTargetClosed``.
    """

    def _rig(self, monkeypatch: pytest.MonkeyPatch, screenshot_error: Exception) -> tuple[MagicMock, dict]:
        now = datetime.now(UTC)
        organization = make_organization(now)
        task = make_task(now, organization, workflow_run_id="wr-cleanup")
        step = make_step(now, task, step_id="step-cleanup", status=StepStatus.completed, order=0, output=None)

        browser_state, _, page = make_browser_state()
        browser_state.get_working_page = AsyncMock(return_value=page)
        browser_state.take_fullpage_screenshot = AsyncMock(side_effect=screenshot_error)

        app_mock = MagicMock()
        app_mock.DATABASE.tasks.get_task = AsyncMock(return_value=task)
        app_mock.BROWSER_MANAGER.get_for_task = MagicMock(return_value=browser_state)
        app_mock.ARTIFACT_MANAGER.create_artifact = AsyncMock()
        monkeypatch.setattr(agent_mod, "app", app_mock)
        monkeypatch.setattr(agent_mod.analytics, "capture", MagicMock())
        monkeypatch.setattr(agent_mod, "settle_browser_downloads_for_context", _noop_async_context)

        log = MagicMock()
        monkeypatch.setattr(agent_mod, "LOG", log)
        skyvern_context.set(SkyvernContext(task_id=task.task_id, organization_id=task.organization_id))

        return log, {"task": task, "last_step": step}

    @pytest.mark.asyncio
    async def test_closed_target_is_logged_without_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, kwargs = self._rig(monkeypatch, ScreenshotTargetClosed(error_message="Page is closed"))

        await ForgeAgent().clean_up_task(**kwargs)

        log.exception.assert_not_called()
        assert any("page is closed" in str(call.args[0]).lower() for call in log.warning.call_args_list), (
            f"closed target was not warned about: {log.warning.call_args_list}"
        )

    @pytest.mark.asyncio
    async def test_other_screenshot_failures_still_log_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, kwargs = self._rig(monkeypatch, FailedToTakeScreenshot(error_message="Target crashed"))

        await ForgeAgent().clean_up_task(**kwargs)

        assert any(
            "Failed to take screenshot before sending task response" in str(call.args[0])
            for call in log.exception.call_args_list
        )


class TestHandleActionTargetClosed:
    async def _run(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> tuple[MagicMock, list]:
        now = datetime.now(UTC)
        task = make_task(now, make_organization(now))
        step = make_step(now, task, step_id="step-action", status=StepStatus.running, order=0, output=None)

        async def failing_handler(*args: object, **kwargs: object) -> None:
            raise error

        app_mock = MagicMock()
        app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()
        log = MagicMock()
        monkeypatch.setattr(handler_mod, "app", app_mock)
        monkeypatch.setattr(handler_mod, "LOG", log)

        with (
            patch.dict(handler_mod.ActionHandler._handled_action_types, {ActionType.CLICK: failing_handler}),
            patch.dict(handler_mod.ActionHandler._setup_action_types, {}, clear=True),
            patch.dict(handler_mod.ActionHandler._teardown_action_types, {}, clear=True),
        ):
            results = await handler_mod.ActionHandler._handle_action(
                scraped_page=MagicMock(id_to_element_dict={"el": {"id": "el"}}),
                task=task,
                step=step,
                page=MagicMock(),
                action=ClickAction(element_id="el"),
            )
        return log, results

    @pytest.mark.asyncio
    async def test_closed_target_fails_the_action_without_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, results = await self._run(monkeypatch, ScreenshotTargetClosed(error_message="Page is closed"))

        assert len(results) == 1 and results[0].success is False
        assert results[0].exception_type == "ScreenshotTargetClosed"
        log.exception.assert_not_called()
        log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_failures_still_log_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, results = await self._run(monkeypatch, RuntimeError("boom"))

        assert len(results) == 1 and results[0].success is False
        assert any(
            "Unhandled exception in action handler" in str(call.args[0]) for call in log.exception.call_args_list
        )


class TestCompleteActionVerificationTargetClosed:
    async def _run(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> tuple[MagicMock, list]:
        now = datetime.now(UTC)
        task = make_task(now, make_organization(now))
        step = make_step(now, task, step_id="step-complete", status=StepStatus.running, order=0, output=None)

        app_mock = MagicMock()
        app_mock.agent.complete_verify = AsyncMock(side_effect=error)
        log = MagicMock()
        monkeypatch.setattr(handler_mod, "app", app_mock)
        monkeypatch.setattr(handler_mod, "LOG", log)

        results = await handler_mod.handle_complete_action(
            CompleteAction(),
            MagicMock(),
            MagicMock(),
            task,
            step,
        )
        return log, results

    @pytest.mark.asyncio
    async def test_closed_target_fails_verification_without_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, results = await self._run(monkeypatch, ScreenshotTargetClosed(error_message="Page is closed"))

        assert len(results) == 1 and results[0].success is False
        log.exception.assert_not_called()
        log.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_other_failures_still_log_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        log, results = await self._run(monkeypatch, RuntimeError("boom"))

        assert len(results) == 1 and results[0].success is False
        assert any("Failed to verify the complete action" in str(call.args[0]) for call in log.exception.call_args_list)


class _CachedBrowserState:
    """Mirrors ``RealBrowserState`` at the two points the step boundary depends on: the liveness read
    is what latches the disconnect diagnostic, and the diagnostic is what every consumer reads to
    decide the browser is provably gone."""

    def __init__(self, *, connected: bool, browser_artifacts: Any = None) -> None:
        self._connected = connected
        self._diagnostic: BrowserStateDiagnostic | None = None
        self.browser_context = None
        self.browser_artifacts = browser_artifacts

    def is_connected(self) -> bool:
        if not self._connected and self._diagnostic is None:
            self._diagnostic = BrowserStateDiagnostic(
                reason="browser_context_closed",
                disconnect_observed_at=datetime.now(UTC),
                event="browser_context_disconnected",
                observation_source="liveness_probe",
            )
        return self._connected

    def get_browser_state_diagnostic(self) -> BrowserStateDiagnostic | None:
        return self._diagnostic


class TestStepBoundaryBrowserLoss:
    """A step that starts against a browser already known to be gone spends its whole scrape ladder
    re-discovering that. ``check_and_fix_state`` clears the diagnostic whenever it rebuilds, and
    nothing in the scrape path rebuilds, so a diagnostic still latched at acquisition means every
    page access this step makes is guaranteed to raise. Reconnecting instead — what ``block.py`` does
    at a block boundary — would hand a mid-flow task a blank browser with none of its state.
    """

    def _rig(self, monkeypatch: pytest.MonkeyPatch, browser_state: _CachedBrowserState) -> tuple[ForgeAgent, dict]:
        now = datetime.now(UTC)
        organization = make_organization(now)
        task = make_task(now, organization, task_id="tsk_boundary")
        step = make_step(now, task, step_id="step-boundary", status=StepStatus.running, order=4, output=None)

        manager = RealBrowserManager()
        manager.pages[task.task_id] = browser_state  # type: ignore[assignment]
        monkeypatch.setattr(agent_mod.app, "BROWSER_MANAGER", manager)
        skyvern_context.set(SkyvernContext(task_id=task.task_id, organization_id=task.organization_id))

        return ForgeAgent(), {"task": task, "step": step}

    @pytest.mark.asyncio
    async def test_cached_dead_browser_fails_the_step_before_it_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A task with no persistent session is handed its cached, dead browser state untouched: the
        manager only probes (and reconnects) persistent-session states, because only a session can
        be re-acquired as the SAME browser. The gate's own probe is what makes the death visible."""
        browser_state = _CachedBrowserState(connected=False, browser_artifacts=MagicMock())
        agent, kwargs = self._rig(monkeypatch, browser_state)
        video_artifacts = AsyncMock(side_effect=AssertionError("the step must not start on a dead browser"))
        monkeypatch.setattr(agent_mod.app.BROWSER_MANAGER, "get_video_artifacts", video_artifacts)
        create = AsyncMock(side_effect=AssertionError("a mid-flow task must not be given a fresh blank browser"))
        monkeypatch.setattr(agent_mod.app.BROWSER_MANAGER, "_create_browser_state", create)

        with pytest.raises(MissingBrowserStatePage) as exc_info:
            await agent.initialize_execution_state(**kwargs)

        assert exc_info.value.diagnostic is browser_state.get_browser_state_diagnostic()
        video_artifacts.assert_not_called()
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_pre_resolved_browser_state_does_not_bypass_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A caller-supplied state skips the manager entirely, so nothing upstream has probed it and
        no diagnostic is latched yet. The gate must probe liveness itself, not just read."""
        browser_state = _CachedBrowserState(connected=False, browser_artifacts=MagicMock())
        assert browser_state.get_browser_state_diagnostic() is None
        agent, kwargs = self._rig(monkeypatch, _CachedBrowserState(connected=True))

        with pytest.raises(MissingBrowserStatePage) as exc_info:
            await agent.initialize_execution_state(**kwargs, pre_resolved_browser_state=browser_state)  # type: ignore[arg-type]

        assert exc_info.value.diagnostic is browser_state.get_browser_state_diagnostic()

    @pytest.mark.asyncio
    async def test_a_live_browser_still_starts_the_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: no observed disconnect means the browser is not provably gone, and the step runs."""
        browser_state = _CachedBrowserState(connected=True)
        agent, kwargs = self._rig(monkeypatch, browser_state)

        _, acquired, _ = await agent.initialize_execution_state(**kwargs)

        assert acquired is browser_state


class TestTaskV2IterationTargetClosed:
    """Structural guard: ``run_task_v2_helper`` needs a full workflow-run rig to drive, so this pins
    the clause ordering instead — the closed-target handler must precede the generic ``LOG.exception``.
    Same ``inspect.getsource`` approach already used in ``test_screenshot_attribution.py``.
    """

    def test_closed_target_handled_before_generic_exception(self) -> None:
        source = inspect.getsource(task_v2_mod.run_task_v2_helper)
        scrape_call = source.index("Failed to get browser state or scrape website in task v2 iteration")
        preceding = source[:scrape_call]

        closed_handler = preceding.rindex("except ScreenshotTargetClosed:")
        generic_handler = preceding.rindex("except Exception:")

        assert closed_handler < generic_handler, "closed-target clause must come before the generic handler"
        assert "LOG.warning(" in preceding[closed_handler:generic_handler]
