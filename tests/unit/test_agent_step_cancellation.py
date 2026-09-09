"""agent_step exception-seam behavior: cancellation and known-exception download evidence.

Two production seams:

* A CancelledError during a step (elapsed-time timeout / user stop) must propagate out of agent_step
  so the timeout actually halts the run, while the step is still persisted as failed.
* A known step exception re-raised after actions ran (e.g. MissingBrowserStatePage from
  ``must_get_working_page()`` once a download action has already recorded its observed HTTP failure
  status) stamps the accumulated output onto the caller-owned step and re-raises unchanged. agent_step
  performs no output-carrying write of its own on this path; ``fail_task`` owns the single failed-step
  write that persists ``step.output``, so the terminal update_task download-status fold still sees the
  same-step evidence.
"""

from __future__ import annotations

from asyncio import CancelledError
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.exceptions import MissingBrowserStatePage
from skyvern.forge.agent import ForgeAgent, StepPromptResult
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task


class _StepHarness:
    def __init__(self, agent: ForgeAgent, task, step: Step, browser_state, page) -> None:
        self.agent = agent
        self.task = task
        self.step = step
        self.browser_state = browser_state
        self.page = page

    async def run(self):
        context = SkyvernContext(
            task_id=self.task.task_id,
            step_id=None,
            organization_id=self.task.organization_id,
            workflow_run_id=self.task.workflow_run_id,
            tz_info=ZoneInfo("UTC"),
        )
        skyvern_context.set(context)
        try:
            return await self.agent.agent_step(
                task=self.task,
                step=self.step,
                browser_state=self.browser_state,
                organization=make_organization(datetime.now(UTC)),
            )
        finally:
            skyvern_context.reset()


def _make_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    navigation_goal: str,
) -> _StepHarness:
    """Common agent_step setup shared by every seam test.

    Applies the single process-global ``asyncio.sleep`` patch for this module here so no test adds
    its own. Callers still install their own ``parse_actions`` / ``handle_action`` /
    ``must_get_working_page`` / ``update_step`` behavior to shape the specific seam under test.
    """
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal=navigation_goal, workflow_run_id="workflow-1")
    step = make_step(now, task, step_id="step-seam", status=StepStatus.created, order=0, output=None)

    browser_state, _, page = make_browser_state()
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

    agent.build_and_record_step_prompt = AsyncMock(
        return_value=StepPromptResult(
            scraped_page=scraped_page,
            extract_action_prompt="prompt",
            use_caching=False,
            prompt_name="extract-actions",
            without_page_information=False,
        )
    )
    json_response: dict[str, object] = {"actions": [{"action_type": "CLICK", "element_id": "node-1"}]}
    agent.handle_potential_OTP_actions = AsyncMock(return_value=(json_response, []))

    agent.record_artifacts_after_action = AsyncMock()
    agent._is_multi_field_totp_sequence = MagicMock(return_value=False)
    agent.check_user_goal_complete = AsyncMock()

    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.prepare_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_action_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.random.uniform", lambda *_args, **_kwargs: 0)

    llm_handler_mock = AsyncMock(return_value=json_response)
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *_args, **_kwargs: llm_handler_mock,
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(return_value=False),
    )

    return _StepHarness(agent, task, step, browser_state, page)


def _click_action(task, step, *, element_id: str, action_order: int) -> ClickAction:
    return ClickAction(
        element_id=element_id,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        task_id=task.task_id,
        step_id=step.step_id,
        step_order=step.order,
        action_order=action_order,
    )


def _download_failure_result(status: int) -> ActionSuccess:
    result = ActionSuccess()
    result.download_triggered = False
    result.download_failure_status = status
    return result


def _persisted_download_status(output: AgentStepOutput | None, status: int) -> bool:
    if output is None or not output.actions_and_results:
        return False
    return any(
        result.download_failure_status == status
        for _action, results in output.actions_and_results
        for result in results
    )


def _install_download_then_missing_page(
    harness: _StepHarness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
) -> None:
    """First action records a no-file download failure status; the next ``must_get_working_page()``
    (top of the following action's loop iteration) raises the reachable MissingBrowserStatePage."""
    download_recorded = {"done": False}

    async def _handle_action(*_args, **_kwargs) -> list[ActionSuccess]:
        download_recorded["done"] = True
        return [_download_failure_result(status)]

    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", AsyncMock(side_effect=_handle_action))

    async def _must_get_working_page(*_args, **_kwargs):
        if download_recorded["done"]:
            raise MissingBrowserStatePage(task_id=harness.task.task_id)
        return harness.page

    harness.browser_state.must_get_working_page = AsyncMock(side_effect=_must_get_working_page)


