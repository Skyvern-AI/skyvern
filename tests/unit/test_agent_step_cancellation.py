from asyncio import CancelledError
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task


@pytest.mark.asyncio
async def test_agent_step_reraises_cancelled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CancelledError during a step (elapsed-time timeout / user stop) must propagate out of
    agent_step so the timeout actually halts the run, instead of being swallowed into a failed-step
    return that the step loop then retries. The step is still persisted as failed before re-raising."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal="Reach confirmation page", workflow_run_id="workflow-1")
    step = make_step(now, task, step_id="step-cancel", status=StepStatus.created, order=0, output=None)

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)
    browser_state.get_working_page = AsyncMock(return_value=page)

    async def _dummy_cleanup(*_args, **_kwargs) -> list[dict]:
        return []

    scraped_page = ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=_dummy_cleanup,
        _scrape_exclude=None,
    )
    scraped_page.screenshots = [b"image"]

    agent.build_and_record_step_prompt = AsyncMock(return_value=(scraped_page, "prompt", False, "extract-actions"))
    json_response: dict[str, object] = {"actions": [{"action_type": "CLICK", "element_id": "node-1"}]}
    agent.handle_potential_OTP_actions = AsyncMock(return_value=(json_response, []))

    click_action = ClickAction(
        element_id="node-1",
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=step.order,
        action_order=0,
    )
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: [click_action])

    # The run-level cancellation lands while the action is executing.
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", AsyncMock(side_effect=CancelledError()))
    agent.record_artifacts_after_action = AsyncMock()
    agent._is_multi_field_totp_sequence = MagicMock(return_value=False)
    agent.check_user_goal_complete = AsyncMock()

    llm_handler_mock = AsyncMock(return_value=json_response)
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *_args, **_kwargs: llm_handler_mock,
    )
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.prepare_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_action_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.random.uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(return_value=False),
    )

    update_statuses: list[StepStatus | None] = []

    async def fake_update_step(
        step: Step,
        status: StepStatus | None = None,
        output=None,
        is_last: bool | None = None,
        retry_index: int | None = None,
        **_kwargs,
    ) -> Step:
        update_statuses.append(status)
        if status is not None:
            step.status = status
        return step

    agent.update_step = AsyncMock(side_effect=fake_update_step)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    try:
        with pytest.raises(CancelledError):
            await agent.agent_step(
                task=task,
                step=step,
                browser_state=browser_state,
                organization=organization,
            )
    finally:
        skyvern_context.reset()

    # The cancelled step is still recorded as failed (so it is not left orphaned as `running`).
    assert StepStatus.failed in update_statuses
