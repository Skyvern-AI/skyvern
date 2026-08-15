"""A closed browser target must not surface as an ERROR at any frame above the screenshot helper.

``ScreenshotTargetClosed`` marks an expected race: run teardown, whole-browser death, or the
site/user closing the tab (an OAuth popup cancelling itself) while a capture is in flight. The
helper classifies it and logs info, but each caller frame decided severity independently, so
suppressing one frame just moved the ERROR-with-traceback — and the auto-filed Error Tracking
issue — up to the next one. ``agent_step`` now passes it through as an expected terminal failure
alongside ``ScrapingFailed`` / ``MissingBrowserStatePage``, and ``execute_step`` fails the task
cleanly at warning. These tests pin every frame, with controls so unrelated failures still ERROR.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError

import skyvern.forge.agent as agent_mod
import skyvern.services.task_v2_service as task_v2_mod
import skyvern.webeye.scraper.scraper as scraper_mod
from skyvern.exceptions import FailedToTakeScreenshot, ScreenshotTargetClosed
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.schemas.runs import RunEngine
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task
from tests.unit.test_agent_step_characterization import make_agent_step_rig


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
    """The terminal scrape attempt re-raises, and its own ERROR is what Error Tracking would file."""

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
    async def test_final_attempt_still_errors_on_other_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent, log, kwargs = self._rig(monkeypatch, FailedToTakeScreenshot(error_message="Target crashed"))

        with pytest.raises(FailedToTakeScreenshot):
            await agent.build_and_record_step_prompt(**kwargs)

        assert any("All scrape attempts failed" in str(call.args[0]) for call in log.error.call_args_list)


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