@pytest.mark.asyncio
async def test_agent_step_reraises_cancelled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CancelledError during a step must propagate out of agent_step so the timeout actually halts
    the run, instead of being swallowed into a failed-step return the step loop then retries. The step
    is still persisted as failed before re-raising."""
    harness = _make_harness(monkeypatch, navigation_goal="Reach confirmation page")
    action = _click_action(harness.task, harness.step, element_id="node-1", action_order=0)
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: [action])

    harness.browser_state.must_get_working_page = AsyncMock(return_value=harness.page)
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", AsyncMock(side_effect=CancelledError()))

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

    harness.agent.update_step = AsyncMock(side_effect=fake_update_step)

    with pytest.raises(CancelledError):
        await harness.run()

    # The cancelled step is still recorded as failed (so it is not left orphaned as `running`).
    assert StepStatus.failed in update_statuses


@pytest.mark.asyncio
async def test_known_exception_stamps_download_evidence_on_caller_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable MissingBrowserStatePage raised after a download action recorded its observed HTTP
    failure status is re-raised unchanged, and agent_step stamps the accumulated output onto the
    caller-owned step (exact download status plus step_exception) without performing any
    output-carrying update_step write of its own -- fail_task owns that single durable write."""
    harness = _make_harness(monkeypatch, navigation_goal="Download the statement")
    actions = [
        _click_action(harness.task, harness.step, element_id="node-1", action_order=0),
        _click_action(harness.task, harness.step, element_id="node-2", action_order=1),
    ]
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: actions)
    _install_download_then_missing_page(harness, monkeypatch, status=500)

    output_carrying_writes: list[AgentStepOutput] = []

    async def fake_update_step(
        step: Step,
        status: StepStatus | None = None,
        output: AgentStepOutput | None = None,
        is_last: bool | None = None,
        retry_index: int | None = None,
        **_kwargs,
    ) -> Step:
        if output is not None:
            output_carrying_writes.append(output)
        if status is not None:
            step.status = status
        return step

    harness.agent.update_step = AsyncMock(side_effect=fake_update_step)

    with pytest.raises(MissingBrowserStatePage):
        await harness.run()

    assert not output_carrying_writes, (
        "agent_step must not perform an output-carrying update_step write on the known-exception seam; "
        "fail_task owns the single failed-step write"
    )
    stamped = harness.step.output
    assert stamped is not None, "known-exception seam did not stamp output onto the caller-owned step"
    assert _persisted_download_status(stamped, 500), (
        "known-exception seam did not carry the same-step download failure evidence onto step.output"
    )
    assert stamped.step_exception == "MissingBrowserStatePage", (
        "known-exception seam did not record the exception class on step.output"
    )


@pytest.mark.asyncio
async def test_fail_task_persists_handoff_step_output_then_updates_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """fail_task, handed a still-running step carrying same-step download evidence, issues a single
    failed-step update that passes that exact output through, then follows the terminal update_task
    failure path. This is the durable write the known-exception seam relies on."""
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal="Download the statement", workflow_run_id="workflow-1")
    step = make_step(now, task, step_id="step-seam", status=StepStatus.running, order=0, output=None)
    action = _click_action(task, step, element_id="node-1", action_order=0)
    handoff_output = AgentStepOutput(
        actions_and_results=[(action, [_download_failure_result(500)])],
        step_exception="MissingBrowserStatePage",
    )
    step.output = handoff_output

    agent = ForgeAgent()

    failed_step_writes: list[tuple[StepStatus | None, AgentStepOutput | None]] = []

    async def fake_update_step(
        step: Step,
        status: StepStatus | None = None,
        output: AgentStepOutput | None = None,
        **_kwargs,
    ) -> Step:
        failed_step_writes.append((status, output))
        if status is not None:
            step.status = status
        return step

    agent.update_step = AsyncMock(side_effect=fake_update_step)

    task_statuses: list[TaskStatus | None] = []

    async def fake_update_task(task, status: TaskStatus | None = None, **_kwargs):
        task_statuses.append(status)
        return task

    agent.update_task = AsyncMock(side_effect=fake_update_task)

    result = await agent.fail_task(task=task, step=step, reason="known step exception")

    assert result is True
    assert len(failed_step_writes) == 1, "fail_task must issue exactly one failed-step update"
    failed_status, persisted_output = failed_step_writes[0]
    assert failed_status == StepStatus.failed
    assert persisted_output is handoff_output, "fail_task did not pass the step's exact handoff output through"
    assert task_statuses == [TaskStatus.failed], "fail_task did not follow the terminal update_task failure path"
