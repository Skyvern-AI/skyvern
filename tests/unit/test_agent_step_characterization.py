from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from skyvern.exceptions import NoTOTPVerificationCodeFound, RepeatedActionFailure
from skyvern.forge.agent import ForgeAgent, StepPromptResult, _get_repeated_action_failure
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.tasks import Task
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    ClickAction,
    CompleteAction,
    DownloadFileAction,
    ExtractAction,
    InputTextAction,
    WaitAction,
)
from skyvern.webeye.actions.models import DetailedAgentStepOutput
from skyvern.webeye.actions.responses import ActionFailure, ActionResult, ActionSuccess
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task


def _click(element_id: str = "node-1") -> ClickAction:
    return ClickAction(
        element_id=element_id,
        organization_id="org-123",
        workflow_run_id="workflow-1",
        task_id="task-123",
        step_id="step-char",
        step_order=0,
        action_order=0,
    )


@dataclass
class AgentStepRig:
    agent: ForgeAgent
    organization: Organization
    task: Task
    step: Step
    browser_state: MagicMock
    scraped_page: ScrapedPage
    context: SkyvernContext
    llm_handler: AsyncMock
    action_handler: AsyncMock
    update_statuses: list[StepStatus | None] = field(default_factory=list)

    async def run(self) -> tuple[Step, DetailedAgentStepOutput]:
        skyvern_context.set(self.context)
        try:
            return await self.agent.agent_step(
                task=self.task,
                step=self.step,
                browser_state=self.browser_state,
                organization=self.organization,
            )
        finally:
            skyvern_context.reset()


