"""agent_step exception-seam behavior: cancellation and known-exception download evidence.

Four production seams:

* A CancelledError during a step (elapsed-time timeout / user stop) must propagate out of agent_step
  so the timeout actually halts the run, while the step is still persisted as failed.
* A run canceled while a step's action batch is running must stop before the next action rather than
  at the next step start, so a cancel costs at most the one action already in flight.
* execute_step then ends that task canceled; a canceled parent run owns the teardown, a task-only
  cancel cleans itself up.
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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.exceptions import MissingBrowserStatePage, TaskAlreadyCanceled
from skyvern.forge.agent import ForgeAgent, StepPromptResult
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import MultiFieldTotpAttempt, SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from skyvern.schemas.steps import AgentStepOutput
from skyvern.services.run_cancellation import RunCancellation
from skyvern.webeye.actions.actions import ClickAction, CompleteAction, ExtractAction
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task
from tests.unit.scoped_asyncio import ScopedAsyncio


class _StepHarness:
    def __init__(self, agent: ForgeAgent, task, step: Step, browser_state, page) -> None:
        self.agent = agent
        self.task = task
        self.step = step
        self.browser_state = browser_state
        self.page = page

    async def run(
        self,
        *,
        totp_codes: dict[str, str | None] | None = None,
        multi_field_totp: dict[str, MultiFieldTotpAttempt] | None = None,
    ):
        self.context = SkyvernContext(
            task_id=self.task.task_id,
            step_id=None,
            organization_id=self.task.organization_id,
            workflow_run_id=self.task.workflow_run_id,
            tz_info=ZoneInfo("UTC"),
            totp_codes=totp_codes if totp_codes is not None else {},
            multi_field_totp=multi_field_totp if multi_field_totp is not None else {},
        )
        skyvern_context.set(self.context)
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

    Patches sleep only in the agent module with ``ScopedAsyncio``.
    Callers install their own ``parse_actions`` / ``handle_action`` /
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
    agent.check_user_goal_complete = AsyncMock()

    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.prepare_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_action_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.asyncio", ScopedAsyncio(sleep=AsyncMock(return_value=None)))
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


def _workflow_run(status: WorkflowRunStatus) -> WorkflowRun:
    now = datetime.now(UTC)
    return WorkflowRun(
        workflow_run_id="workflow-1",
        workflow_id="w-1",
        workflow_permanent_id="wpid-1",
        organization_id="o_1",
        status=status,
        created_at=now,
        modified_at=now,
    )


def _cancel_when_flagged(
    monkeypatch: pytest.MonkeyPatch,
    harness: _StepHarness,
    *,
    workflow_run_read_fails: bool = False,
    task_read_fails: bool = False,
) -> dict[str, bool]:
    """Run and task rows read as canceled once the returned flag is set; either read can be made to fail."""
    canceled = {"yet": False}

    async def _get_workflow_run(*_args, **_kwargs):
        if workflow_run_read_fails:
            raise ConnectionError("db blip")
        return _workflow_run(WorkflowRunStatus.canceled if canceled["yet"] else WorkflowRunStatus.running)

    async def _get_task(*_args, **_kwargs):
        if task_read_fails:
            raise ConnectionError("db blip")
        return harness.task.model_copy(
            update={"status": TaskStatus.canceled if canceled["yet"] else TaskStatus.running}
        )

    monkeypatch.setattr(
        "skyvern.services.run_cancellation.app.DATABASE.workflow_runs.get_workflow_run",
        AsyncMock(side_effect=_get_workflow_run),
    )
    monkeypatch.setattr(
        "skyvern.services.run_cancellation.app.DATABASE.tasks.get_task", AsyncMock(side_effect=_get_task)
    )

    async def fake_update_step(step: Step, status: StepStatus | None = None, **_kwargs) -> Step:
        if status is not None:
            step.status = status
        return step

    harness.agent.update_step = AsyncMock(side_effect=fake_update_step)
    return canceled


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

    task_id = harness.task.task_id
    with pytest.raises(CancelledError):
        await harness.run(
            totp_codes={f"{task_id}_secret": "JBSWY3DPEHPK3PXP", f"{task_id}_totp_cache": "123456"},
            multi_field_totp={
                task_id: MultiFieldTotpAttempt(
                    box_element_ids=[f"box-{i}" for i in range(6)],
                    expected_digits=6,
                    code_source="secret",
                )
            },
        )

    # The cancelled step is still recorded as failed (so it is not left orphaned as `running`).
    assert StepStatus.failed in update_statuses
    assert task_id not in harness.context.multi_field_totp
    assert f"{task_id}_secret" not in harness.context.totp_codes
    assert f"{task_id}_totp_cache" not in harness.context.totp_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action_count", "action_fails", "workflow_run_read_fails", "task_read_fails", "expected_handled"),
    [
        pytest.param(2, False, False, False, 1, id="parent-run-canceled"),
        pytest.param(2, False, True, False, 1, id="task-canceled-while-run-read-fails"),
        pytest.param(2, False, True, True, 2, id="fails-open-when-both-reads-fail"),
        pytest.param(1, False, False, False, 1, id="canceled-during-the-last-action"),
        pytest.param(2, True, False, False, 1, id="canceled-during-a-failing-action"),
    ],
)
async def test_canceled_run_stops_before_the_next_action(
    monkeypatch: pytest.MonkeyPatch,
    action_count: int,
    action_fails: bool,
    workflow_run_read_fails: bool,
    task_read_fails: bool,
    expected_handled: int,
) -> None:
    """A cancel landing during an action stops the step before anything after it; a DB error never stops it."""
    harness = _make_harness(monkeypatch, navigation_goal="Submit the application")
    actions = [
        _click_action(harness.task, harness.step, element_id=f"node-{i}", action_order=i) for i in range(action_count)
    ]
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: actions)
    harness.browser_state.must_get_working_page = AsyncMock(return_value=harness.page)
    canceled = _cancel_when_flagged(
        monkeypatch, harness, workflow_run_read_fails=workflow_run_read_fails, task_read_fails=task_read_fails
    )

    async def _handle_action(*_args, **_kwargs) -> list[ActionResult]:
        canceled["yet"] = True
        return [ActionFailure(exception=RuntimeError("element detached"))] if action_fails else [ActionSuccess()]

    handle_action_mock = AsyncMock(side_effect=_handle_action)
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", handle_action_mock)

    step, detailed_output = await harness.run()

    stopped = not task_read_fails
    assert handle_action_mock.await_count == expected_handled
    assert (step.status == StepStatus.canceled) is stopped
    assert (detailed_output.run_cancellation is not None) is stopped
    if stopped:
        harness.agent.check_user_goal_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_canceled_run_stops_after_the_implicit_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel landing during the extraction that follows a complete action still stops the step."""
    harness = _make_harness(monkeypatch, navigation_goal="Submit the application")
    complete = CompleteAction(
        organization_id=harness.task.organization_id,
        task_id=harness.task.task_id,
        step_id=harness.step.step_id,
        step_order=harness.step.order,
        action_order=0,
    )
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: [complete])
    harness.browser_state.must_get_working_page = AsyncMock(return_value=harness.page)
    harness.agent.create_extract_action = AsyncMock(return_value=ExtractAction(task_id=harness.task.task_id))
    canceled = _cancel_when_flagged(monkeypatch, harness)

    async def _handle_action(*args, **kwargs) -> list[ActionSuccess]:
        if isinstance(kwargs.get("action", args[-1] if args else None), ExtractAction):
            canceled["yet"] = True
        return [ActionSuccess()]

    handle_action_mock = AsyncMock(side_effect=_handle_action)
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", handle_action_mock)

    step, detailed_output = await harness.run()

    assert handle_action_mock.await_count == 2
    assert step.status == StepStatus.canceled
    assert detailed_output.run_cancellation is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancellation", "update_task_error"),
    [
        (RunCancellation(TaskStatus.canceled, from_workflow_run=True), None),
        (RunCancellation(TaskStatus.timed_out, from_workflow_run=True), None),
        (
            RunCancellation(TaskStatus.canceled, from_workflow_run=True),
            TaskAlreadyCanceled(new_status="canceled", task_id="task-123"),
        ),
        (RunCancellation(TaskStatus.canceled, from_workflow_run=True), ConnectionError("db blip")),
        (RunCancellation(TaskStatus.canceled, from_workflow_run=False), None),
    ],
    ids=[
        "parent-run-canceled",
        "parent-run-timed-out",
        "parent-run-canceled-task-row-final",
        "parent-run-canceled-task-write-fails",
        "task-canceled",
    ],
)
async def test_execute_step_ends_the_task_when_agent_step_stops_mid_batch(
    monkeypatch: pytest.MonkeyPatch, cancellation: RunCancellation, update_task_error: Exception | None
) -> None:
    """The task takes the status the action poll saw, even if neither row can be read again.

    A stopped parent run keeps the teardown for itself; only a task-level cancel cleans up here.
    """
    parent_run_canceled = cancellation.from_workflow_run
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, workflow_run_id="workflow-1")
    step = make_step(now, task, step_id="step-0", status=StepStatus.running, order=0, output=None)
    canceled_step = make_step(now, task, step_id="step-0", status=StepStatus.canceled, order=0, output=None)
    detailed_output = SimpleNamespace(scraped_page=None, cua_response=None, run_cancellation=cancellation)
    browser_state = MagicMock()
    browser_state.get_working_page = AsyncMock(return_value=MagicMock())

    canceled = {"yet": False}

    async def _agent_step(*_args, **_kwargs):
        canceled["yet"] = True
        return canceled_step, detailed_output

    async def _get_workflow_run(*_args, **_kwargs):
        if canceled["yet"]:
            raise ConnectionError("db blip")
        return _workflow_run(WorkflowRunStatus.running)

    async def _get_task(*_args, **_kwargs):
        if canceled["yet"]:
            raise ConnectionError("db blip")
        return task.model_copy(update={"status": TaskStatus.running})

    async def _update_task(task, status=None, **_kwargs):
        if update_task_error is not None:
            raise update_task_error
        return task.model_copy(update={"status": status})

    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run", AsyncMock(side_effect=_get_workflow_run)
    )
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", AsyncMock(side_effect=_get_task))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.validate_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_step_execution", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.app.ARTIFACT_MANAGER.flush_step_archive", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.record_fail_fast_shadow", AsyncMock(return_value=None))
    agent.initialize_execution_state = AsyncMock(return_value=(step, browser_state, detailed_output))  # type: ignore[method-assign]
    agent.register_async_operations = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent.agent_step = AsyncMock(side_effect=_agent_step)  # type: ignore[method-assign]
    agent.update_task_errors_from_detailed_output = AsyncMock(side_effect=lambda task, _output: task)  # type: ignore[method-assign]
    update_task = AsyncMock(side_effect=_update_task)
    agent.update_task = update_task  # type: ignore[method-assign]
    clean_up_task = AsyncMock(return_value=None)
    agent.clean_up_task = clean_up_task  # type: ignore[method-assign]
    agent.handle_completed_step = AsyncMock()  # type: ignore[method-assign]
    agent.handle_failed_step = AsyncMock()  # type: ignore[method-assign]

    skyvern_context.set(
        SkyvernContext(organization_id=organization.organization_id, task_id=task.task_id, tz_info=ZoneInfo("UTC"))
    )
    try:
        returned_step, _output, next_step = await agent.execute_step(
            organization=organization, task=task, step=step, api_key="api-key", download_baseline_files=[]
        )
    finally:
        skyvern_context.reset()

    assert returned_step.status == StepStatus.canceled
    assert next_step is None
    agent.handle_completed_step.assert_not_awaited()
    agent.handle_failed_step.assert_not_awaited()
    if parent_run_canceled:
        assert update_task.await_args.kwargs["status"] == cancellation.task_status
        clean_up_task.assert_not_awaited()
    else:
        update_task.assert_not_awaited()
        clean_up_task.assert_awaited_once()


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