def make_agent_step_rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parsed_actions: list[Action] | None = None,
    action_handler: AsyncMock | None = None,
    injected_actions: list[Action] | None = None,
    task_overrides: dict[str, Any] | None = None,
    disable_user_goal_check: bool = True,
    repeated_action_breaker: bool = False,
) -> AgentStepRig:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    overrides: dict[str, Any] = {"navigation_goal": "Reach confirmation page", "workflow_run_id": "workflow-1"}
    overrides.update(task_overrides or {})
    task = make_task(now, organization, **overrides)
    step = make_step(now, task, step_id="step-char", status=StepStatus.created, order=0, output=None)

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)
    browser_state.get_working_page = AsyncMock(return_value=page)
    browser_state.reload_page = AsyncMock()

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

    actions = parsed_actions if parsed_actions is not None else [_click()]
    monkeypatch.setattr("skyvern.forge.agent.parse_actions", lambda *_, **__: actions)

    if action_handler is None:
        action_handler = AsyncMock(return_value=[ActionSuccess()])
    monkeypatch.setattr("skyvern.forge.agent.ActionHandler.handle_action", action_handler)
    agent.record_artifacts_after_action = AsyncMock()
    agent._is_multi_field_totp_sequence = MagicMock(return_value=False)
    agent.check_user_goal_complete = AsyncMock()

    llm_handler = AsyncMock(return_value=json_response)
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *_args, **_kwargs: llm_handler,
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.AGENT_FUNCTION.prepare_step_execution",
        AsyncMock(return_value=injected_actions),
    )
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_action_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.asyncio.sleep", AsyncMock(return_value=None))
    monkeypatch.setattr("skyvern.forge.agent.random.uniform", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.workflow_params.create_action", AsyncMock())
    # Wait-time optimization is a cloud experiment (OSS/killswitch-off returns None).
    # Pin that here so the rig never routes into the half-mocked experiment provider,
    # which would cache a malformed WaitConfig in a module-global keyed by task_id and
    # leak "coroutine never awaited" warnings / cross-test state.
    monkeypatch.setattr("skyvern.forge.agent.get_or_create_wait_config", AsyncMock(return_value=None))

    async def _flag(flag_name: str, *_args, **_kwargs) -> bool:
        if flag_name == "DISABLE_USER_GOAL_CHECK":
            return disable_user_goal_check
        if flag_name == "REPEATED_ACTION_CIRCUIT_BREAKER":
            return repeated_action_breaker
        return False

    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(side_effect=_flag),
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
        if output is not None:
            step.output = output
        return step

    agent.update_step = AsyncMock(side_effect=fake_update_step)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    return AgentStepRig(
        agent=agent,
        organization=organization,
        task=task,
        step=step,
        browser_state=browser_state,
        scraped_page=scraped_page,
        context=context,
        llm_handler=llm_handler,
        action_handler=action_handler,
        update_statuses=update_statuses,
    )


@pytest.mark.asyncio
async def test_injected_actions_from_prepare_step_execution_skip_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    injected = _click()
    rig = make_agent_step_rig(monkeypatch, injected_actions=[injected])

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    assert rig.llm_handler.await_count == 0
    assert rig.action_handler.await_count == 1
    assert rig.action_handler.await_args.kwargs["action"] is injected
    assert output.actions == [injected]


@pytest.mark.asyncio
async def test_no_generated_actions_marks_step_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_agent_step_rig(monkeypatch, parsed_actions=[])

    step, output = await rig.run()

    assert step.status == StepStatus.failed
    assert rig.action_handler.await_count == 0
    assert output.actions == []


@pytest.mark.asyncio
async def test_totp_polling_timeout_produces_terminate_action(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_agent_step_rig(monkeypatch, task_overrides={"totp_identifier": "user@example.com"})
    rig.agent.handle_potential_OTP_actions = AsyncMock(side_effect=NoTOTPVerificationCodeFound(task_id="task-123"))

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    assert output.actions is not None
    assert output.actions[0].action_type == ActionType.TERMINATE
    assert "totp_identifier=user@example.com" in output.actions[0].reasoning


@pytest.mark.asyncio
async def test_pdf_viewer_embed_generates_download_action(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_bytes = b"%PDF-1.4 characterization"
    pdf_src = "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode()
    rig = make_agent_step_rig(monkeypatch)
    monkeypatch.setattr(ScrapedPage, "check_pdf_viewer_embed", lambda self: pdf_src)

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    assert output.actions is not None
    action = output.actions[0]
    assert isinstance(action, DownloadFileAction)
    assert action.byte == pdf_bytes
    assert action.download is True
    assert len(rig.context.downloaded_pdf_sources) == 1


@pytest.mark.asyncio
async def test_wait_actions_skipped_when_batched_with_other_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    wait = WaitAction(seconds=3)
    click = _click()
    rig = make_agent_step_rig(monkeypatch, parsed_actions=[wait, click])

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    assert rig.action_handler.await_count == 1
    assert output.actions_and_results is not None
    assert [action for action, _ in output.actions_and_results] == [click]


@pytest.mark.asyncio
async def test_failed_action_marks_step_failed_and_skips_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _click("node-1"), _click("node-2")
    # The stop-the-batch decision is driven by the RESULT's stop_execution_on_failure
    # (default True), not by the action. Set it explicitly to pin the flag-driven path.
    handler = AsyncMock(return_value=[ActionFailure(Exception("element vanished"), stop_execution_on_failure=True)])
    rig = make_agent_step_rig(monkeypatch, parsed_actions=[first, second], action_handler=handler)

    step, output = await rig.run()

    assert step.status == StepStatus.failed
    assert handler.await_count == 1
    # get_clean_detailed_output strips the (second, []) placeholder: only executed actions survive.
    assert output.actions_and_results is not None
    assert len(output.actions_and_results) == 1
    assert output.actions_and_results[0][0] is first
    assert output.actions_and_results[0][1][0].success is False


@pytest.mark.asyncio
async def test_third_identical_failed_persisted_input_surfaces_repeated_action_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = InputTextAction(element_id="framework-managed-input", text="private payload")
    handler = AsyncMock(
        return_value=[ActionFailure(Exception("value did not commit"), stop_execution_on_failure=False)]
    )
    rig = make_agent_step_rig(
        monkeypatch, parsed_actions=[action], action_handler=handler, repeated_action_breaker=True
    )
    previous_steps = []
    for order in range(2):
        previous_action = action.model_copy(update={"step_id": f"step-{order}", "step_order": order})
        persisted_output = AgentStepOutput.model_validate(
            AgentStepOutput(
                action_results=[ActionFailure(Exception("value did not commit"), stop_execution_on_failure=False)],
                actions_and_results=[
                    (
                        previous_action,
                        [ActionFailure(Exception("value did not commit"), stop_execution_on_failure=False)],
                    ),
                ],
                errors=[],
            ).model_dump(mode="json")
        )
        assert persisted_output.actions_and_results is not None
        persisted_action = persisted_output.actions_and_results[0][0]
        assert type(persisted_action) is Action
        assert persisted_action.action_type == ActionType.INPUT_TEXT
        assert persisted_action.element_id == "framework-managed-input"
        assert persisted_action.text == "private payload"
        previous_step = make_step(
            datetime.now(UTC),
            rig.task,
            step_id=f"step-{order}",
            status=StepStatus.failed,
            order=order,
            output=persisted_output,
        )
        previous_steps.append(previous_step)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task_steps",
        AsyncMock(return_value=previous_steps),
    )

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    assert handler.await_count == 1
    assert output.actions_and_results is not None
    repeated_result = output.actions_and_results[-1][1][-1]
    assert repeated_result.exception_type == RepeatedActionFailure.__name__
    assert "framework-managed-input" in (repeated_result.exception_message or "")
    assert "3" in (repeated_result.exception_message or "")
    assert "Exception" in (repeated_result.exception_message or "")
    assert "private payload" not in (repeated_result.exception_message or "")
    assert repeated_result.stop_execution_on_failure is False


@pytest.mark.asyncio
async def test_repeat_ineligible_failure_does_not_load_task_history(monkeypatch: pytest.MonkeyPatch) -> None:
    action = WaitAction(seconds=1)
    history = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task_steps", history)
    rig = make_agent_step_rig(
        monkeypatch,
        parsed_actions=[action],
        action_handler=AsyncMock(return_value=[ActionFailure(Exception("failed"))]),
    )

    await rig.run()

    history.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_action_breaker_disabled_adds_no_db_read_and_no_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off is today's behavior: the failure surfaces as itself and the task-steps history is never
    loaded, so a flag-off org pays nothing for the detector on the failing-action hot path."""
    action = InputTextAction(element_id="framework-managed-input", text="private payload")
    history = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task_steps", history)
    rig = make_agent_step_rig(
        monkeypatch,
        parsed_actions=[action, action.model_copy(), action.model_copy()],
        action_handler=AsyncMock(
            return_value=[ActionFailure(Exception("value did not commit"), stop_execution_on_failure=False)]
        ),
        repeated_action_breaker=False,
    )

    _, output = await rig.run()

    history.assert_not_awaited()
    for _action, results in output.actions_and_results:
        assert results[-1].exception_type == "Exception"
        assert results[-1].exception_type != RepeatedActionFailure.__name__


@pytest.mark.asyncio
async def test_repeated_action_history_loaded_at_most_once_per_step(monkeypatch: pytest.MonkeyPatch) -> None:
    action = InputTextAction(element_id="framework-managed-input", text="private payload")
    history = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task_steps", history)
    rig = make_agent_step_rig(
        monkeypatch,
        parsed_actions=[action, action.model_copy()],
        action_handler=AsyncMock(
            return_value=[ActionFailure(Exception("value did not commit"), stop_execution_on_failure=False)]
        ),
        repeated_action_breaker=True,
    )

    await rig.run()

    assert rig.action_handler.await_count == 2
    assert history.await_count == 1


def test_repeated_action_failure_requires_matching_unsuccessful_attempts() -> None:
    first = InputTextAction(element_id="field", text="first")
    second = InputTextAction(element_id="field", text="second")
    failure = [ActionFailure(Exception("failed"))]

    assert _get_repeated_action_failure([(first, failure), (first, [ActionSuccess()]), (first, failure)]) is None
    assert _get_repeated_action_failure([(first, failure), (second, failure), (first, failure)]) is None


@pytest.mark.asyncio
async def test_failed_action_with_continue_flag_executes_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirror of the skip case: a failure result that opts out of stopping the batch
    # (stop_execution_on_failure=False) lets the loop run every action.
    first, second = _click("node-1"), _click("node-2")
    handler = AsyncMock(return_value=[ActionFailure(Exception("transient"), stop_execution_on_failure=False)])
    rig = make_agent_step_rig(monkeypatch, parsed_actions=[first, second], action_handler=handler)

    step, output = await rig.run()

    # A tolerated failure (stop_execution_on_failure=False) does not fail the step —
    # every action runs and the step still completes.
    assert step.status == StepStatus.completed
    assert handler.await_count == 2
    assert output.actions_and_results is not None
    assert [action for action, _ in output.actions_and_results] == [first, second]


@pytest.mark.asyncio
async def test_skip_remaining_actions_stops_batch_but_step_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _click("node-1"), _click("node-2")
    handler = AsyncMock(return_value=[ActionResult(success=True, skip_remaining_actions=True)])
    rig = make_agent_step_rig(monkeypatch, parsed_actions=[first, second], action_handler=handler)

    step, _output = await rig.run()

    assert step.status == StepStatus.completed
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_refresh_working_page_signal_reloads_and_skips_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_agent_step_rig(monkeypatch)
    rig.context.refresh_working_page = True

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    rig.browser_state.reload_page.assert_awaited_once()
    assert rig.action_handler.await_count == 0
    assert output.actions_and_results is not None
    assert output.actions_and_results[0][0].action_type == ActionType.RELOAD_PAGE
    assert rig.context.refresh_working_page is False


@pytest.mark.asyncio
async def test_unexpected_exception_returns_failed_step_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = AsyncMock(side_effect=RuntimeError("browser exploded"))
    rig = make_agent_step_rig(monkeypatch, action_handler=handler)

    step, output = await rig.run()

    assert step.status == StepStatus.failed
    assert output.step_exception == "RuntimeError"


@pytest.mark.asyncio
async def test_successful_complete_action_with_extraction_goal_appends_extract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = CompleteAction(reasoning="goal reached")
    rig = make_agent_step_rig(monkeypatch, parsed_actions=[complete])
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.get_task", AsyncMock(return_value=rig.task))
    extract = ExtractAction(
        reasoning="collect",
        data_extraction_goal=rig.task.data_extraction_goal,
        data_extraction_schema=None,
    )
    rig.agent.create_extract_action = AsyncMock(return_value=extract)

    step, output = await rig.run()

    assert step.status == StepStatus.completed
    rig.agent.create_extract_action.assert_awaited_once()
    assert rig.action_handler.await_count == 2
    assert output.actions_and_results is not None
    assert output.actions_and_results[-1][0] is extract


@pytest.mark.asyncio
async def test_parallel_verification_marks_speculative_original_status(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_agent_step_rig(monkeypatch, disable_user_goal_check=False)

    step, _output = await rig.run()

    assert step.status == StepStatus.completed
    assert step.speculative_original_status == StepStatus.completed
