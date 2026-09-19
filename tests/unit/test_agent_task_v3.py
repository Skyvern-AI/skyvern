"""Executor-level tests for the Task V3 dispatch path (`ForgeAgent._execute_task_v3`).

The engine tool-loop itself is unit-tested in test_taskv3_*; here we mock it out and
assert the wiring around it: the loop runs once for the whole task, its outcome maps
onto task/step status, the browser actions it reports are emitted as billable
action-results, and the per-step billing hook is invoked with that step so a v3 run
meters per action exactly like the step engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.config import settings
from skyvern.errors.errors import UserDefinedError
from skyvern.exceptions import MissingBrowserStatePage
from skyvern.forge import agent as agent_module
from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.db.exceptions import NotFoundError
from skyvern.forge.sdk.db.utils import hydrate_action
from skyvern.forge.sdk.experimentation.billing_tier import BillingTier
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider
from skyvern.forge.sdk.experimentation.workflow_block_engine import DISABLE_TASK_V3_FLAG
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.forge.sdk.workflow.models.block import (
    ActionBlock,
    BaseTaskBlock,
    ExtractionBlock,
    FileDownloadBlock,
    HumanInteractionBlock,
    LoginBlock,
    NavigationBlock,
    TaskBlock,
    ValidationBlock,
)
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter, OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRunStatus
from skyvern.forge.taskv3.engine import MIN_ACTION_STEPS
from skyvern.forge.taskv3.frame_perception import FRAME_PERCEPTION_FLAG, frame_perception_enabled
from skyvern.forge.taskv3.loop import LoopOutcome, RoundAction
from skyvern.forge.taskv3.run_arms import OBSERVE_DROP_OFFVIEWPORT_UNNAMED_FLAG, run_arm_enabled
from skyvern.schemas.workflows import BlockStatus, BlockType
from skyvern.utils.secret_redaction import REDACTED_SECRET_PLACEHOLDER
from skyvern.webeye.actions.actions import (
    ActionStatus,
    ActionType,
    ClickAction,
    GotoUrlAction,
    HoverAction,
    InputTextAction,
    KeypressAction,
    SelectOptionAction,
    SolveCaptchaAction,
    UploadFileAction,
)
from tests.unit.helpers import make_action_row, make_browser_state, make_organization, make_step, make_task


async def _run_execute_task_v3(
    monkeypatch: pytest.MonkeyPatch,
    outcome: LoopOutcome,
    post_step_side_effect: BaseException | None = None,
    action_rounds: list[list[RoundAction]] | None = None,
    action_round_texts: list[str | None] | None = None,
    screenshot_raises: bool = False,
    task_block: BaseTaskBlock | None = None,
    validation_without_page_information: bool = False,
    provider_probe_calls: int = 0,
    get_working_page_side_effect: list[Any] | None = None,
    must_get_working_page_side_effect: BaseException | list[Any] | None = None,
    loop_raises: BaseException | None = None,
    update_task_side_effect: BaseException | None = None,
    completion_gate_vetoes: bool = False,
    initial_active_credential_parameter_key: str | None = None,
    context_overrides: dict[str, Any] | None = None,
    own_block_row: WorkflowRunBlock | None = None,
    own_block_lookup_raises: BaseException | None = None,
    **task_overrides: Any,
) -> tuple[Step, Any, AsyncMock, AsyncMock]:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, **task_overrides)
    step = make_step(now, task, step_id="step-v3", status=StepStatus.created, order=0, output=None)

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page, side_effect=must_get_working_page_side_effect)
    if get_working_page_side_effect is not None:
        browser_state.get_working_page = AsyncMock(side_effect=get_working_page_side_effect)
    else:
        browser_state.get_working_page = AsyncMock(return_value=page)
    browser_state.take_post_action_screenshot = AsyncMock(
        return_value=b"png-bytes",
        side_effect=RuntimeError("screenshot boom") if screenshot_raises else None,
    )

    async def _loop(**kwargs: Any) -> LoopOutcome:
        # Exposed so tests can probe context state as seen from inside the loop (and, since this
        # runs before any loop_raises, even when the loop goes on to raise).
        loop_mock.context = context
        loop_mock.active_credential_parameter_key_during_loop = context.active_credential_parameter_key
        loop_mock.frame_perception_enabled_during_loop = frame_perception_enabled()
        loop_mock.observe_offviewport_drop_during_loop = run_arm_enabled(
            OBSERVE_DROP_OFFVIEWPORT_UNNAMED_FLAG, forced=False
        )
        cb = kwargs.get("on_action_round")
        if cb is not None and action_rounds:
            for i, round_actions in enumerate(action_rounds):
                turn_text = action_round_texts[i] if action_round_texts and i < len(action_round_texts) else None
                await cb(round_actions, turn_text)
        if provider_probe_calls:
            provider = kwargs["page_provider"]
            loop_mock.resolved_pages = [await provider() for _ in range(provider_probe_calls)]
        if loop_raises is not None:
            raise loop_raises
        return outcome

    loop_mock = AsyncMock(side_effect=_loop)
    loop_mock.browser_state = browser_state
    monkeypatch.setattr("skyvern.forge.taskv3.engine.run_task_v3_agent_loop", loop_mock)
    monkeypatch.setattr("skyvern.forge.agent.LLMCaller", MagicMock())
    monkeypatch.setattr("skyvern.forge.sdk.api.files.resolve_run_download_id", lambda *_a, **_k: "download-1")
    monkeypatch.setattr("skyvern.forge.sdk.api.files.get_download_dir", lambda *_a, **_k: "/tmp/taskv3-test")
    monkeypatch.setattr(
        "skyvern.forge.agent.app.ARTIFACT_MANAGER.create_artifact", AsyncMock(return_value="artifact-1")
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.workflow_params.create_action",
        AsyncMock(side_effect=lambda action: action),
    )
    get_own_block_mock = AsyncMock(return_value=own_block_row, side_effect=own_block_lookup_raises)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_block_by_task_id", get_own_block_mock
    )
    # _execute_task_v3 builds auth tools, whose credential-candidate gate reads the workflow-run
    # context; stub it out so executor tests (which don't exercise auth) don't hit that lookup.
    monkeypatch.setattr("skyvern.services.otp_service.has_credential_totp_candidate", lambda *_a, **_k: False)

    async def fake_update_step(
        step: Step, status: StepStatus | None = None, output: Any = None, **_kwargs: Any
    ) -> Step:
        if status is not None:
            step.status = status
        if output is not None:
            step.output = output
        return step

    async def fake_update_task(task: Any, status: Any = None, **_kwargs: Any) -> Any:
        if status is not None:
            task.status = status
        if "extracted_information" in _kwargs:
            task.extracted_information = _kwargs["extracted_information"]
        if "failure_reason" in _kwargs:
            task.failure_reason = _kwargs["failure_reason"]
        return task

    agent.update_step = AsyncMock(side_effect=fake_update_step)
    agent.update_task = AsyncMock(side_effect=update_task_side_effect or fake_update_task)
    agent.clean_up_task = AsyncMock()

    post_step_mock = AsyncMock(side_effect=post_step_side_effect)
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_step_execution", post_step_mock)
    completion_gate = AsyncMock(return_value=not completion_gate_vetoes)
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.gate_step_completion", completion_gate)
    loop_mock.completion_gate = completion_gate

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=step.step_id,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        validation_without_page_information=validation_without_page_information,
        active_credential_parameter_key=initial_active_credential_parameter_key,
    )
    for name, value in (context_overrides or {}).items():
        setattr(context, name, value)
    skyvern_context.set(context)
    try:
        out_step, out_task = await agent._execute_task_v3(
            task=task,
            step=step,
            browser_state=browser_state,
            organization=organization,
            api_key=None,
            close_browser_on_completion=True,
            browser_session_id=None,
            task_block=task_block,
        )
    finally:
        skyvern_context.reset()

    loop_mock.clean_up_kwargs = agent.clean_up_task.await_args.kwargs if agent.clean_up_task.await_args else {}
    loop_mock.update_task_kwargs = agent.update_task.await_args.kwargs if agent.update_task.await_args else {}
    loop_mock.get_own_block_mock = get_own_block_mock
    return out_step, out_task, loop_mock, post_step_mock


@pytest.mark.asyncio
async def test_execute_task_v3_pins_the_observe_offviewport_arm_before_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TASK_V3_OBSERVE_DROP_OFFVIEWPORT_UNNAMED", False)
    reader = AsyncMock(
        side_effect=lambda flag, *_a, **_k: "treatment" if flag == OBSERVE_DROP_OFFVIEWPORT_UNNAMED_FLAG else None
    )
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "get_value_cached", reader)

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        workflow_run_id="wr_offviewport_reach",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    # Read inside the loop: the context is the same object after the run, so a pin written late would
    # still be visible here -- and every observe call builds its script from the arm as it stands then.
    assert loop_mock.observe_offviewport_drop_during_loop is True
    assert loop_mock.context.run_arms[OBSERVE_DROP_OFFVIEWPORT_UNNAMED_FLAG] == (task.workflow_run_id, "treatment")
    assert [c.args[1] for c in reader.await_args_list if c.args[0] == OBSERVE_DROP_OFFVIEWPORT_UNNAMED_FLAG] == [
        task.workflow_run_id
    ]


@pytest.mark.asyncio
async def test_execute_task_v3_resolves_frame_perception_before_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_execute_task_v3` must actually call `resolve_frame_perception` with the run's real
    identity before the loop starts -- an accessor that reads a hand-pinned context correctly
    proves nothing about whether the real call site still resolves it. `workflow_run_id` is set to
    a value distinct from `task_id` so a passing distinct_id assertion pins the documented
    precedence (workflow_run_id wins) rather than passing because the two happened to match.
    """
    monkeypatch.setattr(settings, "TASK_V3_FRAME_PERCEPTION", False)
    provider = AsyncMock(return_value=True)
    monkeypatch.setattr(app.EXPERIMENTATION_PROVIDER, "is_feature_enabled_cached", provider)

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        workflow_run_id="wr_frame_perception_reach",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    assert task.workflow_run_id == "wr_frame_perception_reach"
    assert task.workflow_run_id != task.task_id

    # As seen from inside the loop, before context is reset.
    assert loop_mock.context.frame_perception_resolved_run_id == task.workflow_run_id
    assert loop_mock.context.frame_perception_flag is True
    assert loop_mock.frame_perception_enabled_during_loop is True

    provider.assert_awaited_once_with(
        FRAME_PERCEPTION_FLAG,
        task.workflow_run_id,
        properties={"organization_id": task.organization_id},
    )


@pytest.mark.asyncio
async def test_execute_task_v3_bills_per_browser_action(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(
        status="completed",
        reason="filled and ready",
        billable_actions=["type", "type", "click"],
        turns=4,
        tool_calls=6,
    )
    step, task, loop_mock, post_step_mock = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )

    assert loop_mock.await_count == 1
    assert step.status == StepStatus.completed

    # Every reported browser action becomes one action-result with a non-empty results list,
    # which is exactly what the per-step billing hook counts.
    pairs = step.output.actions_and_results
    assert len(pairs) == 3
    billable = sum(1 for _action, results in pairs if len(results) > 0)
    assert billable == 3

    # The billing hook is invoked once, with the finalized step carrying those actions.
    post_step_mock.assert_awaited_once()
    billed_step = post_step_mock.await_args.args[1]
    assert billed_step.step_id == step.step_id
    assert len(billed_step.output.actions_and_results) == 3


@pytest.mark.asyncio
async def test_execute_task_v3_no_actions_bills_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A terminate with no browser mutations: the hook still runs (it no-ops on zero actions).
    outcome = LoopOutcome(status="terminated", reason="blocked by captcha", billable_actions=[])
    step, task, _loop, post_step_mock = await _run_execute_task_v3(monkeypatch, outcome)

    # A terminate is a successful step (only a real error fails it), so it is billing-eligible.
    assert step.status == StepStatus.completed
    assert step.output.actions_and_results == []
    post_step_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_task_v3_failure_maps_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(status="budget_exhausted", reason="max_turns reached", billable_actions=[])
    step, task, _loop, post_step_mock = await _run_execute_task_v3(monkeypatch, outcome)

    assert step.status == StepStatus.failed
    # The hook is still called; it self-guards on completed status, so a failed step bills nothing.
    post_step_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_task_v3_failed_run_with_real_actions_completes_step_for_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This one step is the run's billing unit and post-step billing meters only completed steps:
    # a budget-exhausted run that performed real page actions must still bill them, while the
    # task-level status keeps reporting the failure. A canceled run stays unbilled.
    outcome = LoopOutcome(status="budget_exhausted", reason="cap", billable_actions=["click", "type"])
    step, task, _loop, post_step_mock = await _run_execute_task_v3(monkeypatch, outcome)
    assert task.status == TaskStatus.failed
    assert step.status == StepStatus.completed
    billed_step = post_step_mock.await_args.args[1]
    assert billed_step.status == StepStatus.completed

    canceled = LoopOutcome(status="canceled", reason="canceled", billable_actions=["click"])
    step, task, _loop, _post = await _run_execute_task_v3(monkeypatch, canceled)
    assert step.status == StepStatus.canceled


@pytest.mark.asyncio
async def test_execute_task_v3_completed_without_extraction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Completed browsing but no extracted_output for the extraction goal: report failure rather
    # than fabricate an empty result that reads as a successful-but-empty extraction.
    outcome = LoopOutcome(status="completed", reason="filled", billable_actions=["type"], extracted_output=None)
    step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=None,
    )
    # The task-level result is the failure; the step (billing unit) completes because a real
    # billable action ran.
    assert step.status == StepStatus.completed
    assert task.status == TaskStatus.failed


@pytest.mark.asyncio
async def test_execute_task_v3_completed_with_extraction_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(
        status="completed", reason="done", billable_actions=["type"], extracted_output={"confirmation": "XYZ"}
    )
    step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=None,
    )
    assert step.status == StepStatus.completed
    assert task.status == TaskStatus.completed


@pytest.mark.asyncio
async def test_execute_task_v3_billing_error_does_not_fail_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # A billing-hook error (e.g. Stripe send_meter_event re-raising) must be contained: the run
    # already finished, so it must NOT propagate and let execute_step fail_task a completed run.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type", "click"])
    step, task, _loop, post_step_mock = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        post_step_side_effect=RuntimeError("stripe boom"),
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    # The billing hook was invoked and raised, but the run stayed completed (no propagation).
    post_step_mock.assert_awaited_once()
    assert step.status == StepStatus.completed
    assert task.status == TaskStatus.completed


@pytest.mark.asyncio
async def test_execute_task_v3_defaults_step_cap_not_below_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit cap anywhere falls back to the step engine's MAX_STEPS_PER_RUN default, then the v3
    # floor applies — the run never gets fewer than MIN_ACTION_STEPS action rounds.
    from skyvern.config import settings

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, max_steps_per_run=None, data_extraction_goal=None, extracted_information_schema=None
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == max(settings.MAX_STEPS_PER_RUN, MIN_ACTION_STEPS)


@pytest.mark.asyncio
async def test_execute_task_v3_floors_low_explicit_step_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit cap tuned for the step engine but below the v3 floor is raised to MIN_ACTION_STEPS,
    # so the less round-efficient v3 loop isn't starved before it can finish a form.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, max_steps_per_run=7, data_extraction_goal=None, extracted_information_schema=None
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS


@pytest.mark.asyncio
async def test_execute_task_v3_honors_explicit_step_cap_above_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # A generous explicit cap (above the floor) passes through unchanged — the floor only raises.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, max_steps_per_run=30, data_extraction_goal=None, extracted_information_schema=None
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 30


@pytest.mark.asyncio
async def test_execute_task_v3_scales_token_backstop_with_step_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A long block's token need grows with its action-step budget: a high explicit cap must raise
    # the loop's token backstop proportionally, or the run dies at the flat ceiling mid-progress
    # while well inside its step budget.
    from skyvern.forge.taskv3.engine import DEFAULT_MAX_TOKENS

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        max_steps_per_run=2 * MIN_ACTION_STEPS,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 2 * MIN_ACTION_STEPS
    assert loop_mock.await_args.kwargs["max_tokens"] == 2 * DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_execute_task_v3_token_backstop_unchanged_at_or_below_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # At or below the action-step floor the token backstop stays at its historical default — the
    # scaling only ever raises the ceiling for budgets above the floor, never changes small blocks.
    from skyvern.forge.taskv3.engine import DEFAULT_MAX_TOKENS

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, max_steps_per_run=7, data_extraction_goal=None, extracted_information_schema=None
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS
    assert loop_mock.await_args.kwargs["max_tokens"] == DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_execute_task_v3_surfaces_criteria_in_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        navigation_goal="Apply to the job",
        complete_criterion="the confirmation page is shown",
        terminate_criterion="the posting is closed",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "the confirmation page is shown" in goal
    assert "the posting is closed" in goal
    # The precedence rule is gated to validation tasks, and this seam is the only place that can catch
    # the gate regressing: compose_goal's own tests exercise its default, not the policy the caller
    # sets. `make_task` defaults to TaskType.general.
    assert "the completion criterion wins" not in goal


@pytest.mark.asyncio
async def test_execute_task_v3_states_criteria_precedence_only_for_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both criteria can describe one page at once, and the two engines resolved that differently
    (SKY-16193). v1 shows the terminate criterion to a decision-maker on validation tasks only, so
    saying which wins has no measured meaning anywhere else."""
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        navigation_goal="Confirm the store was added",
        complete_criterion="no error message is present",
        terminate_criterion="an error message is present, or the pop up is not available",
        task_type=TaskType.validation,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert "the completion criterion wins" in loop_mock.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_execute_task_v3_excludes_untrusted_complete_criterion_from_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        context_overrides={"complete_criterion_is_untrusted": True},
        navigation_goal="Apply to the job",
        complete_criterion="the confirmation page is shown",
        terminate_criterion="the posting is closed",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "the confirmation page is shown" not in goal
    assert "the posting is closed" in goal


@pytest.mark.asyncio
async def test_execute_task_v3_validates_extraction_against_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    # The model returned a dict missing a required field; parity with the step engine means we
    # repair it against the schema (fill the missing required field) rather than store it raw.
    schema = {
        "type": "object",
        "properties": {"confirmation": {"type": "string"}},
        "required": ["confirmation"],
    }
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type"], extracted_output={})
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=schema,
    )
    assert task.status == TaskStatus.completed
    assert isinstance(task.extracted_information, dict)
    assert "confirmation" in task.extracted_information  # filled by validate_and_fill_extraction_result


@pytest.mark.asyncio
async def test_execute_task_v3_validates_array_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    # An array-typed schema with a list output: the old dict-only guard skipped validation here;
    # it must run and repair list items (fill missing required fields), matching the step engine.
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
            "required": ["name", "price"],
        },
    }
    outcome = LoopOutcome(
        status="completed", reason="done", billable_actions=["type"], extracted_output=[{"name": "Widget"}]
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract all line items",
        extracted_information_schema=schema,
    )
    assert task.status == TaskStatus.completed
    assert isinstance(task.extracted_information, list) and len(task.extracted_information) == 1
    assert "price" in task.extracted_information[0]  # required field filled on the list item


@pytest.mark.asyncio
async def test_execute_task_v3_leaves_shape_mismatched_extraction_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-conforming extraction (wrong shape for the schema) must be left as-is, not laundered by
    # the validator into an all-default stub that still reports completed.
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    outcome = LoopOutcome(
        status="completed", reason="done", billable_actions=["type"], extracted_output="just a string"
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the value",
        extracted_information_schema=schema,
    )
    assert task.status == TaskStatus.completed
    assert task.extracted_information == "just a string"  # left raw, not swapped for {"a": None}


@pytest.mark.asyncio
async def test_execute_task_v3_validates_property_inferred_object_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    # A schema that omits an explicit root "type" but carries "properties" is an object to the step
    # engine's validator; the shape guard must recognize it so a missing required field is repaired,
    # not skipped (regression: the old type=="object" guard left these raw).
    schema = {"properties": {"confirmation": {"type": "string"}}, "required": ["confirmation"]}
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type"], extracted_output={})
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=schema,
    )
    assert task.status == TaskStatus.completed
    assert isinstance(task.extracted_information, dict)
    assert "confirmation" in task.extracted_information  # repaired despite no explicit root "type"


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_registered_secret_from_extracted_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run", lambda *_a, **_k: {"482913"}
    )
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=["type"],
        extracted_output={"confirmation": "verification code 482913 accepted"},
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    assert "482913" not in task.extracted_information["confirmation"]


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_registered_secret_from_persisted_action_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    # A long (>=8 char) secret glued to adjacent alphanumerics: only substring matching (the
    # free-form-prose mode) catches it — a boundary-anchored scrub would leak it.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type"])
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[RoundAction("type", {"selector": "#otp", "text": "sk4829137765"}, True, billable=True)]],
        action_round_texts=["typing the keysk4829137765into the field"],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    # The type action's row, not the terminal decision row appended after it — the decision row
    # carries the loop's own "done" reason, not this round's turn_reasoning.
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    assert "sk4829137765" not in (persisted.reasoning or "")
    assert REDACTED_SECRET_PLACEHOLDER in (persisted.reasoning or "")


@pytest.mark.asyncio
async def test_execute_task_v3_leaves_extraction_unscrubbed_when_redaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A workflow-registered secret is available but the gate is off, so it must not reach the
    # scrub. Engine-minted runtime values still do -- that is the fallback the hoist provides --
    # which is why this pins the gate rather than the scrub being skipped entirely.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run", lambda *_a, **_k: {"482913"}
    )
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=["type"],
        extracted_output={"confirmation": "verification code 482913 accepted"},
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    assert task.extracted_information["confirmation"] == "verification code 482913 accepted"


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_secret_used_as_extraction_dict_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run", lambda *_a, **_k: {"482913"}
    )
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=["type"],
        extracted_output={"482913": "value", "other": "safe"},
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the value",
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    assert "482913" not in task.extracted_information
    assert task.extracted_information["other"] == "safe"


@pytest.mark.asyncio
async def test_execute_task_v3_redacts_deeply_nested_extraction_without_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Built programmatically, well past sys.getrecursionlimit()'s default (1000): a recursive
    # implementation would raise RecursionError here, which — uncaught inside _execute_task_v3 —
    # would propagate before update_task(completed) ever runs.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run", lambda *_a, **_k: {"482913"}
    )
    deep: Any = "leaf-content"
    for _ in range(1500):
        deep = [deep]
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type"], extracted_output=deep)
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the value",
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    unwrapped = task.extracted_information
    for _ in range(1500):
        assert isinstance(unwrapped, list) and len(unwrapped) == 1
        unwrapped = unwrapped[0]
    assert unwrapped == "leaf-content"


@pytest.mark.asyncio
async def test_execute_task_v3_extraction_redaction_respects_word_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An 8+ char secret ("Sunshine1") must not mangle an unrelated value it happens to be a substring
    # of ("MySunshine1Co"), while a genuinely standalone occurrence is still redacted.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"Sunshine1"},
    )
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=["type"],
        extracted_output={"company": "MySunshine1Co", "note": "code: Sunshine1 ok"},
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the value",
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    assert task.extracted_information["company"] == "MySunshine1Co"
    assert "Sunshine1" not in task.extracted_information["note"]


# ---------------------------------------------------------------------------
# Task V3 model selection: task.llm_key -> TASK_V3_LLM_NAME (PostHog) -> TASK_V3_LLM_KEY -> LLM_KEY
# ---------------------------------------------------------------------------


def stub_workflow_block_engine_app(mock_wbe_app: MagicMock, *, billing_tier: BillingTier = BillingTier.UNKNOWN) -> None:
    """Give a patched workflow_block_engine ``app`` an awaitable billing-tier seam.

    Without it, awaiting the bare MagicMock's ``resolve_billing_tier`` raises TypeError, which
    ``_billing_tier_for_arm`` swallows into UNKNOWN (with a logged traceback); the arm is still
    decided by the flag, but with ``billing_tier="unknown"`` in its properties. Only tests that
    assert on that property need this; ``ForgeAgent.execute_step`` never reaches the arm resolver.
    """
    mock_wbe_app.AGENT_FUNCTION.resolve_billing_tier = AsyncMock(return_value=billing_tier)


def _v3_task(llm_key: str | None = None, workflow_permanent_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        llm_key=llm_key, task_id="tsk_x", organization_id="o_test", workflow_permanent_id=workflow_permanent_id
    )


@pytest.mark.asyncio
async def test_resolve_v3_llm_key_prefers_explicit_task_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit task.llm_key wins; the flag is never read.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.get_value_cached",
        AsyncMock(side_effect=AssertionError("flag must not be read when task.llm_key is set")),
    )
    assert await agent_module._resolve_task_v3_llm_key(_v3_task(llm_key="EXPLICIT_KEY")) == "EXPLICIT_KEY"


@pytest.mark.asyncio
async def test_resolve_v3_llm_key_uses_posthog_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.get_value_cached",
        AsyncMock(return_value="OPENAI_GPT5_6_LUNA"),
    )
    monkeypatch.setattr(agent_module.LLMConfigRegistry, "is_registered", lambda _k: True)
    monkeypatch.setattr(agent_module, "is_custom_llm_key", lambda _k: False)
    assert await agent_module._resolve_task_v3_llm_key(_v3_task()) == "OPENAI_GPT5_6_LUNA"


@pytest.mark.asyncio
async def test_resolve_v3_llm_key_sends_wpid_flag_property(monkeypatch: pytest.MonkeyPatch) -> None:
    # wpid-scoped PostHog conditions need workflow_permanent_id in properties; non-workflow
    # tasks send the "not_workflow" sentinel (same convention as the workflow-block-engine flag).
    reader = AsyncMock(return_value="FLAG_KEY")
    monkeypatch.setattr("skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.get_value_cached", reader)
    monkeypatch.setattr(agent_module.LLMConfigRegistry, "is_registered", lambda _k: True)
    monkeypatch.setattr(agent_module, "is_custom_llm_key", lambda _k: False)

    resolved = await agent_module._resolve_task_v3_llm_key(_v3_task(workflow_permanent_id="wpid_123"))
    assert resolved == "FLAG_KEY"
    assert reader.call_args.kwargs["properties"] == {
        "organization_id": "o_test",
        "workflow_permanent_id": "wpid_123",
    }

    await agent_module._resolve_task_v3_llm_key(_v3_task())
    assert reader.call_args.kwargs["properties"] == {
        "organization_id": "o_test",
        "workflow_permanent_id": "not_workflow",
    }

    # The Task row itself never carries the wpid on the execution path (get_task builds it
    # without one); the run context is the reliable source, mirroring the extraction-cache path.
    skyvern_context.set(SkyvernContext(workflow_permanent_id="wpid_from_ctx"))
    try:
        await agent_module._resolve_task_v3_llm_key(_v3_task())
    finally:
        skyvern_context.reset()
    assert reader.call_args.kwargs["properties"] == {
        "organization_id": "o_test",
        "workflow_permanent_id": "wpid_from_ctx",
    }


@pytest.mark.asyncio
async def test_resolve_v3_llm_key_ignores_unregistered_flag_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.get_value_cached", AsyncMock(return_value="TYPO_KEY")
    )
    monkeypatch.setattr(agent_module.LLMConfigRegistry, "is_registered", lambda _k: False)
    monkeypatch.setattr(agent_module, "is_custom_llm_key", lambda _k: False)
    monkeypatch.setattr(agent_module.settings, "TASK_V3_LLM_KEY", "")
    monkeypatch.setattr(agent_module.settings, "LLM_KEY", "DEFAULT_MODEL")
    assert await agent_module._resolve_task_v3_llm_key(_v3_task()) == "DEFAULT_MODEL"


@pytest.mark.asyncio
async def test_resolve_v3_llm_key_falls_back_when_flag_read_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.get_value_cached",
        AsyncMock(side_effect=RuntimeError("posthog down")),
    )
    monkeypatch.setattr(agent_module.settings, "TASK_V3_LLM_KEY", "V3_DIRECT")
    monkeypatch.setattr(agent_module.settings, "LLM_KEY", "DEFAULT_MODEL")
    # TASK_V3_LLM_KEY set → used ahead of LLM_KEY.
    assert await agent_module._resolve_task_v3_llm_key(_v3_task()) == "V3_DIRECT"


@pytest.mark.asyncio
async def test_execute_task_v3_persists_per_action_screenshots_and_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop reports two successful action rounds; the closure must capture one screenshot per
    # round and persist one actions-table row per action (FK'ing the round's screenshot), so the
    # Task API's action_screenshot_urls and GET /tasks/{id}/actions are populated for v3.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click", "type", "click"])
    rounds = [
        [
            RoundAction("click", {"selector": "#a"}, True, billable=True),
            RoundAction("type", {"selector": "#b", "text": "x"}, True, billable=True),
        ],
        [RoundAction("click", {"selector": "#submit"}, True, billable=True)],
    ]
    round_texts = ["clicking the field then typing into it", "submitting the form"]
    step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        action_round_texts=round_texts,
        workflow_run_id="wr_v3test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert step.status == StepStatus.completed
    # One SCREENSHOT_ACTION artifact per action round plus one for the terminal decision row;
    # one actions-table row per action plus the decision row itself.
    assert agent_mod.app.ARTIFACT_MANAGER.create_artifact.await_count == 3
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 4
    persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[:-1]]
    # organization_id/task_id/step_id must be set, or GET /tasks/{id}/actions filters the rows out;
    # workflow_run_id must carry through for workflow-level action attribution (parity with the step engine).
    assert all(a.organization_id == task.organization_id for a in persisted)
    assert all(a.workflow_run_id == task.workflow_run_id for a in persisted)
    assert all(a.task_id == task.task_id and a.step_id == step.step_id for a in persisted)
    assert all(a.screenshot_artifact_id == "artifact-1" for a in persisted)
    assert [a.action_type for a in persisted] == [ActionType.CLICK, ActionType.INPUT_TEXT, ActionType.CLICK]
    # Every action in a round carries that round's turn text as its reasoning -- never the response,
    # which the turn has no per-action value for. intention is never None for a tool in TARGET_VERBS:
    # with no captured name or kind, every row still gets the tool's own FLOOR label.
    assert [a.reasoning for a in persisted] == [round_texts[0], round_texts[0], round_texts[1]]
    assert [a.intention for a in persisted] == ["Clicked an element", "Typed into a text field", "Clicked an element"]
    assert all(a.response is None for a in persisted)


_GOTO_OUTCOME = {
    "requested_url": "https://forms.example.test/contact-us",
    "url": "https://forms.example.test/contact",
    "http_status": 200,
    "page_transitioned": True,
}


@pytest.mark.asyncio
async def test_execute_task_v3_persists_a_navigation_as_a_goto_url_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # SKY-16374: a URL the model typed itself is an action on a par with a click, so it persists as a
    # `goto_url` row carrying the outcome -- where it asked to go, where it landed, what the page
    # answered. It must not meter: nothing billable, and the round does not advance the workflow-run
    # step index (so a navigation cannot inflate a workflow's step budget).
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    rounds = [
        [RoundAction("navigate", {"url": _GOTO_OUTCOME["requested_url"]}, True, None, None, False, _GOTO_OUTCOME)],
        [RoundAction("click", {"selector": "#send"}, True, billable=True)],
    ]
    step, _task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        action_round_texts=["the contact link 404s, typing the contact URL instead", "sending the form"],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list]
    navigation, click = persisted[0], persisted[1]
    assert navigation.action_type == ActionType.GOTO_URL
    assert isinstance(navigation, GotoUrlAction) and navigation.url == _GOTO_OUTCOME["requested_url"]
    assert navigation.status == ActionStatus.completed
    assert navigation.intention == f"Navigated to {_GOTO_OUTCOME['requested_url']}"
    assert navigation.reasoning == "the contact link 404s, typing the contact URL instead"
    assert navigation.screenshot_artifact_id == "artifact-1"  # the round's own screenshot
    assert navigation.response is not None
    assert _GOTO_OUTCOME["url"] in navigation.response and "HTTP 200" in navigation.response
    # Recorded, never metered: the navigation round leaves the step index where it was, so the click
    # that follows it is still round 0, and only the click bills.
    assert (navigation.step_order, click.step_order) == (0, 0)
    assert len(step.output.actions_and_results) == 1


def test_a_navigation_row_calls_a_move_a_move_by_the_same_identity_the_page_was_judged_by() -> None:
    # The row renders "requested -> landed" and "page changed / same page" out of the SAME outcome, so
    # the two have to agree. The handler judged the transition on CANONICAL URLs, so comparing the raw
    # strings here made a browser re-spelling (the model types a bare host, the page reports it with
    # the root slash) read as a move the very next clause calls "same page".
    same_page = {
        "requested_url": "https://forms.example.test",
        "url": "https://forms.example.test/",
        "http_status": 200,
        "page_transitioned": False,
    }
    assert agent_module._taskv3_row_response(same_page) == "https://forms.example.test (HTTP 200, same page)"
    moved = dict(same_page, url="https://forms.example.test/thanks", page_transitioned=True)
    assert agent_module._taskv3_row_response(moved) == (
        "https://forms.example.test -> https://forms.example.test/thanks (HTTP 200, page changed)"
    )


@pytest.mark.asyncio
async def test_execute_task_v3_keeps_a_trailing_navigation_off_the_step_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The workflow-run step budget counts DISTINCT (task_id, step_order) pairs across steps and
    # round-stamped action rows (get_total_unique_progress_round_count_by_task_ids), so a round that
    # bills nothing must never open a pair of its own. A navigation BEFORE a billable round shares
    # the index that round goes on to claim; one AFTER the last billable round has nobody left to
    # share with, so it has to step back onto the index already spent -- the way the decision row does.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])

    async def _persisted(rounds: list[list[RoundAction]]) -> set[int | None]:
        await _run_execute_task_v3(
            monkeypatch,
            outcome,
            action_rounds=rounds,
            data_extraction_goal=None,
            extracted_information_schema=None,
        )
        calls = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list
        return {call.kwargs["action"].step_order for call in calls}

    click_round: list[RoundAction] = [RoundAction("click", {"selector": "#send"}, True, billable=True)]
    nav_round: list[RoundAction] = [
        RoundAction("navigate", {"url": _GOTO_OUTCOME["requested_url"]}, True, None, None, False, _GOTO_OUTCOME)
    ]
    assert await _persisted([click_round]) == {0}
    # Same run, one navigation appended: the budget the run spends must be identical.
    assert await _persisted([click_round, nav_round]) == {0}
    # And a run whose only action is a navigation spends the one unit the Step row already occupies.
    assert await _persisted([nav_round]) == {0}


@pytest.mark.asyncio
async def test_execute_task_v3_persists_a_dead_end_navigation_as_a_failed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The live shape this exists for: two navigations the model typed, the second landing on a hard 404,
    # then the loop's dead-end terminate. The 404 must read as a FAILED goto_url row carrying the status,
    # with the terminate row still following it -- not a terminate out of nowhere.
    from skyvern.forge import agent as agent_mod

    dead_end = {
        "requested_url": "https://forms.example.test/get-in-touch",
        "url": "https://forms.example.test/get-in-touch",
        "http_status": 404,
        "page_transitioned": True,
        "navigation_dead_end": 404,
    }
    outcome = LoopOutcome(
        status="terminated",
        reason="navigation_dead_end: navigate landed on a dead page (HTTP 404)",
        billable_actions=[],
    )
    rounds = [
        [
            RoundAction("navigate", {"url": _GOTO_OUTCOME["requested_url"]}, True, None, None, False, _GOTO_OUTCOME),
            RoundAction("navigate", {"url": dead_end["requested_url"]}, False, None, None, False, dead_end),
        ]
    ]
    _step, _task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list]
    assert [a.action_type for a in persisted] == [ActionType.GOTO_URL, ActionType.GOTO_URL, ActionType.TERMINATE]
    live, dead, terminate = persisted
    assert (live.status, dead.status) == (ActionStatus.completed, ActionStatus.failed)
    assert dead.response is not None and "HTTP 404" in dead.response and "dead end" in dead.response
    assert live.intention == f"Navigated to {_GOTO_OUTCOME['requested_url']}"
    assert dead.intention == f"Tried to navigate to {dead_end['requested_url']}"
    assert terminate.reasoning == outcome.reason


@pytest.mark.asyncio
async def test_execute_task_v3_persists_a_failed_navigation_with_the_reason_it_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A navigate whose tool call errored (a load timeout, the destructive-reload refusal the engine
    # issues on purpose) reached no page, so it reports no outcome -- but a failed row with an empty
    # response reads as a navigation that broke for no reason, which is worse than not showing it. The
    # tool's own error is what the row has to say, and it is scrubbed like every other persisted text.
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    refusal = (
        "already on this page and it has filled fields (including any attached file); reloading it "
        "would discard them. Act on the current page instead. token=sk4829137765"
    )
    outcome = LoopOutcome(status="failed", reason="could not load the page", billable_actions=[])
    rounds = [
        [RoundAction("navigate", {"url": _GOTO_OUTCOME["requested_url"]}, False, None, None, False, None, refusal)],
        # ...and a failure the tool said nothing about still leaves the response empty rather than
        # inventing one.
        [RoundAction("navigate", {"url": _GOTO_OUTCOME["requested_url"]}, False)],
    ]
    _step, _task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list]
    refused, silent = persisted[0], persisted[1]
    assert refused.action_type == ActionType.GOTO_URL
    assert refused.status == ActionStatus.failed
    assert refused.intention == f"Tried to navigate to {_GOTO_OUTCOME['requested_url']}"
    assert refused.response is not None and refused.response.startswith("already on this page")
    assert "sk4829137765" not in refused.response and REDACTED_SECRET_PLACEHOLDER in refused.response
    assert silent.response is None


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_a_secret_out_of_a_navigation_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # A landed URL is page-supplied and its query routinely carries the credential itself (a sign-in
    # link's token). It reaches a persisted, displayed row for the first time here, so it goes through
    # the same scrub the row's args already get -- in the intention AND in the outcome line.
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    landed = {
        "requested_url": "https://forms.example.test/login?token=sk4829137765",
        "url": "https://forms.example.test/account?session=sk4829137765",
        "http_status": 200,
        "page_transitioned": True,
    }
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    rounds = [[RoundAction("navigate", {"url": landed["requested_url"]}, True, None, None, False, landed)]]
    _step, _task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    navigation = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    assert "sk4829137765" not in (navigation.response or "")
    assert "sk4829137765" not in (navigation.intention or "")
    assert "sk4829137765" not in navigation.url
    assert REDACTED_SECRET_PLACEHOLDER in (navigation.response or "")


def test_every_tool_that_pays_for_a_name_has_something_to_say_about_it() -> None:
    # The set that triggers the capture lives with the tools; the verbs that render it live in
    # target_label.py. A tool added to one and not the other pays the probe on every call and throws
    # the answer away, which nothing else in the build would notice.
    from skyvern.forge.taskv3.target_label import TARGET_VERBS
    from skyvern.forge.taskv3.tools import _TARGET_LABEL_TOOL_NAMES

    assert set(TARGET_VERBS) == set(_TARGET_LABEL_TOOL_NAMES)


@pytest.mark.asyncio
async def test_execute_task_v3_persists_the_captured_target_name_as_the_action_intention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of the capture: a turn that emitted only tool calls (no prose, so `reasoning`
    # is None) still leaves every row with a readable label -- a named one where a name was
    # captured, a floor one where it was not. Each name below differs from every other, so no
    # assertion can pass on a field nobody wrote.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click", "type", "click"])
    rounds = [
        [
            RoundAction("click", {"selector": "#a"}, True, "Sign In"),
            RoundAction("type", {"selector": "#b", "text": "x"}, True, "Email address"),
            RoundAction("click", {"selector": "#c"}, True, None),
        ]
    ]
    _step, _task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[:-1]]
    # "type" enriches with its default kind's noun ("field") even though no page kind was captured;
    # "click" has no default kind, so a name enriches bare and a missing one floors generic.
    assert [a.intention for a in persisted] == [
        'Clicked "Sign In"',
        'Typed into the "Email address" field',
        "Clicked an element",
    ]
    assert all(a.reasoning is None for a in persisted)


# Every shape a registered secret has been shown to survive in on this path, kept together so the
# next one is added here rather than in a new file. Each entry is (label_the_page_would_render,
# registered_secret). Scrubbing lost to all of these because it has to match exactly and the page-side
# probe had already reshaped the value; dropping does not care about the shape.
_SECRET_BEARING_LABELS = [
    pytest.param("Saved sk4829137765 to vault", "sk4829137765", id="plain"),
    # A secret past the transport cap: the capture is truncated page-side, so an exact match against
    # the registered whole value can never fire again.
    pytest.param("Session expired: " + "j" * 900, "j" * 900, id="past-the-cap"),
    # Whitespace-bearing: the probe collapses it before Python sees it.
    pytest.param(
        "Saved -----BEGIN KEY----- MIIBOgIBAAJBAK7 to vault", "-----BEGIN KEY-----\nMIIBOgIBAAJBAK7", id="newlines"
    ),
    # Long only because of a whitespace run: collapses UNDER the cap, so the label is accepted while
    # the registered form still reads as over-cap.
    pytest.param(
        "Saved " + "A" * 100 + " " + "B" * 100 + " ok", "A" * 100 + " " * 400 + "B" * 100, id="whitespace-run"
    ),
    # Shorter than 8 characters and glued to letters: the shared prose scrub boundary-anchors these.
    pytest.param("Code123456accepted", "123456", id="short-and-glued"),
    # Zero-width and bidi marks injected INTO the credential, which is what breaks an exact match.
    pytest.param("Saved sk48\u200b2913\u200d7765 ok", "sk4829137765", id="zero-width-inside"),
    pytest.param("Saved sk4829\u202e137765 ok", "sk4829137765", id="bidi-inside"),
    # Case-flipped, since the check casefolds both sides.
    pytest.param("Token ABCdef123456 x", "abcDEF123456", id="case-flipped"),
    # Fullwidth compatibility forms. NFKC folds these; a codepoint blocklist does not, and this is
    # not purely hostile -- East-Asian pages render digits and letters fullwidth as ordinary styling.
    pytest.param(
        "Saved " + "".join(chr(ord(c) + 0xFEE0) for c in "sk4829137765") + " ok", "sk4829137765", id="fullwidth"
    ),
    # Combining marks interspersed through the credential. U+034F (combining grapheme joiner) and
    # U+FE0F (variation selector-16) are category Mn, NOT Cf, so a format-only strip misses them --
    # and they render invisibly, so the label would look exactly like the plaintext secret.
    pytest.param(
        "Saved " + "".join(c + "\u034f" for c in "sk4829137765") + " ok", "sk4829137765", id="combining-joiner"
    ),
    pytest.param(
        "Saved " + "".join(c + "\ufe0f" for c in "sk4829137765") + " ok", "sk4829137765", id="variation-selector"
    ),
]


@pytest.mark.parametrize(("rendered_label", "secret"), _SECRET_BEARING_LABELS)
@pytest.mark.asyncio
async def test_an_element_name_that_touches_a_secret_is_dropped_not_scrubbed(
    monkeypatch: pytest.MonkeyPatch, rendered_label: str, secret: str
) -> None:
    # A page can render what was just typed into it as the field's own accessible name. The name is
    # dropped whole and the row falls back to the kind's floor label, which holds no page text.
    touched, untouched = await _persisted_click_intentions(monkeypatch, rendered_label, {secret})

    assert touched == "Clicked a button"
    assert secret not in touched
    assert untouched == 'Clicked the "Continue to review" button'


@pytest.mark.asyncio
async def test_a_registered_secret_that_reads_like_a_label_is_still_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Words only, so the shape filter passes it: the registered-secret matcher is the only layer that
    # can stop it on a masked run.
    secret = "correct horse battery"
    touched, untouched = await _persisted_click_intentions(monkeypatch, f"Saved {secret.upper()}", {secret})

    assert touched == "Clicked a button"
    assert untouched == 'Clicked the "Continue to review" button'


@pytest.mark.asyncio
async def test_a_configured_secret_is_dropped_from_the_label_even_when_masking_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The label check only decides whether to drop a page-supplied name, so it consults every
    # configured secret; the run's masking opt-out still leaves its reasoning unredacted.
    from skyvern.forge import agent as agent_mod

    secret = "correct horse battery"

    def _configured_secrets(*_a: object, respect_artifact_redaction_flag: bool = True, **_k: object) -> set[str]:
        return set() if respect_artifact_redaction_flag else {secret}

    manager = "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER"
    monkeypatch.setattr(f"{manager}.artifact_redaction_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(f"{manager}.get_secret_values_for_run", _configured_secrets)
    monkeypatch.setattr(f"{manager}.runtime_secret_values_for_artifacts", lambda *_a, **_k: set())
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click", "click"])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[
            [
                RoundAction("click", {"selector": "#a"}, True, f"Saved {secret}", "button"),
                RoundAction("click", {"selector": "#b"}, True, "Continue to review", "button"),
            ]
        ],
        action_round_texts=[f"typing {secret} into the box"],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    calls = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list
    touched, untouched = calls[0].kwargs["action"], calls[1].kwargs["action"]

    assert touched.intention == "Clicked a button"
    assert untouched.intention == 'Clicked the "Continue to review" button'
    assert secret in (touched.reasoning or "")


@pytest.mark.asyncio
async def test_a_short_configured_secret_below_the_redaction_floor_is_still_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A three-digit CVV never reaches the redaction set (numeric floor of six), and the shape filter
    # accepts "CVV 123", so only the unfloored drop-check set can catch it.
    from skyvern.forge import agent as agent_mod

    manager = "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER"
    monkeypatch.setattr(f"{manager}.artifact_redaction_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(f"{manager}.get_secret_values_for_run", lambda *_a, **_k: set())
    monkeypatch.setattr(f"{manager}.runtime_secret_values_for_artifacts", lambda *_a, **_k: set())
    monkeypatch.setattr(f"{manager}.secret_values_for_drop_check", lambda *_a, **_k: {"123"})
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click", "click"])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[
            [
                RoundAction("click", {"selector": "#a"}, True, "CVV 123", "button"),
                RoundAction("click", {"selector": "#b"}, True, "Continue to review", "button"),
            ]
        ],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    calls = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list

    assert calls[0].kwargs["action"].intention == "Clicked a button"
    assert calls[1].kwargs["action"].intention == 'Clicked the "Continue to review" button'


@pytest.mark.asyncio
async def test_a_label_failure_never_logs_a_traceback_that_could_hold_the_drop_check_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The label helper is the only frame holding the unfloored configured secrets; its own failure is
    # logged without exc_info, so no traceback renderer that prints frame locals can reach them.
    from structlog.testing import capture_logs

    from skyvern.forge import agent as agent_mod

    secret = "correct horse battery"
    manager = "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER"
    monkeypatch.setattr(f"{manager}.secret_values_for_drop_check", lambda *_a, **_k: {secret})

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("label composition failed")

    monkeypatch.setattr(agent_mod, "compose_target_intention", _boom)
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    with capture_logs() as logs:
        await _run_execute_task_v3(
            monkeypatch,
            outcome,
            action_rounds=[[RoundAction("click", {"selector": "#a"}, True, f"Saved {secret}", "button")]],
            data_extraction_goal=None,
            extracted_information_schema=None,
        )
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    label_failures = [entry for entry in logs if entry.get("event") == "task_v3 failed to compose action label"]

    # The row still persists, just without a label.
    assert persisted.intention is None
    assert label_failures
    assert all("exc_info" not in entry for entry in label_failures)
    assert secret not in repr(logs)


async def _persisted_click_intentions(
    monkeypatch: pytest.MonkeyPatch, rendered_label: str, secret_values: set[str]
) -> tuple[str | None, str | None]:
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run", lambda *_a, **_k: secret_values
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click", "click"])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[
            [
                RoundAction("click", {"selector": "#a"}, True, rendered_label, "button"),
                # Positive control under the same secret set: without it, a build that dropped every
                # name unconditionally would pass.
                RoundAction("click", {"selector": "#b"}, True, "Continue to review", "button"),
            ]
        ],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    calls = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list
    return calls[0].kwargs["action"].intention, calls[1].kwargs["action"].intention


@pytest.mark.asyncio
async def test_execute_task_v3_persists_action_row_when_screenshot_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A screenshot-capture failure must not lose the action row: it persists with a null screenshot FK.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[RoundAction("click", {"selector": "#a"}, True, billable=True)]],
        screenshot_raises=True,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert step.status == StepStatus.completed
    assert agent_mod.app.ARTIFACT_MANAGER.create_artifact.await_count == 0  # capture raised before create
    # The click row plus the terminal decision row (its screenshot capture also raises, via the
    # same mocked take_post_action_screenshot).
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 2
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    assert persisted.screenshot_artifact_id is None
    assert persisted.organization_id == task.organization_id


@pytest.mark.asyncio
async def test_execute_task_v3_no_action_rounds_persist_only_the_terminal_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A click-free terminal run (e.g. a captcha-blocked terminate) has no per-action rows, but the
    # run's own verdict still persists as exactly one decision row with its own screenshot — the
    # step-detail view for a click-free block has no other row to show.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="terminated", reason="blocked", billable_actions=[])
    await _run_execute_task_v3(monkeypatch, outcome, action_rounds=None)
    assert agent_mod.app.ARTIFACT_MANAGER.create_artifact.await_count == 1
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 1
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args.kwargs["action"]
    assert persisted.action_type == ActionType.TERMINATE
    assert persisted.screenshot_artifact_id == "artifact-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["completed", "cancelled"])
async def test_execute_step_v3_standalone_flushes_llm_artifacts(cancelled: bool) -> None:
    """The V3 dispatch writes buffered LLM artifacts before normal or canceled exit."""
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, data_extraction_goal=None)
    step = make_step(now, task, step_id="step-v3-archive", status=StepStatus.created, order=0, output=None)
    browser_state, _, _ = make_browser_state()
    manager = ArtifactManager()
    storage = MagicMock()
    storage.build_uri.return_value = "s3://bucket/step-v3-archive.zip"
    storage.store_artifact = AsyncMock()
    database = MagicMock()
    database.artifacts.bulk_create_artifacts = AsyncMock()

    async def execute_v3(**kwargs: Any) -> tuple[Step, Any]:
        manager.accumulate_llm_call_to_archive(kwargs["step"], prompt=b"v3 prompt")
        if cancelled:
            raise asyncio.CancelledError()
        return kwargs["step"], kwargs["task"]

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=step.step_id,
        organization_id=task.organization_id,
    )
    skyvern_context.set(context)
    try:
        with (
            patch("skyvern.forge.agent.app") as mock_app,
            patch("skyvern.forge.sdk.artifact.manager.app", mock_app),
            patch("skyvern.forge.sdk.experimentation.workflow_block_engine.app") as mock_wbe_app,
        ):
            mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=None)
            mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
            mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
            mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
            mock_wbe_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
            mock_app.ARTIFACT_MANAGER = manager
            mock_app.STORAGE = storage
            mock_app.DATABASE.artifacts = database.artifacts
            agent.initialize_execution_state = AsyncMock(return_value=(step, browser_state, None))
            agent._execute_task_v3 = AsyncMock(side_effect=execute_v3)  # type: ignore[method-assign]

            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await agent.execute_step(
                        organization=organization,
                        task=task,
                        step=step,
                        engine=agent_module.RunEngine.skyvern_v3,
                        download_baseline_files=[],
                    )
            else:
                result = await agent.execute_step(
                    organization=organization,
                    task=task,
                    step=step,
                    engine=agent_module.RunEngine.skyvern_v3,
                    download_baseline_files=[],
                )
    finally:
        skyvern_context.reset()

    if not cancelled:
        assert result[2] is None
    storage.store_artifact.assert_awaited_once()
    database.artifacts.bulk_create_artifacts.assert_awaited_once()
    written_artifacts = database.artifacts.bulk_create_artifacts.await_args.args[0]
    assert any(artifact.artifact_type.value == "llm_prompt" for artifact in written_artifacts)
    assert step.step_id not in manager._step_archives


# ---------------------------------------------------------------------------
# P2: the v3 dispatch gate for workflow task blocks (_task_block_supports_v3
# and its wiring into ForgeAgent.execute_step)
# ---------------------------------------------------------------------------


def _make_output_parameter(key: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description="test output",
        output_parameter_id=f"op_{key}",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_block(block_cls: type[BaseTaskBlock], label: str = "blk", **overrides: Any) -> BaseTaskBlock:
    return block_cls(label=label, output_parameter=_make_output_parameter(label), **overrides)


def _make_credential_parameter(key: str) -> CredentialParameter:
    now = datetime.now(UTC)
    return CredentialParameter(
        key=key,
        credential_parameter_id=f"cp_{key}",
        workflow_id="w_test",
        credential_id=f"cred_{key}",
        created_at=now,
        modified_at=now,
    )


_ALLOWED_BLOCK_CASES: list[tuple[type[BaseTaskBlock], dict[str, Any]]] = [
    (TaskBlock, {}),
    (NavigationBlock, {"navigation_goal": "Apply to the job"}),
    (LoginBlock, {}),
    (ActionBlock, {}),
    (ValidationBlock, {}),
    (ExtractionBlock, {"data_extraction_goal": "Extract the price"}),
    (FileDownloadBlock, {"complete_on_download": True}),
]
_ALLOWED_BLOCK_IDS = ["task", "navigation", "login", "action", "validation", "extraction", "file_download"]


@pytest.mark.parametrize("block_cls,overrides", _ALLOWED_BLOCK_CASES, ids=_ALLOWED_BLOCK_IDS)
def test_task_block_supports_v3_allows_supported_block_types(
    block_cls: type[BaseTaskBlock], overrides: dict[str, Any]
) -> None:
    assert agent_module._task_block_supports_v3(_make_block(block_cls, **overrides)) is True


def test_task_block_supports_v3_denies_unsupported_block_type() -> None:
    # HUMAN_INTERACTION is a BaseTaskBlock subclass but not in the v3 allow-list.
    assert agent_module._task_block_supports_v3(_make_block(HumanInteractionBlock)) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"complete_on_download": True},
        {"download_suffix": "invoice"},
        {"download_timeout": 5.0},
    ],
    ids=["complete_on_download", "download_suffix", "download_timeout"],
)
def test_task_block_supports_v3_allows_download_semantics(overrides: dict[str, Any]) -> None:
    assert agent_module._task_block_supports_v3(_make_block(ActionBlock, **overrides)) is True


@pytest.mark.parametrize(
    "block_cls", [ActionBlock, TaskBlock, FileDownloadBlock], ids=["action", "task", "file_download"]
)
def test_task_block_supports_v3_allows_complete_on_download_for_non_validation(
    block_cls: type[BaseTaskBlock],
) -> None:
    assert agent_module._task_block_supports_v3(_make_block(block_cls, complete_on_download=True)) is True


def test_task_block_supports_v3_denies_download_gated_validation() -> None:
    # A validation block never acts on the page, so it can't trigger the download it would
    # complete on; this combination must stay on the step engine (SKY-14905).
    assert agent_module._task_block_supports_v3(_make_block(ValidationBlock, complete_on_download=True)) is False


def test_task_block_supports_v3_allows_validation_without_complete_on_download() -> None:
    assert agent_module._task_block_supports_v3(_make_block(ValidationBlock)) is True


class _StepEngineDispatched(BaseException):
    """Raised by the mocked agent_step to prove the gate fell through to the step engine.

    Subclasses BaseException (not Exception) so execute_step's internal `except Exception`
    handlers can't swallow it; it must propagate straight out to the test.
    """


async def _run_execute_step_gate(
    *,
    engine: agent_module.RunEngine,
    task_block: BaseTaskBlock | None,
    experimentation_provider: BaseExperimentationProvider | None = None,
    workflow_run: Any = None,
    **task_overrides: Any,
) -> tuple[AsyncMock, AsyncMock]:
    """Drive ForgeAgent.execute_step through the v3 dispatch gate.

    Returns (mocked _execute_task_v3, mocked agent_step). agent_step is a terminal probe that
    raises a sentinel on call, so a fallthrough is detected without simulating its full body.
    """
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, data_extraction_goal=None, extracted_information_schema=None, **task_overrides)
    step = make_step(now, task, step_id="step-gate", status=StepStatus.created, order=0, output=None)
    browser_state, _, _ = make_browser_state()
    browser_state.get_working_page = AsyncMock(return_value=None)

    v3_mock = AsyncMock(return_value=(step, task))
    step_engine_mock = AsyncMock(side_effect=_StepEngineDispatched)
    agent._execute_task_v3 = v3_mock  # type: ignore[method-assign]
    agent.agent_step = step_engine_mock  # type: ignore[method-assign]
    agent.initialize_execution_state = AsyncMock(return_value=(step, browser_state, None))  # type: ignore[method-assign]

    context = SkyvernContext(task_id=task.task_id, step_id=step.step_id, organization_id=task.organization_id)
    skyvern_context.set(context)
    try:
        with (
            patch("skyvern.forge.agent.app") as mock_app,
            patch("skyvern.forge.sdk.experimentation.workflow_block_engine.app") as mock_wbe_app,
        ):
            mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=None)
            mock_app.DATABASE.tasks.update_task = AsyncMock()
            mock_app.DATABASE.workflow_runs.get_workflow_run = AsyncMock(return_value=workflow_run)
            mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
            if experimentation_provider is not None:
                mock_app.EXPERIMENTATION_PROVIDER = experimentation_provider
                mock_wbe_app.EXPERIMENTATION_PROVIDER = experimentation_provider
            else:
                mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
                mock_wbe_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
            mock_app.ARTIFACT_MANAGER.flush_step_archive = AsyncMock()
            try:
                await agent.execute_step(
                    organization=organization,
                    task=task,
                    step=step,
                    engine=engine,
                    task_block=task_block,
                    download_baseline_files=[],
                )
            except _StepEngineDispatched:
                pass
    finally:
        skyvern_context.reset()
    return v3_mock, step_engine_mock


@pytest.mark.asyncio
@pytest.mark.parametrize("block_cls,overrides", _ALLOWED_BLOCK_CASES, ids=_ALLOWED_BLOCK_IDS)
async def test_execute_step_dispatches_supported_block_types_to_v3(
    block_cls: type[BaseTaskBlock], overrides: dict[str, Any]
) -> None:
    block = _make_block(block_cls, **overrides)
    v3_mock, step_engine_mock = await _run_execute_step_gate(engine=agent_module.RunEngine.skyvern_v3, task_block=block)
    v3_mock.assert_awaited_once()
    assert v3_mock.await_args.kwargs["task_block"] is block
    step_engine_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"complete_on_download": True},
        {"download_suffix": "invoice"},
        {"download_timeout": 5.0},
    ],
    ids=["complete_on_download", "download_suffix", "download_timeout"],
)
async def test_execute_step_dispatches_download_semantics_blocks_to_v3(overrides: dict[str, Any]) -> None:
    block = _make_block(ActionBlock, **overrides)
    v3_mock, step_engine_mock = await _run_execute_step_gate(engine=agent_module.RunEngine.skyvern_v3, task_block=block)
    v3_mock.assert_awaited_once()
    assert v3_mock.await_args.kwargs["task_block"] is block
    step_engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_step_falls_through_to_step_engine_on_unsupported_block_type() -> None:
    block = _make_block(HumanInteractionBlock)
    v3_mock, step_engine_mock = await _run_execute_step_gate(engine=agent_module.RunEngine.skyvern_v3, task_block=block)
    v3_mock.assert_not_awaited()
    step_engine_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_step_v1_engine_block_unaffected() -> None:
    # A v1-engine run always falls to the step engine, regardless of whether the block type
    # would otherwise be v3-eligible.
    block = _make_block(TaskBlock)
    v3_mock, step_engine_mock = await _run_execute_step_gate(engine=agent_module.RunEngine.skyvern_v1, task_block=block)
    v3_mock.assert_not_awaited()
    step_engine_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_step_bare_task_dispatch_unchanged() -> None:
    v3_mock, step_engine_mock = await _run_execute_step_gate(engine=agent_module.RunEngine.skyvern_v3, task_block=None)
    v3_mock.assert_awaited_once()
    assert v3_mock.await_args.kwargs["task_block"] is None
    step_engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_step_bare_task_with_verification_url_dispatches_to_v3() -> None:
    # v3 resolves verification-URL codes via get_verification_code, so a URL no longer pins the task
    # to the step engine.
    v3_mock, step_engine_mock = await _run_execute_step_gate(
        engine=agent_module.RunEngine.skyvern_v3, task_block=None, totp_verification_url="https://totp.example/poll"
    )
    v3_mock.assert_awaited_once()
    step_engine_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_v3_block_consumes_enabled_dispatch_seam_without_legacy_fallback() -> None:
    provider = MagicMock(spec=BaseExperimentationProvider)
    provider.is_feature_enabled_cached = AsyncMock(return_value=False)
    block = _make_block(TaskBlock, label="pure_task", engine=agent_module.RunEngine.skyvern_v3)

    v3_mock, step_engine_mock = await _run_execute_step_gate(
        engine=agent_module.RunEngine.skyvern_v3,
        task_block=block,
        experimentation_provider=provider,
        workflow_run_id="wr_task_v3_pure",
    )

    v3_mock.assert_awaited_once()
    step_engine_mock.assert_not_awaited()
    disable_call = provider.is_feature_enabled_cached.await_args
    assert disable_call.args == (DISABLE_TASK_V3_FLAG, "wr_task_v3_pure")
    assert disable_call.kwargs["properties"] == {
        "organization_id": make_organization(datetime.now(UTC)).organization_id
    }


@pytest.mark.asyncio
async def test_disabled_v3_dispatch_is_not_credited_as_pure() -> None:
    provider = MagicMock(spec=BaseExperimentationProvider)
    provider.is_feature_enabled_cached = AsyncMock(return_value=True)
    block = _make_block(TaskBlock, label="disabled_pure_task", engine=agent_module.RunEngine.skyvern_v3)

    v3_mock, step_engine_mock = await _run_execute_step_gate(
        engine=agent_module.RunEngine.skyvern_v3,
        task_block=block,
        experimentation_provider=provider,
        workflow_run_id="wr_task_v3_disabled",
    )

    v3_mock.assert_not_awaited()
    step_engine_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# P2b: complete_on_download wiring -- the completion_probe/completion_blocker
# handed to run_task_v3_agent_loop, backed by the same finalize-and-rename path
# v1 uses (ForgeAgent._finalize_downloaded_files_for_task).
# ---------------------------------------------------------------------------


async def _run_execute_task_v3_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    drop_file: bool,
    block_cls: type[BaseTaskBlock] = ActionBlock,
    task_id: str = "task-123",
    step_id: str = "step-download",
    download_suffix: str = "invoice",
    dropped_filename: str = "report.pdf",
    loop_fn: Callable[[dict[str, Any], dict[str, Any]], Awaitable[LoopOutcome]] | None = None,
) -> dict[str, Any]:
    """Drive _execute_task_v3 for a complete_on_download block, capturing the loop kwargs.

    The fake loop stands in for run_task_v3_agent_loop: it optionally drops a new file into the
    run's download directory (mimicking a tool call that triggered a download), then awaits the
    completion_probe/completion_blocker it was handed, exactly like the real loop does.

    ``loop_fn``, when given, fully replaces the default drop-then-probe-once loop body -- used to
    drive a block through a custom probe/blocker sequence (e.g. a second block in the same run,
    checked against a baseline that already contains an earlier block's file).
    """
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        task_id=task_id,
        workflow_run_id="wr-download-test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    step = make_step(now, task, step_id=step_id, status=StepStatus.created, order=0, output=None)
    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)
    browser_state.get_working_page = AsyncMock(return_value=page)
    browser_state.take_post_action_screenshot = AsyncMock(return_value=b"png-bytes")

    block = _make_block(block_cls, complete_on_download=True, download_suffix=download_suffix)

    captured: dict[str, Any] = {}

    async def _default_loop_body(**kwargs: Any) -> LoopOutcome:
        if drop_file:
            (tmp_path / dropped_filename).write_bytes(b"file-bytes")
        captured["probe_reason"] = await kwargs["completion_probe"](frozenset())
        captured["blocker_message"] = await kwargs["completion_blocker"](frozenset())
        if captured["probe_reason"]:
            return LoopOutcome(status="completed", reason=captured["probe_reason"], billable_actions=["click"])
        return LoopOutcome(status="budget_exhausted", reason="no download detected", billable_actions=[])

    async def _loop(**kwargs: Any) -> LoopOutcome:
        if loop_fn is not None:
            return await loop_fn(kwargs, captured)
        return await _default_loop_body(**kwargs)

    loop_mock = AsyncMock(side_effect=_loop)
    monkeypatch.setattr("skyvern.forge.taskv3.engine.run_task_v3_agent_loop", loop_mock)
    monkeypatch.setattr("skyvern.forge.agent.LLMCaller", MagicMock())
    # get_path_for_workflow_download_directory (imported by agent.py) resolves through this same
    # module-level name at call time, so patching it here also redirects that call.
    monkeypatch.setattr("skyvern.forge.sdk.api.files.get_download_dir", lambda *_a, **_k: str(tmp_path))
    monkeypatch.setattr(
        "skyvern.forge.agent.app.ARTIFACT_MANAGER.create_artifact", AsyncMock(return_value="artifact-1")
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.workflow_params.create_action",
        AsyncMock(side_effect=lambda action: action),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_block_by_task_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("skyvern.services.otp_service.has_credential_totp_candidate", lambda *_a, **_k: False)
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_step_execution", AsyncMock())
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.gate_step_completion", AsyncMock(return_value=True))

    async def fake_update_step(
        step: Step, status: StepStatus | None = None, output: Any = None, **_kwargs: Any
    ) -> Step:
        if status is not None:
            step.status = status
        if output is not None:
            step.output = output
        return step

    async def fake_update_task(task: Any, status: Any = None, **_kwargs: Any) -> Any:
        if status is not None:
            task.status = status
        if "failure_reason" in _kwargs:
            task.failure_reason = _kwargs["failure_reason"]
        return task

    agent.update_step = AsyncMock(side_effect=fake_update_step)
    agent.update_task = AsyncMock(side_effect=fake_update_task)
    agent.clean_up_task = AsyncMock()

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=step.step_id,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
    )
    skyvern_context.set(context)
    try:
        await agent._execute_task_v3(
            task=task,
            step=step,
            browser_state=browser_state,
            organization=organization,
            api_key=None,
            close_browser_on_completion=True,
            browser_session_id=None,
            task_block=block,
        )
    finally:
        skyvern_context.reset()

    captured["clean_up_kwargs"] = agent.clean_up_task.await_args.kwargs if agent.clean_up_task.await_args else {}
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("block_cls", [ActionBlock, FileDownloadBlock], ids=["action", "file_download"])
async def test_execute_task_v3_download_completion_probe_finalizes_and_ends_the_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, block_cls: type[BaseTaskBlock]
) -> None:
    captured = await _run_execute_task_v3_download(monkeypatch, tmp_path, drop_file=True, block_cls=block_cls)

    assert captured["probe_reason"]
    # Renamed per download_suffix="invoice", same as v1's finalize path.
    assert (tmp_path / "invoice.pdf").exists()
    assert not (tmp_path / "report.pdf").exists()
    # The probe already finalized against the pre-loop baseline; clean_up_task must not
    # finalize again (v1's no-double-finalize contract).
    assert captured["clean_up_kwargs"]["list_files_before"] is None
    assert captured["clean_up_kwargs"]["download_suffix"] == "invoice"


@pytest.mark.asyncio
@pytest.mark.parametrize("block_cls", [ActionBlock, FileDownloadBlock], ids=["action", "file_download"])
async def test_execute_task_v3_download_completion_probe_no_file_blocks_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, block_cls: type[BaseTaskBlock]
) -> None:
    captured = await _run_execute_task_v3_download(monkeypatch, tmp_path, drop_file=False, block_cls=block_cls)

    assert captured["probe_reason"] is None
    assert captured["blocker_message"]
    assert isinstance(captured["blocker_message"], str)


@pytest.mark.asyncio
async def test_execute_task_v3_download_completion_probe_returns_none_when_wait_reports_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A run being canceled races with a download landing: the wait reports the cancellation, but
    # a file already sits in the directory. The probe must not finalize it into a `completed`
    # result out from under the cancellation -- it must return None and leave finalize untouched.
    finalize_mock = AsyncMock()
    monkeypatch.setattr(ForgeAgent, "_wait_for_in_flight_downloads", AsyncMock(return_value=True))
    monkeypatch.setattr(ForgeAgent, "_finalize_downloaded_files_for_task", finalize_mock)

    captured = await _run_execute_task_v3_download(monkeypatch, tmp_path, drop_file=True)

    assert captured["probe_reason"] is None
    finalize_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_v3_download_completion_with_extraction_goal_is_blocker_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A data-extraction goal needs the model to keep the turn and call finish(completed,
    # extracted_output=...) itself; the probe would otherwise end the loop the moment a billable
    # action lands the download, before extraction ever happens.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock, complete_on_download=True, download_suffix="invoice")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_extract",
        data_extraction_goal="Extract the invoice total",
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["completion_probe"] is None
    assert loop_mock.await_args.kwargs["completion_blocker"] is not None


@pytest.mark.asyncio
async def test_execute_task_v3_download_timeout_only_gets_wait_only_probe_no_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # download_timeout alone carries no completion semantics; it must not go inert on v3 -- v1
    # bounds a post-action download-settle wait with it, so v3 gets an equivalent wait-only probe
    # that awaits the same in-flight-download wait and never ends the run or blocks finish.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock, download_timeout=5.0)
    assert block.complete_on_download is False
    wait_mock = AsyncMock()
    monkeypatch.setattr(ForgeAgent, "_wait_for_in_flight_downloads", wait_mock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_wait_only",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    probe = loop_mock.await_args.kwargs["completion_probe"]
    assert probe is not None
    assert loop_mock.await_args.kwargs["completion_blocker"] is None

    result = await probe(frozenset())
    assert result is None
    wait_mock.assert_awaited_once()
    assert wait_mock.await_args.kwargs["timeout_cap"] is not None
    assert isinstance(wait_mock.await_args.kwargs["exhausted"], set)


@pytest.mark.asyncio
async def test_execute_task_v3_download_baseline_is_scoped_per_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Two complete_on_download blocks in the same workflow run, sharing one download directory:
    # block 1 (ActionBlock) lands a.pdf and completes on it; block 2 (FileDownloadBlock) starts
    # with block 1's renamed file already sitting in the directory. Block 2's own baseline -- taken
    # fresh at the top of its own _execute_task_v3 call -- must already contain that leftover file,
    # so it is never mistaken for something block 2 downloaded.
    captured_block1 = await _run_execute_task_v3_download(
        monkeypatch,
        tmp_path,
        drop_file=True,
        block_cls=ActionBlock,
        task_id="task-block-1",
        step_id="step-block-1",
        download_suffix="first.pdf",
        dropped_filename="a.pdf",
    )
    assert captured_block1["probe_reason"]
    assert (tmp_path / "first.pdf").exists()
    assert not (tmp_path / "a.pdf").exists()
    assert captured_block1["clean_up_kwargs"]["list_files_before"] is None

    async def _block2_loop(kwargs: dict[str, Any], captured: dict[str, Any]) -> LoopOutcome:
        # No new file yet: block 2's baseline already contains first.pdf, so the probe must not
        # mistake it for a fresh download, and the blocker must withhold completion.
        captured["probe_before_new_file"] = await kwargs["completion_probe"](frozenset())
        captured["blocker_before_new_file"] = await kwargs["completion_blocker"](frozenset())

        (tmp_path / "b.pdf").write_bytes(b"file-bytes-b")
        captured["probe_after_new_file"] = await kwargs["completion_probe"](frozenset())
        if captured["probe_after_new_file"]:
            return LoopOutcome(status="completed", reason=captured["probe_after_new_file"], billable_actions=["click"])
        return LoopOutcome(status="budget_exhausted", reason="no download detected", billable_actions=[])

    captured_block2 = await _run_execute_task_v3_download(
        monkeypatch,
        tmp_path,
        drop_file=False,
        block_cls=FileDownloadBlock,
        task_id="task-block-2",
        step_id="step-block-2",
        download_suffix="second.pdf",
        loop_fn=_block2_loop,
    )

    assert captured_block2["probe_before_new_file"] is None
    assert isinstance(captured_block2["blocker_before_new_file"], str)
    assert captured_block2["blocker_before_new_file"]
    assert captured_block2["probe_after_new_file"]

    # Only b.pdf was new to block 2's run: it alone is finalized/renamed, and first.pdf (block 1's
    # already-baselined file) is left untouched.
    assert (tmp_path / "second.pdf").exists()
    assert not (tmp_path / "b.pdf").exists()
    assert (tmp_path / "first.pdf").exists()

    assert captured_block2["clean_up_kwargs"]["list_files_before"] is None


@pytest.mark.asyncio
async def test_execute_task_v3_download_completion_excludes_staged_download_persistently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # file_upload's staged http(s) source file sits in the same download directory and never goes
    # away on its own; the probe/blocker must exclude it by name on every later call this run, not
    # just the one call that staged it, or it gets finalized/renamed as if it were a real download.
    async def _loop(kwargs: dict[str, Any], captured: dict[str, Any]) -> LoopOutcome:
        (tmp_path / "in.pdf").write_bytes(b"staged-input-bytes")
        staged = frozenset({"in.pdf"})
        captured["probe_before_download"] = await kwargs["completion_probe"](staged)
        captured["blocker_before_download"] = await kwargs["completion_blocker"](staged)

        (tmp_path / "out.pdf").write_bytes(b"real-download-bytes")
        captured["probe_after_download"] = await kwargs["completion_probe"](staged)
        if captured["probe_after_download"]:
            return LoopOutcome(status="completed", reason=captured["probe_after_download"], billable_actions=["click"])
        return LoopOutcome(status="budget_exhausted", reason="no download detected", billable_actions=[])

    captured = await _run_execute_task_v3_download(monkeypatch, tmp_path, drop_file=False, loop_fn=_loop)

    assert captured["probe_before_download"] is None
    assert isinstance(captured["blocker_before_download"], str)
    assert captured["blocker_before_download"]
    assert captured["probe_after_download"]

    # Only the real download was finalized/renamed per download_suffix; the staged input is
    # untouched -- neither renamed nor deleted.
    assert (tmp_path / "invoice.pdf").exists()
    assert not (tmp_path / "out.pdf").exists()
    assert (tmp_path / "in.pdf").exists()
    assert (tmp_path / "in.pdf").read_bytes() == b"staged-input-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_exists_at_add_time", "expect_finalized"),
    [
        pytest.param(False, True, id="managed_storage_source_not_shadowed"),
        pytest.param(True, False, id="http_staged_source_still_excluded"),
    ],
)
async def test_execute_task_v3_download_completion_staged_add_gated_by_existence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, file_exists_at_add_time: bool, expect_finalized: bool
) -> None:
    # file_upload reports staged_download=<basename> for every source, but managed-file/s3://
    # sources are written to a temp file OUTSIDE the downloads dir -- only http(s) sources are
    # actually staged into it. Recording the name unconditionally would let a genuine later
    # browser download that happens to share the name (upload report.csv, site returns a
    # processed report.csv) get excluded and never finalized. Recording is gated on the file
    # existing in the downloads dir at add()-time, so the http-staged case still gets excluded.
    async def _loop(kwargs: dict[str, Any], captured: dict[str, Any]) -> LoopOutcome:
        staged_downloads = kwargs["staged_downloads"]
        if file_exists_at_add_time:
            (tmp_path / "report.csv").write_bytes(b"staged-input-bytes")
        staged_downloads.add("report.csv")
        if not file_exists_at_add_time:
            (tmp_path / "report.csv").write_bytes(b"real-download-bytes")

        captured["probe_reason"] = await kwargs["completion_probe"](frozenset(staged_downloads))
        if captured["probe_reason"]:
            return LoopOutcome(status="completed", reason=captured["probe_reason"], billable_actions=["click"])
        return LoopOutcome(status="budget_exhausted", reason="no download detected", billable_actions=[])

    captured = await _run_execute_task_v3_download(
        monkeypatch, tmp_path, drop_file=False, download_suffix="processed", loop_fn=_loop
    )

    if expect_finalized:
        assert captured["probe_reason"]
        assert (tmp_path / "processed.csv").exists()
        assert not (tmp_path / "report.csv").exists()
    else:
        assert captured["probe_reason"] is None
        assert (tmp_path / "report.csv").exists()
        assert (tmp_path / "report.csv").read_bytes() == b"staged-input-bytes"


@pytest.mark.asyncio
async def test_execute_task_v3_download_completion_probe_does_not_refinalize_once_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A finish(completed) deferred by the settle gate re-probes after the first probe already
    # finalized the download. Without download_suffix, finalize renames to a fresh random name
    # every call, so re-finalizing on the re-probe would rename the file again and return a
    # different (but still truthy) reason. The cached-reason short-circuit must make every later
    # probe/blocker call a no-op: same reason, same directory listing, one finalize call total.
    original_finalize = ForgeAgent._finalize_downloaded_files_for_task
    finalize_calls = 0

    async def _spy_finalize(self: ForgeAgent, *args: Any, **kwargs: Any) -> Any:
        nonlocal finalize_calls
        finalize_calls += 1
        return await original_finalize(self, *args, **kwargs)

    monkeypatch.setattr(ForgeAgent, "_finalize_downloaded_files_for_task", _spy_finalize)

    async def _loop(kwargs: dict[str, Any], captured: dict[str, Any]) -> LoopOutcome:
        (tmp_path / "a.pdf").write_bytes(b"file-bytes")
        captured["probe_reason_1"] = await kwargs["completion_probe"](frozenset())
        captured["listing_1"] = sorted(p.name for p in tmp_path.iterdir())

        captured["probe_reason_2"] = await kwargs["completion_probe"](frozenset())
        captured["listing_2"] = sorted(p.name for p in tmp_path.iterdir())

        captured["blocker_reason_3"] = await kwargs["completion_blocker"](frozenset())
        captured["listing_3"] = sorted(p.name for p in tmp_path.iterdir())

        if captured["probe_reason_1"]:
            return LoopOutcome(status="completed", reason=captured["probe_reason_1"], billable_actions=["click"])
        return LoopOutcome(status="budget_exhausted", reason="no download detected", billable_actions=[])

    captured = await _run_execute_task_v3_download(
        monkeypatch, tmp_path, drop_file=False, download_suffix=None, loop_fn=_loop
    )

    assert captured["probe_reason_1"]
    assert captured["probe_reason_2"] == captured["probe_reason_1"]
    assert captured["blocker_reason_3"] is None

    assert captured["listing_2"] == captured["listing_1"]
    assert captured["listing_3"] == captured["listing_1"]

    assert finalize_calls == 1


# ---------------------------------------------------------------------------
# P3: block-true budgets, task_type-aware goal framing, workflow-cancel detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_task_v3_action_block_step_cap_not_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    # An action block's budget is its contract — one action round — not a step-engine-sized number
    # to translate, so the floor leaves it alone. (A round is not a single tool call: the cap bounds
    # rounds, and one round can dispatch a batch.)
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(ActionBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.action,
        max_steps_per_run=1,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 1


@pytest.mark.asyncio
async def test_execute_task_v3_action_block_not_floored_when_task_type_left_at_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # task_type is a defaulted field, so a block that reaches execution without it set would look
    # general. The block class settles it: an action block keeps its budget either way, because
    # over-flooring multiplies what one block may spend and under-flooring only costs rounds.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(ActionBlock)
    assert block.task_type == TaskType.general
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        max_steps_per_run=1,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 1


@pytest.mark.asyncio
async def test_execute_task_v3_validation_block_step_cap_not_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same for a validation block's deliberate 1-attempt-plus-retry budget.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ValidationBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.validation,
        max_steps_per_run=2,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 2


@pytest.mark.asyncio
async def test_execute_task_v3_null_task_type_still_floors_a_general_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # task_type is nullable on the task row, and `None != TaskType.general` is true — so a naive
    # comparison would read NULL as "owns its budget" and skip the floor for every block class.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=None,
        max_steps_per_run=7,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS


@pytest.mark.asyncio
async def test_execute_task_v3_non_general_task_type_alone_skips_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The task_type signal stands on its own, so a block class that isn't Action/Validation but
    # declares an atomic task_type keeps its budget. Nothing produces this pairing today; it is
    # here so the semantic signal keeps working if a future block type adopts one.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.action,
        max_steps_per_run=3,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 3


@pytest.mark.asyncio
async def test_execute_task_v3_general_block_step_cap_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    # The regression this floor exists for: a navigation block carrying a step-engine-sized cap ran
    # out of action rounds mid-form. A general-purpose block now gets the same translation a bare
    # task gets, because the same unit mismatch applies to it.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        max_steps_per_run=7,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS


def test_min_action_steps_is_pinned() -> None:
    # The constant is this change's headline risk, and every other assertion here is written against
    # the symbol — so without this line it could be retuned to anything and the suite would stay green.
    # Moving it is fine; moving it without a deliberate edit here is not.
    assert MIN_ACTION_STEPS == 24


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_cls", "block_kwargs"),
    [
        (TaskBlock, {}),
        (NavigationBlock, {"navigation_goal": "Apply to the job"}),
        (LoginBlock, {}),
        (ExtractionBlock, {"data_extraction_goal": "Grab the confirmation number"}),
    ],
)
async def test_execute_task_v3_every_general_block_type_is_floored(
    monkeypatch: pytest.MonkeyPatch, block_cls: type[BaseTaskBlock], block_kwargs: dict[str, Any]
) -> None:
    # All four carry a general-purpose budget sized in step-engine steps, so all four get translated.
    # Extraction is included deliberately: it reads rather than acts, but it holds the full browser
    # tool set, so its cap can bind like any other.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(block_cls, **block_kwargs)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        max_steps_per_run=7,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS


@pytest.mark.asyncio
async def test_execute_task_v3_general_block_generous_cap_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # The floor only ever raises: a block budgeted above it keeps exactly what its author wrote.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        max_steps_per_run=40,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 40


@pytest.mark.asyncio
async def test_execute_task_v3_workflow_run_ceiling_still_beats_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The org's workflow-run-wide ceiling is a cost control, not a step-engine artifact, so it is
    # applied after the floor and wins. The remaining budget is chosen to sit strictly between the
    # authored cap and the floor: 10 can only be the answer if the floor ran first and the ceiling
    # then cut it back. Clamping before flooring would return the floor instead.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    monkeypatch.setattr(ForgeAgent, "_check_workflow_run_step_budget", AsyncMock(return_value=(41, 50)))
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_ceiling",
        max_steps_per_run=7,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert 7 < 10 < MIN_ACTION_STEPS
    assert loop_mock.await_args.kwargs["max_action_steps"] == 10
    # The pool remainder is also handed to the loop as a HARD ceiling, so the in-loop budget
    # extension can never grant rounds the org-wide pool cannot fund.
    assert loop_mock.await_args.kwargs["max_action_steps_ceiling"] == 10


@pytest.mark.asyncio
async def test_execute_task_v3_workflow_ceiling_above_cap_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pool remainder larger than the block's own cap doesn't clamp the cap, but still flows to the
    # loop as the ceiling so an extension can only grow into what the pool actually has left.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    monkeypatch.setattr(ForgeAgent, "_check_workflow_run_step_budget", AsyncMock(return_value=(1, 30)))
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_pool_above_cap",
        max_steps_per_run=7,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS
    assert loop_mock.await_args.kwargs["max_action_steps_ceiling"] == 30


@pytest.mark.asyncio
async def test_execute_task_v3_atomic_block_ceiling_pinned_to_its_own_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # A block that owns a deliberately small budget (action/validation) must not have it extended:
    # the hard ceiling is pinned to the cap itself, so the in-loop extension is refused.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(ActionBlock, navigation_goal="Click the confirm button")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        max_steps_per_run=5,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == 5
    assert loop_mock.await_args.kwargs["max_action_steps_ceiling"] == 5


@pytest.mark.asyncio
async def test_execute_task_v3_no_workflow_ceiling_without_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    # No org pool -> no hard ceiling: the loop's extension is bounded only by its own gate.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Apply to the job")
    monkeypatch.setattr(ForgeAgent, "_check_workflow_run_step_budget", AsyncMock(return_value=None))
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_no_pool",
        max_steps_per_run=7,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["max_action_steps_ceiling"] is None


@pytest.mark.asyncio
async def test_execute_task_v3_bare_task_step_cap_still_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, max_steps_per_run=2, data_extraction_goal=None, extracted_information_schema=None
    )
    assert loop_mock.await_args.kwargs["max_action_steps"] == MIN_ACTION_STEPS


@pytest.mark.asyncio
async def test_execute_task_v3_validation_block_goal_has_assessment_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ValidationBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.validation,
        complete_criterion="the confirmation banner is shown",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "assessment task" in goal
    assert "do not modify page state" in goal
    assert "no further page perception" not in goal


@pytest.mark.asyncio
async def test_execute_task_v3_validation_without_page_information_adds_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ValidationBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.validation,
        validation_without_page_information=True,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "page-free assessment" in goal
    assert "Do not call observe or get_html" in goal


@pytest.mark.asyncio
async def test_execute_task_v3_action_block_goal_has_single_action_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.action,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "single, focused action" in goal


@pytest.mark.asyncio
async def test_execute_task_v3_general_block_task_goal_has_no_framing(monkeypatch: pytest.MonkeyPatch) -> None:
    # task_type=general (the default) gets no task-type framing, even for a block task.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "assessment task" not in goal
    assert "single, focused action" not in goal


@pytest.mark.asyncio
async def test_execute_task_v3_should_cancel_true_when_workflow_run_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A workflow task must also stop when its parent run is canceled, not just the task row
    # itself (mirrors the legacy step-engine check).
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task",
        AsyncMock(return_value=SimpleNamespace(status=TaskStatus.running)),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(status=WorkflowRunStatus.canceled)),
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        workflow_run_id="wr_cancel_test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    should_cancel = loop_mock.await_args.kwargs["should_cancel"]
    assert await should_cancel() is True


@pytest.mark.asyncio
async def test_execute_task_v3_should_cancel_fails_open_on_workflow_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient DB error on the parent-run poll must not raise out of the loop (which the
    # execute_step catch-all would convert into a failed task) — it means "don't cancel yet".
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task",
        AsyncMock(return_value=SimpleNamespace(status=TaskStatus.running)),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run",
        AsyncMock(side_effect=ConnectionError("db blip")),
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        workflow_run_id="wr_cancel_test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    should_cancel = loop_mock.await_args.kwargs["should_cancel"]
    assert await should_cancel() is False


@pytest.mark.asyncio
async def test_execute_task_v3_should_cancel_skips_workflow_read_for_bare_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No workflow_run_id -> the extra workflow-run read never happens, even if it would say
    # canceled -- the extra read is only paid for workflow tasks.
    workflow_run_mock = AsyncMock(return_value=SimpleNamespace(status=WorkflowRunStatus.canceled))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run", workflow_run_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task",
        AsyncMock(return_value=SimpleNamespace(status=TaskStatus.running)),
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, workflow_run_id=None, data_extraction_goal=None, extracted_information_schema=None
    )
    should_cancel = loop_mock.await_args.kwargs["should_cancel"]
    assert await should_cancel() is False
    workflow_run_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# P4: the page provider (live re-resolution for workflow blocks, once for bare tasks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_task_v3_bare_task_provider_resolves_page_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare task must preserve today's exact semantics: must_get_working_page grabs the page once
    # up front, and every later provider call returns that same object, not a re-resolved one.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        provider_probe_calls=3,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.resolved_pages == [loop_mock.resolved_pages[0]] * 3
    loop_mock.browser_state.must_get_working_page.assert_awaited_once()
    # The completion gate reads the page once on a completed outcome; the PROVIDER itself never
    # consults get_working_page for a bare task.
    assert loop_mock.browser_state.get_working_page.await_count <= 1


@pytest.mark.asyncio
async def test_execute_task_v3_workflow_provider_resolves_live_working_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A workflow block's provider must re-acquire the working page on every call through the
    # recovering accessor (must_get_working_page), so a popup/new tab is followed and a crashed
    # page gets a reopen attempt, matching the step engine's per-action re-acquisition.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    probe, page_a, page_b, page_c = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    block = _make_block(ActionBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        provider_probe_calls=3,
        must_get_working_page_side_effect=[probe, page_a, page_b, page_c],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.resolved_pages == [page_a, page_b, page_c]
    # 1 fail-fast start probe + 3 per-call resolutions, all through the recovering accessor.
    assert loop_mock.browser_state.must_get_working_page.await_count == 4
    # One get_working_page read comes from the completion gate, none from the provider.
    assert loop_mock.browser_state.get_working_page.await_count <= 1


@pytest.mark.asyncio
async def test_execute_task_v3_workflow_block_fails_fast_when_page_gone_at_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the page is already lost at block start, the task must fail before any LLM turn is spent
    # (parity with the pre-provider must_get_working_page raise), not grind to budget exhaustion.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock)
    with pytest.raises(MissingBrowserStatePage):
        await _run_execute_task_v3(
            monkeypatch,
            outcome,
            task_block=block,
            must_get_working_page_side_effect=MissingBrowserStatePage(),
            data_extraction_goal=None,
            extracted_information_schema=None,
        )


# ---------------------------------------------------------------------------
# P5: block-scoped credential TOTP disambiguation -- a block with exactly one
# login-credential parameter pins active_credential_parameter_key for the loop's
# duration (mirrors v1's handler.py get_actual_value_of_parameter_if_secret).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_task_v3_single_credential_sets_active_key_during_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock, parameters=[_make_credential_parameter("cred_1")])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        initial_active_credential_parameter_key="pre_existing_key",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.active_credential_parameter_key_during_loop == "cred_1"
    assert loop_mock.context.active_credential_parameter_key == "pre_existing_key"


@pytest.mark.asyncio
async def test_execute_task_v3_two_credential_params_leaves_active_key_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(
        ActionBlock,
        parameters=[_make_credential_parameter("cred_1"), _make_credential_parameter("cred_2")],
    )
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        initial_active_credential_parameter_key="pre_existing_key",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.active_credential_parameter_key_during_loop == "pre_existing_key"
    assert loop_mock.context.active_credential_parameter_key == "pre_existing_key"


@pytest.mark.asyncio
async def test_execute_task_v3_zero_credential_params_leaves_active_key_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        initial_active_credential_parameter_key="pre_existing_key",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.active_credential_parameter_key_during_loop == "pre_existing_key"
    assert loop_mock.context.active_credential_parameter_key == "pre_existing_key"


@pytest.mark.asyncio
async def test_execute_task_v3_active_credential_key_restored_when_loop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock, parameters=[_make_credential_parameter("cred_1")])
    with pytest.raises(RuntimeError, match="loop boom"):
        await _run_execute_task_v3(
            monkeypatch,
            outcome,
            task_block=block,
            initial_active_credential_parameter_key="pre_existing_key",
            loop_raises=RuntimeError("loop boom"),
            data_extraction_goal=None,
            extracted_information_schema=None,
        )
    from skyvern.forge.taskv3 import engine as engine_module

    loop_mock = engine_module.run_task_v3_agent_loop
    assert loop_mock.active_credential_parameter_key_during_loop == "cred_1"
    assert loop_mock.context.active_credential_parameter_key == "pre_existing_key"


@pytest.mark.asyncio
async def test_execute_task_v3_threads_secret_resolver_for_block_tasks_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Block tasks get fill-time placeholder resolution via the step engine's own helper; bare
    # tasks keep typing the literal text (no workflow context to resolve against).
    resolver_mock = MagicMock(return_value="real-value")
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler.get_actual_value_of_parameter_if_secret_with_task",
        resolver_mock,
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(ActionBlock)
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    resolve_typed_text = loop_mock.await_args.kwargs["resolve_typed_text"]
    assert resolve_typed_text is not None
    assert resolve_typed_text("placeholder_x") == "real-value"
    resolver_mock.assert_called_once_with(task, "placeholder_x")

    _step, _task, bare_loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert bare_loop_mock.await_args.kwargs["resolve_typed_text"] is None


@pytest.mark.asyncio
async def test_execute_task_v3_block_tasks_get_verify_first_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    # Blocks resume mid-workflow: the goal must instruct verifying the criterion against full page
    # text before acting, and forbid leaving the flow. Bare tasks get none of that.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Open the summary page")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "ALREADY" in goal and "never sign out" in goal

    _step, _task, bare_loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert "never sign out" not in bare_loop_mock.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_execute_task_v3_should_cancel_detects_timed_out_reaper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The stuck-run reaper sets timed_out (not canceled) on both the run and its child tasks;
    # the poll must stop the loop for either status on either axis.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task",
        AsyncMock(return_value=SimpleNamespace(status=TaskStatus.timed_out)),
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, workflow_run_id="wr_reaped", data_extraction_goal=None, extracted_information_schema=None
    )
    assert await loop_mock.await_args.kwargs["should_cancel"]() is True

    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task",
        AsyncMock(return_value=SimpleNamespace(status=TaskStatus.running)),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.workflow_runs.get_workflow_run",
        AsyncMock(return_value=SimpleNamespace(status=WorkflowRunStatus.timed_out)),
    )
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, workflow_run_id="wr_reaped", data_extraction_goal=None, extracted_information_schema=None
    )
    assert await loop_mock.await_args.kwargs["should_cancel"]() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("flavor", ["timed_out", "canceled"])
async def test_execute_task_v3_keeps_externally_finalized_terminal_status(
    monkeypatch: pytest.MonkeyPatch, flavor: str
) -> None:
    # An external finalizer (reaper -> timed_out, cancel API -> canceled) can win the race while
    # the loop is stopping; the v3 finalization must keep that status instead of raising into the
    # failure path. Webhook asymmetry matches v1: the cancel API already webhooks synchronously
    # (suppress the duplicate), the reaper path does not (send).
    from skyvern.exceptions import TaskAlreadyCanceled, TaskAlreadyTimeout

    if flavor == "timed_out":
        side_effect: BaseException = TaskAlreadyTimeout("tsk_reaped")
        final_row = SimpleNamespace(status=TaskStatus.timed_out)
        expect_webhook = True
    else:
        side_effect = TaskAlreadyCanceled("canceled", "tsk_reaped")
        final_row = SimpleNamespace(status=TaskStatus.canceled)
        expect_webhook = False
    outcome = LoopOutcome(status="canceled", reason="run canceled", billable_actions=[])
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.tasks.get_task",
        AsyncMock(return_value=final_row),
    )
    step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        update_task_side_effect=side_effect,
        workflow_run_id="wr_reaped",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task is final_row
    assert loop_mock.clean_up_kwargs["need_call_webhook"] is expect_webhook


def test_redact_extracted_information_disambiguates_colliding_secret_keys() -> None:
    result = agent_module._redact_extracted_information(
        {"482913": "codeA", "735264": "codeB", "other": "safe"},
        {"482913", "735264"},
    )
    assert result == {
        "[REDACTED_SECRET]": "codeA",
        "[REDACTED_SECRET]#2": "codeB",
        "other": "safe",
    }


@pytest.mark.asyncio
async def test_execute_task_v3_completion_gate_veto_fails_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # The deployment completion gate (e.g. a submit block requiring a deterministic confirmation)
    # must be able to veto a v3 finish(completed); the veto fails safe instead of falsely completing.
    outcome = LoopOutcome(status="completed", reason="looks done", billable_actions=["click"])
    block = _make_block(NavigationBlock, navigation_goal="Submit the application")
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        completion_gate_vetoes=True,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.failed
    loop_mock.completion_gate.assert_awaited_once()

    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    kwargs = loop_mock.completion_gate.await_args.kwargs
    assert kwargs["task_block"] is block


@pytest.mark.asyncio
async def test_execute_task_v3_threads_workflow_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    # A workflow-level system prompt carries customer behavioral/compliance instructions; the
    # step engine passes it on every LLM call, so v3 must surface it in its system guidance.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Open the page")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_system_prompt="Always use formal salutations.",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert "Always use formal salutations." in loop_mock.await_args.kwargs["extra_system_guidance"]


@pytest.mark.asyncio
async def test_execute_task_v3_page_free_validation_prompt_has_no_perception_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # without_page_information validations judge only durable inputs; the goal must not
    # simultaneously instruct reading the page (the contradiction codex flagged).
    outcome = LoopOutcome(status="completed", reason="ok", billable_actions=[])
    block = _make_block(ValidationBlock, complete_criterion="The data is consistent")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        validation_without_page_information=True,
        task_type=TaskType.validation,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "page-free assessment" in goal
    # No perception INSTRUCTIONS: neither the verify-first preamble nor the grounding clause
    # (the page-free text itself may name the tools only to forbid them).
    assert "First read" not in goal
    assert "read the page's visible text (get_html with format text) before concluding" not in goal
    assert "Do not call observe or get_html" in goal


@pytest.mark.asyncio
async def test_execute_task_v3_actions_are_round_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each action row carries its ROUND index in step_order, so the workflow-run step budget
    # counts v3 rounds exactly (distinct (task, order) pairs across steps and actions).
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type", "type", "click"])
    rounds = [
        [
            RoundAction("type", {"selector": "#a"}, True, billable=True),
            RoundAction("type", {"selector": "#b"}, True, billable=True),
        ],
        [RoundAction("click", {"selector": "#go"}, True, billable=True)],
    ]
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, action_rounds=rounds, data_extraction_goal=None, extracted_information_schema=None
    )
    create_action = agent_module.app.DATABASE.workflow_params.create_action
    action_rows = create_action.await_args_list[:-1]
    stamped = [(c.kwargs["action"].step_order, c.kwargs["action"].action_order) for c in action_rows]
    assert stamped == [(0, 0), (0, 1), (1, 2)]
    # The terminal decision row rides the LAST consumed billable index — a fresh index would read
    # as a new distinct (task, step_order) pair to the workflow-run step budget.
    decision = create_action.await_args_list[-1].kwargs["action"]
    assert (decision.step_order, decision.action_order) == (1, 3)


@pytest.mark.asyncio
async def test_execute_task_v3_workflow_run_step_budget_caps_and_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The org-wide workflow-run step ceiling binds v3 blocks: remaining budget caps the action
    # rounds, and an exhausted budget fails the block before any LLM turn is spent.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Go", max_steps_per_run=10)
    monkeypatch.setattr(
        ForgeAgent,
        "_check_workflow_run_step_budget",
        AsyncMock(return_value=(47, 50)),
    )
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_budget",
        max_steps_per_run=10,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    # 47 counted includes this block's own fresh placeholder step; credited back -> 50-46=4.
    # 47 counted includes this block's own fresh placeholder step; credited back -> 50-46=4.
    assert loop_mock.await_args.kwargs["max_action_steps"] == 4

    monkeypatch.setattr(
        ForgeAgent,
        "_check_workflow_run_step_budget",
        AsyncMock(return_value=(51, 50)),
    )
    step, task, exhausted_loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_budget",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    exhausted_loop_mock.assert_not_awaited()
    assert task.status == TaskStatus.failed
    assert "maximum steps" in (task.failure_reason or "")


@pytest.mark.asyncio
async def test_execute_task_v3_detects_user_defined_errors_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # error_code_mapping workflows consume configured codes on failure; v3 must run the same
    # detection the step engine does before finalizing failed/terminated outcomes.
    detected = [
        SimpleNamespace(model_dump=lambda: {"error_code": "payment_failed"}, error_code="payment_failed", reasoning="")
    ]
    detect_mock = AsyncMock(return_value=detected)
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(status="terminated", reason="blocked by portal", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"payment_failed": "declined"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"payment_failed": "declined"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_awaited_once()
    assert errors_update.await_args.kwargs["errors"] == [{"error_code": "payment_failed"}]

    detect_mock.reset_mock()
    completed = LoopOutcome(status="completed", reason="done", billable_actions=[])
    await _run_execute_task_v3(
        monkeypatch,
        completed,
        task_block=block,
        error_code_mapping={"payment_failed": "declined"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_v3_actually_delivers_the_codes_to_the_loop_and_the_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wiring test. Without it the whole feature can be deleted -- the mapping pass-through, the engine
    # kwarg, or the goal block -- and every other test here still passes, because they call
    # make_finish_tool directly or inject a hand-built LoopOutcome. This is the only one that fails if
    # the codes never reach the model.
    mapping = {"COVERAGE_NOT_ACTIVE": "The member has no active plan today."}
    monkeypatch.setattr(
        "skyvern.forge.agent.app.AGENT_FUNCTION.resolve_task_v3_error_code_choice",
        AsyncMock(return_value=True),
    )
    outcome = LoopOutcome(status="completed", reason="ok", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping=mapping)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping=mapping,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["error_code_mapping"] == mapping

    goal = loop_mock.await_args.kwargs["goal"]
    assert "COVERAGE_NOT_ACTIVE" in goal
    assert "The member has no active plan today." in goal
    # The rule is pinned as content, not left to survive a prompt edit by luck. It is drawn on OUR
    # side of the line: our own failures may not wear a code, while a site problem the customer
    # defined a code for still can.
    assert "failure of YOU or the browser" in goal
    assert "problem with the SITE may take a code" in goal
    # The example has to name OUR disorientation, not the page's state: "losing the page" sat
    # directly opposite a customer code for a tab that could not render because of a portal-side
    # failure -- the exact code the rule above must leave reachable.
    assert "losing track of which page you are on" in goal
    assert "losing the page" not in goal


@pytest.mark.asyncio
async def test_execute_task_v3_flag_off_keeps_the_codes_out_of_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Off-state must be byte-identical to before this feature existed: no mapping reaches the loop,
    # nothing is added to the goal, and error_codes_offered stays False so every downstream branch
    # falls through to the detector exactly as today.
    mapping = {"COVERAGE_NOT_ACTIVE": "The member has no active plan today."}
    monkeypatch.setattr(
        "skyvern.forge.agent.app.AGENT_FUNCTION.resolve_task_v3_error_code_choice",
        AsyncMock(return_value=False),
    )
    outcome = LoopOutcome(status="completed", reason="ok", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping=mapping)
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping=mapping,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["error_code_mapping"] is None
    assert "COVERAGE_NOT_ACTIVE" not in loop_mock.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_execute_task_v3_persists_a_chosen_code_on_a_completed_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A named code is an answer about WHAT HAPPENED, so it survives whatever status the run reached.
    # Discarding it on `completed` would throw away the business-outcome verdict this change exists
    # to capture -- and the prompt actively tells the model to choose its status on the run's merits,
    # so it steers exactly that class of run here.
    detect_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="completed",
        reason="the member has no active plan today",
        billable_actions=[],
        error_code="COVERAGE_NOT_ACTIVE",
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_not_awaited()
    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert persisted["error_code"] == "COVERAGE_NOT_ACTIVE"
    # failure_reason is None on a clean completion, so the model's own finish reason is the account.
    assert persisted["reasoning"] == "the member has no active plan today"


@pytest.mark.asyncio
async def test_execute_task_v3_prefers_the_models_own_code_over_the_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SKY-15586. Once the model is shown the codes it finishes deliberately, so ITS verdict is the
    # answer -- re-running the detector would put a guess back on top of a choice.
    detect_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="terminated",
        reason="no active plan",
        billable_actions=[],
        error_code="COVERAGE_NOT_ACTIVE",
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "no plan"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "no plan"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_not_awaited()
    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert persisted["error_code"] == "COVERAGE_NOT_ACTIVE"
    assert persisted["confidence_float"] == 1.0


@pytest.mark.asyncio
async def test_execute_task_v3_terminal_block_that_names_no_code_gets_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model that was offered the codes and named none must end with NO business code at all. This
    # is the case the detector cannot get right on its own: it sees a page, not the question, so on a
    # block whose terminal reason none of the codes describes it can still match one from the page.
    detect_mock = AsyncMock(
        return_value=[SimpleNamespace(model_dump=lambda: {"error_code": "COVERAGE_NOT_ACTIVE"}, reasoning="")]
    )
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="terminated",
        reason="the control this block needs was never reachable",
        billable_actions=[],
        error_code=None,
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Log out", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_not_awaited()
    errors_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_v3_declining_a_code_holds_on_the_failed_path_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate is error_codes_offered, NOT status. Honoring the choice only on terminated would leave
    # the failed path running the detector over `failure_reason` -- which on a deliberate finish is
    # the model's own free text, now written with the code descriptions in front of it. The
    # detector's last resort substring-matches a description with no negation handling, so a model
    # explaining "this was NOT <description>" would persist that code at confidence 1.0: the very
    # defect this PR exists to fix.
    detect_mock = AsyncMock(
        return_value=[SimpleNamespace(model_dump=lambda: {"error_code": "COVERAGE_NOT_ACTIVE"}, reasoning="")]
    )
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="failed",
        reason="the coverage tab was present, so this was not a portal-side failure",
        billable_actions=[],
        error_code=None,
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_not_awaited()
    errors_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_v3_persists_a_model_chosen_code_on_a_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A code is about what happened, not about which status was reached. If only terminated could
    # carry one, a mapped task ending in a model-declared failure would end with no code at all --
    # the detector is skipped whenever codes were offered.
    detect_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="failed",
        reason="no active plan on file",
        billable_actions=[],
        error_code="COVERAGE_NOT_ACTIVE",
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_not_awaited()
    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert persisted["error_code"] == "COVERAGE_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_execute_task_v3_vetoed_completion_still_reaches_the_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A completion the gate vetoes is demoted to failed, but the MODEL said "completed" -- it was
    # never asked which business outcome explains a failure, so its silence is not a deliberate
    # decline and must not suppress the detector. Gating on task_status alone would swallow it.
    detected = [SimpleNamespace(model_dump=lambda: {"error_code": "COVERAGE_NOT_ACTIVE"}, reasoning="")]
    detect_mock = AsyncMock(return_value=detected)
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=[],
        error_code=None,
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
        completion_gate_vetoes=True,
    )
    detect_mock.assert_awaited_once()
    assert errors_update.await_args.kwargs["errors"] == [{"error_code": "COVERAGE_NOT_ACTIVE"}]


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_a_registered_secret_from_the_chosen_codes_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # task.errors is aggregated into the workflow run's top-level errors[] and goes out over the
    # customer's webhook, so this string LEAVES the system. The sibling decision row built from the
    # same outcome.reason is already scrubbed; a code's reasoning that skipped the pass would publish
    # a secret the row next to it redacts.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", AsyncMock(return_value=[]))
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="terminated",
        reason="the portal rejected the keysk4829137765I was given",
        billable_actions=[],
        error_code="COVERAGE_NOT_ACTIVE",
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert "sk4829137765" not in persisted["reasoning"]
    assert REDACTED_SECRET_PLACEHOLDER in persisted["reasoning"]


@pytest.mark.asyncio
async def test_execute_task_v3_caps_the_chosen_codes_reasoning_after_redacting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two sibling reasoning persists (action row, decision row) both cap at
    # _TASKV3_REASONING_MAX_CHARS; the code's reasoning is the only one that reaches the customer's
    # webhook and was the only one uncapped.
    # The secret straddles the cap boundary, so this also pins the ORDER: redact, then truncate.
    # Truncating first would cut the secret in half, leaving a fragment the redactor can no longer
    # match, and publish it.
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", AsyncMock(return_value=[]))
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    straddling = "a" * (agent_mod._TASKV3_REASONING_MAX_CHARS - 5) + "sk4829137765" + "b" * 4000
    outcome = LoopOutcome(
        status="terminated",
        reason=straddling,
        billable_actions=[],
        error_code="COVERAGE_NOT_ACTIVE",
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert len(persisted["reasoning"]) == agent_mod._TASKV3_REASONING_MAX_CHARS
    assert "sk48" not in persisted["reasoning"]


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_a_registered_secret_from_the_webhook_bound_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # failure_reason is a first-class field on the task webhook payload, so this string LEAVES the
    # system. run_secrets is hoisted for "every model-authored string this method persists" and
    # failure_reason is one of them, so a path that skipped it published a secret the sibling redacts.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", AsyncMock(return_value=[]))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock())
    outcome = LoopOutcome(
        status="terminated",
        reason="the portal rejected the key sk4829137765 I was given",
        billable_actions=[],
    )
    _, out_task, loop_mock, _ = await _run_execute_task_v3(monkeypatch, outcome)

    persisted = loop_mock.update_task_kwargs["failure_reason"]
    assert "sk4829137765" not in persisted
    assert REDACTED_SECRET_PLACEHOLDER in persisted
    # Asserted at the egress boundary rather than only at the local: this is the field the webhook
    # payload is built from, which is what makes the leak customer-visible.
    assert "sk4829137765" not in (out_task.to_task_response().failure_reason or "")


@pytest.mark.asyncio
async def test_execute_task_v3_redacts_failure_reason_before_any_consumer_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins WHERE the redaction runs, not just that it runs. The redactor matches whole registered
    # values, so it has to happen at the point the string is born -- before the classifier and the
    # error detector read it. Moving it down to just before the terminal update would still pass the
    # test above while handing raw secrets to a detector whose own output also leaves over the webhook.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    detector = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detector)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock())
    outcome = LoopOutcome(
        status="terminated",
        reason="the portal rejected the key sk4829137765 I was given",
        billable_actions=[],
    )
    await _run_execute_task_v3(monkeypatch, outcome, error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})

    seen_by_detector = detector.await_args.kwargs["failure_reason"]
    assert "sk4829137765" not in seen_by_detector
    assert REDACTED_SECRET_PLACEHOLDER in seen_by_detector


@pytest.mark.asyncio
async def test_execute_task_v3_scrubs_a_registered_secret_from_detector_written_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fourth reasoning persist in this method. The detector also reads the page, so redacting
    # its input is not enough -- a secret typed into the form can come back in its reasoning, and
    # task.errors leaves over the customer's webhook exactly like the chosen-code branch does.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.detect_user_defined_errors_for_task",
        AsyncMock(
            return_value=[
                UserDefinedError(
                    error_code="COVERAGE_NOT_ACTIVE",
                    reasoning="the page showed sk4829137765 after submit",
                    confidence_float=1.0,
                )
            ]
        ),
    )
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(status="terminated", reason="could not continue", billable_actions=[])
    await _run_execute_task_v3(monkeypatch, outcome, error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})

    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert "sk4829137765" not in persisted["reasoning"]
    assert REDACTED_SECRET_PLACEHOLDER in persisted["reasoning"]


@pytest.mark.asyncio
async def test_execute_task_v3_caps_detector_written_reasoning_after_redacting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # task.errors APPENDS rather than replaces (tasks.py `task.errors = (task.errors or []) + errors`),
    # so an uncapped detector reasoning accumulates in a webhook-shipped column.
    # The secret straddles the cap boundary, so this also pins the ORDER: redact, then truncate.
    # Truncating first would cut the secret in half and publish a fragment the redactor cannot match.
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    straddling = "a" * (agent_mod._TASKV3_REASONING_MAX_CHARS - 5) + "sk4829137765" + "b" * 4000
    monkeypatch.setattr(
        "skyvern.forge.agent.detect_user_defined_errors_for_task",
        AsyncMock(
            return_value=[
                UserDefinedError(error_code="COVERAGE_NOT_ACTIVE", reasoning=straddling, confidence_float=1.0)
            ]
        ),
    )
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(status="terminated", reason="could not continue", billable_actions=[])
    await _run_execute_task_v3(monkeypatch, outcome, error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})

    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert len(persisted["reasoning"]) == agent_mod._TASKV3_REASONING_MAX_CHARS
    assert "sk48" not in persisted["reasoning"]


@pytest.mark.asyncio
async def test_execute_task_v3_caps_detector_written_reasoning_with_no_secrets_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap is not gated on the Mask Secrets opt-in: a long reasoning accumulates in an
    # append-only, webhook-shipped column whether or not the run registered any secret.
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
        lambda *_a, **_k: set(),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.detect_user_defined_errors_for_task",
        AsyncMock(
            return_value=[
                UserDefinedError(
                    error_code="COVERAGE_NOT_ACTIVE",
                    reasoning="z" * (agent_mod._TASKV3_REASONING_MAX_CHARS + 2500),
                    confidence_float=1.0,
                )
            ]
        ),
    )
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(status="terminated", reason="could not continue", billable_actions=[])
    await _run_execute_task_v3(monkeypatch, outcome, error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})

    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert len(persisted["reasoning"]) == agent_mod._TASKV3_REASONING_MAX_CHARS


@pytest.mark.asyncio
async def test_execute_task_v3_leaves_failure_reason_byte_identical_when_redaction_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Secret values are available, but the gate itself is off -- the scrub must not run at all.
    # Redaction is opt-in per workflow, so a run that never enabled it must get the model's text back
    # unchanged rather than a quiet rewrite of a field customers read and alert on.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
        lambda *_a, **_k: set(),
    )
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", AsyncMock(return_value=[]))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", AsyncMock())
    raw = "the portal rejected the key sk4829137765 I was given"
    outcome = LoopOutcome(status="terminated", reason=raw, billable_actions=[])
    _, _, loop_mock, _ = await _run_execute_task_v3(monkeypatch, outcome)

    assert loop_mock.update_task_kwargs["failure_reason"] == raw


@pytest.mark.asyncio
async def test_execute_task_v3_records_the_models_own_account_not_the_gate_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The one path where failure_reason and the model's finish reason disagree: the gate demotes a
    # completion to failed and writes its own text, which explains the DEMOTION, not why the model
    # named this code. Reading failure_reason first would file the business code under "the gate
    # rejected it" and lose the only account of the code.
    detect_mock = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="completed",
        reason="the member has no active plan today",
        billable_actions=[],
        error_code="COVERAGE_NOT_ACTIVE",
        error_codes_offered=True,
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
        completion_gate_vetoes=True,
    )
    (persisted,) = errors_update.await_args.kwargs["errors"]
    assert persisted["error_code"] == "COVERAGE_NOT_ACTIVE"
    assert persisted["reasoning"] == "the member has no active plan today"
    assert "completion gate" not in persisted["reasoning"]


@pytest.mark.asyncio
async def test_execute_task_v3_leaves_unasked_failures_to_the_detector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scope guard: every v1 caller and every v3 task without a mapping has error_codes_offered
    # False, and those runs must keep exactly today's behavior -- the detector still runs.
    detected = [SimpleNamespace(model_dump=lambda: {"error_code": "COVERAGE_NOT_ACTIVE"}, reasoning="")]
    detect_mock = AsyncMock(return_value=detected)
    monkeypatch.setattr("skyvern.forge.agent.detect_user_defined_errors_for_task", detect_mock)
    errors_update = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.tasks.update_task", errors_update)
    outcome = LoopOutcome(
        status="failed",
        reason="the page never loaded",
        billable_actions=[],
        error_code=None,
        error_codes_offered=False,  # never asked
    )
    block = _make_block(NavigationBlock, navigation_goal="Go", error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"})
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        error_code_mapping={"COVERAGE_NOT_ACTIVE": "x"},
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    detect_mock.assert_awaited_once()
    assert errors_update.await_args.kwargs["errors"] == [{"error_code": "COVERAGE_NOT_ACTIVE"}]


@pytest.mark.asyncio
async def test_execute_task_v3_router_selected_data_only_validation_goes_page_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The validation evidence router can select data-only mode even when the block does not set
    # without_page_information; v3 must honor it like v1 does (and stay page-aware on router error).
    router_result = SimpleNamespace(effective_without_page_information=True)
    monkeypatch.setattr("skyvern.forge.agent.resolve_validation_evidence_route", AsyncMock(return_value=router_result))
    outcome = LoopOutcome(status="completed", reason="ok", billable_actions=[])
    block = _make_block(ValidationBlock, complete_criterion="The totals match")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.validation,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert "page-free assessment" in loop_mock.await_args.kwargs["goal"]

    monkeypatch.setattr(
        "skyvern.forge.agent.resolve_validation_evidence_route", AsyncMock(side_effect=RuntimeError("router down"))
    )
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        task_type=TaskType.validation,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    goal = loop_mock.await_args.kwargs["goal"]
    assert "page-free assessment" not in goal
    assert "read the page's visible text (get_html with format text) before concluding" in goal


@pytest.mark.asyncio
async def test_execute_task_v3_hands_the_loop_the_live_verification_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # The finish gate the loop receives must be the same VerificationState the auth tools mutate;
    # a fresh or detached state would let a refused source false-complete despite the blocker.
    from skyvern.forge.taskv3.auth_tools import VerificationFailure

    seen_states: list[Any] = []

    def capturing_build(
        task: Any, page_provider: Any = None, state: Any = None, allowed_credential_parameter_keys: Any = None
    ) -> tuple[list[Any], str]:
        seen_states.append(state)
        return [], ""

    monkeypatch.setattr("skyvern.forge.taskv3.auth_tools.build_auth_tools", capturing_build)
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    blocker = loop_mock.await_args.kwargs["verification_blocker"]
    assert blocker is not None
    assert len(seen_states) == 1 and blocker.__self__ is seen_states[0]
    assert await blocker("completed") is None
    seen_states[0].arm(VerificationFailure.NO_CODE_TWICE, "get_verification_code")
    assert await blocker("completed") is not None


@pytest.mark.asyncio
async def test_execute_task_v3_pins_credential_before_building_auth_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tool-offer gate consults credential candidates; built before the pin, a multi-credential
    # context looks ambiguous and get_verification_code is never offered. The pin must be active
    # when build_auth_tools runs.
    seen_keys: list[str | None] = []
    seen_providers: list[Any] = []

    def capturing_build(
        task: Any, page_provider: Any = None, state: Any = None, allowed_credential_parameter_keys: Any = None
    ) -> tuple[list[Any], str]:
        ctx = skyvern_context.current()
        seen_keys.append(ctx.active_credential_parameter_key if ctx else None)
        seen_providers.append(page_provider)
        return [], ""

    monkeypatch.setattr("skyvern.forge.taskv3.auth_tools.build_auth_tools", capturing_build)
    monkeypatch.setattr("skyvern.forge.agent.build_auth_tools", capturing_build, raising=False)
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(LoginBlock, parameters=[_make_credential_parameter("MyCreds")])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert seen_keys == ["MyCreds"]
    assert len(seen_providers) == 1 and callable(seen_providers[0])


@pytest.mark.asyncio
async def test_execute_task_v3_withholds_the_page_provider_from_auth_tools_when_page_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A page-free assessment never touches the live DOM, so the auth tools must not be handed a page to
    # navigate; a page-aware run must be, or the sign-in-link tool is silently never offered.
    seen_providers: list[Any] = []

    def capturing_build(
        task: Any, page_provider: Any = None, state: Any = None, allowed_credential_parameter_keys: Any = None
    ) -> tuple[list[Any], str]:
        seen_providers.append(page_provider)
        return [], ""

    monkeypatch.setattr("skyvern.forge.taskv3.auth_tools.build_auth_tools", capturing_build)
    monkeypatch.setattr("skyvern.forge.agent.build_auth_tools", capturing_build, raising=False)
    outcome = LoopOutcome(status="completed", reason="ok", billable_actions=[])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=_make_block(ValidationBlock, complete_criterion="The data is consistent"),
        validation_without_page_information=True,
        task_type=TaskType.validation,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert seen_providers[0] is None
    assert callable(seen_providers[1])


@pytest.mark.asyncio
async def test_execute_task_v3_failed_round_persists_failed_action_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dispatched round that errored still consumed budget: its row persists with status=failed
    # and its round index, so later blocks count it against the workflow-run ceiling.
    outcome = LoopOutcome(status="budget_exhausted", reason="cap", billable_actions=[])
    rounds = [[RoundAction("click", {"selector": "#x"}, False, billable=True)]]
    await _run_execute_task_v3(
        monkeypatch, outcome, action_rounds=rounds, data_extraction_goal=None, extracted_information_schema=None
    )
    create_action = agent_module.app.DATABASE.workflow_params.create_action
    action = create_action.await_args.kwargs["action"]
    assert action.status == ActionStatus.failed
    assert action.step_order == 0
    clicked = create_action.await_args_list[0].kwargs["action"]
    assert clicked.intention == "Tried to click an element"


@pytest.mark.asyncio
async def test_execute_task_v3_page_free_validation_threads_page_free_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="ok", billable_actions=[])
    block = _make_block(ValidationBlock, complete_criterion="The data is consistent")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        validation_without_page_information=True,
        task_type=TaskType.validation,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["page_free"] is True

    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert loop_mock.await_args.kwargs["page_free"] is False


@pytest.mark.asyncio
async def test_execute_task_v3_recordable_round_persists_without_budget_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # navigate/scroll/wait persist as action rows with screenshots (artifact parity) but never
    # consume a workflow-run budget unit: their rows share the step_order of the billable round
    # nearest them, ahead OR behind, so they open no (task_id, step_order) pair of their own.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    rounds = [
        [RoundAction("navigate", {"url": "https://a.test"}, True)],
        [RoundAction("click", {"selector": "#go"}, True, billable=True)],
        [RoundAction("scroll", {"amount": 500}, True)],
    ]
    await _run_execute_task_v3(
        monkeypatch, outcome, action_rounds=rounds, data_extraction_goal=None, extracted_information_schema=None
    )
    create_action = agent_module.app.DATABASE.workflow_params.create_action
    # Slice off the trailing terminal decision row — its own stamping is covered elsewhere.
    action_rows = create_action.await_args_list[:-1]
    stamped = [(c.kwargs["action"].action_type, c.kwargs["action"].step_order) for c in action_rows]
    assert stamped == [
        (ActionType.GOTO_URL, 0),
        (ActionType.CLICK, 0),
        # The trailing scroll rides the index the click already spent: a fresh one would be a second
        # distinct pair for the budget to count, charging the run a step nothing billable claimed.
        (ActionType.SCROLL, 0),
    ]


@pytest.mark.asyncio
async def test_execute_task_v3_failed_run_carries_failure_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fail_task records a code-level failure classification; v3's direct finalization must too.
    outcome = LoopOutcome(status="budget_exhausted", reason="Reached the maximum steps (2)", billable_actions=[])
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert task.status == TaskStatus.failed
    update_kwargs = loop_mock.update_task_kwargs
    assert update_kwargs.get("failure_category")


@pytest.mark.asyncio
async def test_execute_task_v3_budget_exit_carries_typed_category_and_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cap-tripped run that never finished: the human sentence (not the raw cap literal) is the
    # failure_reason, the raw literal rides the BUDGET_EXHAUSTED category's reasoning, and a partial
    # extraction the model had staged is not discarded with the failure.
    outcome = LoopOutcome(
        status="budget_exhausted",
        reason="The run reached its turn budget before the model finished; the recorded output may be partial.",
        cap_trip="max_turns (40) reached",
        billable_actions=["click"],
        extracted_output={"partial": True},
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert task.status == TaskStatus.failed
    update_kwargs = loop_mock.update_task_kwargs
    assert "max_turns (" not in (update_kwargs.get("failure_reason") or "")
    assert update_kwargs.get("failure_category") == [
        {"category": "BUDGET_EXHAUSTED", "confidence_float": 1.0, "reasoning": "max_turns (40) reached"}
    ]
    assert update_kwargs.get("extracted_information") == {"partial": True}


@pytest.mark.asyncio
async def test_execute_task_v3_cap_tripped_finish_keeps_output_and_decision_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A finish delivered on the granted final turn: the model's verdict and reason stand, the
    # extracted_output persists despite the non-completed status, the category classifies from the
    # model's reason (a captcha block is anti-bot, not budget — the cap merely coincided), and the
    # terminal decision row is written like any other finish.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(
        status="failed",
        reason="blocked by a captcha",
        cap_trip="Reached the maximum steps (25)",
        billable_actions=[],
        extracted_output={"rows": [1]},
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(monkeypatch, outcome, action_rounds=None)
    assert task.status == TaskStatus.failed
    update_kwargs = loop_mock.update_task_kwargs
    assert update_kwargs.get("failure_reason") == "blocked by a captcha"
    category = update_kwargs.get("failure_category")
    assert category and category[0]["category"] == "ANTI_BOT_DETECTION", category
    assert update_kwargs.get("extracted_information") == {"rows": [1]}
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args.kwargs["action"]
    assert persisted.action_type == ActionType.TERMINATE
    assert persisted.reasoning == "blocked by a captcha"


@pytest.mark.asyncio
async def test_execute_task_v3_cap_tripped_finish_with_unclassifiable_reason_falls_back_to_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A granted-turn failed verdict whose reason carries no classifiable signal is usually the
    # model narrating the truncation: the typed cap fact beats an UNKNOWN keyword fallback.
    outcome = LoopOutcome(
        status="failed",
        reason="could not finish filling in the remaining sections",
        cap_trip="max_turns (40) reached",
        billable_actions=["click"],
        extracted_output={"partial": True},
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(monkeypatch, outcome, action_rounds=None)
    assert task.status == TaskStatus.failed
    category = loop_mock.update_task_kwargs.get("failure_category")
    assert category == [
        {"category": "BUDGET_EXHAUSTED", "confidence_float": 1.0, "reasoning": "max_turns (40) reached"}
    ]


@pytest.mark.asyncio
async def test_execute_task_v3_cap_tripped_guard_termination_is_not_budget_categorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A repeat-guard termination on the granted turn terminated for the guard's reason (its typed
    # prefix rides failure_reason); stamping BUDGET_EXHAUSTED over it would mix the label axes.
    outcome = LoopOutcome(
        status="terminated",
        reason="action_loop: repeated identical click on the same target",
        cap_trip="max_turns (40) reached",
        billable_actions=["click"],
        extracted_output=None,
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert task.status == TaskStatus.terminated
    assert loop_mock.update_task_kwargs.get("failure_category") is None


@pytest.mark.asyncio
async def test_execute_task_v3_cap_tripped_missing_extraction_keeps_its_own_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A completion demoted for missing extraction failed for THAT reason even when it landed on a
    # granted final turn: failure_category must agree with failure_reason, not read BUDGET_EXHAUSTED.
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        cap_trip="max_turns (40) reached",
        billable_actions=["type"],
        extracted_output=None,
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal="Extract the rows", extracted_information_schema=None
    )
    assert task.status == TaskStatus.failed
    category = loop_mock.update_task_kwargs.get("failure_category")
    assert category, category
    assert category[0]["category"] != "BUDGET_EXHAUSTED", category


@pytest.mark.asyncio
async def test_execute_task_v3_cap_tripped_loop_error_keeps_provider_category_and_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A provider failure on the granted call is an infra failure that merely happened after the
    # cap tripped: the category must reflect the error (not BUDGET_EXHAUSTED), while the staged
    # extraction the loop salvaged still persists through the relaxed gate.
    outcome = LoopOutcome(
        status="loop_error",
        reason="llm_call_failed: RuntimeError: provider unavailable",
        cap_trip="max_tool_calls (100) reached",
        billable_actions=[],
        extracted_output={"rows": [8]},
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert task.status == TaskStatus.failed
    category = loop_mock.update_task_kwargs.get("failure_category")
    assert category, category
    assert category[0]["category"] != "BUDGET_EXHAUSTED", category
    assert loop_mock.update_task_kwargs.get("extracted_information") == {"rows": [8]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("budget_exhausted", "The run reached its turn budget before the model finished."),
        ("loop_error", "llm_call_failed: RuntimeError: provider unavailable"),
    ],
)
async def test_execute_task_v3_verdictless_death_persists_a_failed_decision_row(
    monkeypatch: pytest.MonkeyPatch, status: str, reason: str
) -> None:
    # A death with no agent verdict is exactly the run a click-free block has no step details for:
    # the terminal decision row is synthesized as a FAILED terminate carrying the outcome reason.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status=status, reason=reason, billable_actions=["click"])
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert task.status == TaskStatus.failed
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args.kwargs["action"]
    assert persisted.action_type == ActionType.TERMINATE
    assert persisted.status == ActionStatus.failed
    assert persisted.reasoning == reason


@pytest.mark.asyncio
async def test_execute_task_v3_verdictless_death_with_salvaged_output_still_fails_the_decision_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The decision row records whether the agent gave a verdict, not whether data survived: a
    # cap-tripped death still persists FAILED even though AC-2 carries the staged extraction
    # through. Coupling the row's status to the salvage would hide these runs from the
    # failed-decision-row taxonomy the docs point at.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(
        status="budget_exhausted",
        reason="The run reached its turn budget before the model finished.",
        cap_trip="max_turns (40) reached",
        billable_actions=["click"],
        extracted_output={"rows": [3]},
    )
    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert task.status == TaskStatus.failed
    assert loop_mock.update_task_kwargs.get("extracted_information") == {"rows": [3]}
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args.kwargs["action"]
    assert persisted.action_type == ActionType.TERMINATE
    assert persisted.status == ActionStatus.failed


@pytest.mark.asyncio
async def test_execute_task_v3_canceled_death_persists_no_decision_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cancellation is the user's decision, not a run verdict — no synthesized row.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="canceled", reason="run canceled", billable_actions=[])
    await _run_execute_task_v3(monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None)
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_args is None


def test_task_validate_update_allows_partial_extraction_on_failed_and_terminated() -> None:
    # The budget-cap final turn persists PARTIAL extraction with a failed/terminated verdict. The
    # execute harness stubs update_task, so the real contract is pinned here: failed/terminated
    # accept data, pre-run statuses keep rejecting it.
    now = datetime.now(UTC)
    organization = make_organization(now)
    make_task(now, organization, status=TaskStatus.running).validate_update(
        TaskStatus.failed, {"partial": 1}, "budget capped"
    )
    make_task(now, organization, status=TaskStatus.running).validate_update(
        TaskStatus.terminated, {"partial": 1}, "budget capped"
    )
    with pytest.raises(ValueError):
        make_task(now, organization, status=TaskStatus.created).validate_update(TaskStatus.running, {"partial": 1})


@pytest.mark.asyncio
async def test_execute_task_v3_settle_completion_fenced_to_block_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both populations get a fingerprint sampler: the failure-evidence gate needs one to run at all
    # (SKY-14598). Only the completed-side settle deferral stays fenced to block tasks, and it is
    # fenced by its own deferral budget rather than by starving the shared sampler.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Open the panel")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, task_block=block, data_extraction_goal=None, extracted_information_schema=None
    )
    assert loop_mock.await_args.kwargs["page_fingerprint"] is not None
    assert loop_mock.await_args.kwargs["page_probe"] is not None  # batch-poisoning probe reaches the engine
    assert loop_mock.await_args.kwargs["max_settle_deferrals"] > 0

    _step, _task, bare_loop_mock, _post = await _run_execute_task_v3(
        monkeypatch, outcome, data_extraction_goal=None, extracted_information_schema=None
    )
    assert bare_loop_mock.await_args.kwargs["page_fingerprint"] is not None
    assert bare_loop_mock.await_args.kwargs["page_probe"] is not None
    assert bare_loop_mock.await_args.kwargs["max_settle_deferrals"] == 0


@pytest.mark.asyncio
async def test_execute_task_v3_bare_task_fingerprint_samples_the_pinned_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare task pins one page for the run, so its fingerprint must sample THAT page. Going through
    # browser_state.get_working_page() would return the newest tab after any popup — sampling a page
    # the model never acted on — and would repoint the working page as a side effect, which is a
    # behaviour change to the live bare-task arm rather than the scoped one this gate intends.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    pinned = MagicMock()
    pinned.is_closed = MagicMock(return_value=False)
    pinned.evaluate = AsyncMock(return_value="pinned-hash:100:10")
    popup = MagicMock()
    popup.is_closed = MagicMock(return_value=False)
    popup.evaluate = AsyncMock(return_value="popup-hash:1:1")

    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        must_get_working_page_side_effect=[pinned],
        get_working_page_side_effect=[popup, popup, popup],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    fingerprint = loop_mock.await_args.kwargs["page_fingerprint"]
    before = loop_mock.browser_state.get_working_page.await_count
    assert await fingerprint() == "pinned-hash:100:10"
    # The sampler probed the pinned page and never the popup, and did not consult (or repoint) the
    # browser's working page to do it. Counting the delta rather than asserting never-awaited: the
    # post-loop completion-veto gate legitimately calls get_working_page once, before this point.
    popup.evaluate.assert_not_awaited()
    assert loop_mock.browser_state.get_working_page.await_count == before

    # A closed pinned page yields None rather than silently falling back to another tab.
    pinned.is_closed = MagicMock(return_value=True)
    assert await fingerprint() is None


@pytest.mark.asyncio
async def test_execute_task_v3_page_fingerprint_peeks_without_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sampler peeks with the NON-recovering accessor: a lost page yields None (accept the
    # verdict) rather than triggering recovery navigation at finish time. A sampling error
    # propagates — the finish gate fails closed on it instead of reading it as settled.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=[])
    block = _make_block(NavigationBlock, navigation_goal="Open the panel")

    page = MagicMock()
    page.evaluate = AsyncMock(side_effect=["hash-a:100:10", "hash-b:100:10"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        get_working_page_side_effect=[page, page, page, page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    fingerprint = loop_mock.await_args.kwargs["page_fingerprint"]
    assert await fingerprint() == "hash-a:100:10"
    assert await fingerprint() == "hash-b:100:10"
    loop_mock.browser_state.get_working_page.assert_awaited()

    page.evaluate = AsyncMock(side_effect=RuntimeError("execution context was destroyed"))
    loop_mock.browser_state.get_working_page = AsyncMock(return_value=page)
    with pytest.raises(RuntimeError):
        await fingerprint()

    lost_page_peek = AsyncMock(return_value=None)
    loop_mock.browser_state.get_working_page = lost_page_peek
    assert await fingerprint() is None
    lost_page_peek.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_task_v3_persists_typed_actions_that_hydrate_as_their_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A row carrying only a description fails typed-subclass validation on every read and falls back
    # to base Action with a warning; the persisted action must carry the tool call's typed fields so
    # hydrate_action returns the subclass its action_type names (SKY-14494).
    from skyvern.forge import agent as agent_mod

    rounds = [
        [
            RoundAction("click", {"selector": "#go"}, True, billable=True),
            RoundAction("hover", {"selector": "#menu"}, True, billable=True),
            RoundAction("type", {"selector": "#q", "text": "hello"}, True, billable=True),
            RoundAction("select_option", {"selector": "#plan", "label": "Pro"}, True, billable=True),
            RoundAction("select_combobox", {"selector": "#city", "value": "Lisbon"}, True, billable=True),
            RoundAction("press_key", {"key": "Enter"}, True, billable=True),
            RoundAction(
                "file_upload", {"selector": "#cv", "file": "https://files.example/cv.pdf"}, True, billable=True
            ),
            RoundAction("solve_captcha", {}, True),
        ]
    ]
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        workflow_run_id="wr_v3test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    all_persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list]
    # The terminal decision row is appended after the 8 tool-call rows this test is about.
    persisted = all_persisted[:-1]
    assert [a.description for a in persisted[:2]] == ["task_v3 click #go", "task_v3 hover #menu"]
    click, hover, typed, selected, combobox, keypress, upload, captcha = (
        hydrate_action(make_action_row(action_type=a.action_type, element_id=a.element_id, action_json=a.model_dump()))
        for a in persisted
    )
    assert isinstance(click, ClickAction) and click.element_id == "#go"
    assert isinstance(hover, HoverAction) and hover.element_id == "#menu"
    assert isinstance(typed, InputTextAction) and (typed.element_id, typed.text) == ("#q", "hello")
    assert isinstance(selected, SelectOptionAction) and (selected.element_id, selected.option.label) == ("#plan", "Pro")
    assert isinstance(combobox, SelectOptionAction) and (combobox.element_id, combobox.option.value) == (
        "#city",
        "Lisbon",
    )
    assert isinstance(keypress, KeypressAction) and keypress.keys == ["Enter"]
    assert isinstance(upload, UploadFileAction) and (upload.element_id, upload.file_url) == (
        "#cv",
        "https://files.example/cv.pdf",
    )
    assert isinstance(captcha, SolveCaptchaAction)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed_code", ["482913", 482913, ["482913"]])
async def test_execute_task_v3_redacts_registered_secrets_from_persisted_action_fields(
    monkeypatch: pytest.MonkeyPatch, typed_code: str | int | list[str]
) -> None:
    # A verification code the model typed is a registered secret; persisting the typed field must
    # scrub it under the same redaction gate the v3 path applies to extracted output - also when the
    # model emits the code as a JSON number, since nothing coerces tool args to the declared schema.
    from skyvern.forge import agent as agent_mod

    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run", lambda *_a, **_k: {"482913"}
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type"])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[RoundAction("type", {"selector": "#otp", "text": typed_code}, True, billable=True)]],
        workflow_run_id="wr_v3test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    # The type action's own row, not the terminal decision row appended after it.
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    assert isinstance(persisted, InputTextAction)
    assert persisted.element_id == "#otp"
    assert "482913" not in persisted.model_dump_json()


# ---------------------------------------------------------------------------
# Cross-block handoff (TASK_V3_BLOCK_HANDOFF): predecessor context rendered
# into the goal when the flag is on, and this block's own outcome persisted
# for the next block's handoff on every terminal path.
# ---------------------------------------------------------------------------


def _make_predecessor_run_block(**overrides: Any) -> WorkflowRunBlock:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "workflow_run_block_id": "wrb_prev",
        "workflow_run_id": "wr_handoff",
        "organization_id": "org-123",
        "block_type": BlockType.TASK,
        "status": BlockStatus.failed,
        "label": "checkout",
        "finish_reason": "captcha never cleared",
        "task_id": "task_prev",
        "created_at": now - timedelta(minutes=5),
        "modified_at": now - timedelta(minutes=5),
    }
    base.update(overrides)
    return WorkflowRunBlock(**base)


@pytest.mark.asyncio
async def test_execute_task_v3_handoff_flag_on_renders_predecessor_into_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.agent.settings.TASK_V3_BLOCK_HANDOFF", True)
    # The mocked rows include the current block's own (running) row, to prove it's excluded from
    # predecessor selection by task_id rather than accidentally winning as the "most recent" row.
    own_running_row = _make_predecessor_run_block(
        workflow_run_block_id="wrb_current",
        task_id="task-123",
        status=BlockStatus.running,
        label="shipping",
        finish_reason=None,
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks",
        AsyncMock(return_value=[_make_predecessor_run_block(), own_running_row]),
    )
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", AsyncMock())

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_handoff",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    goal = loop_mock.await_args.kwargs["goal"]
    assert "Workflow context" in goal
    assert "status: failed" in goal
    assert "captcha never cleared" in goal
    # The block-kind framing (mid-flow guidance) is still present, and precedes the handoff section.
    framing_marker = "one block of a larger workflow"
    assert framing_marker in goal
    assert goal.index(framing_marker) < goal.index("Workflow context")


@pytest.mark.asyncio
async def test_execute_task_v3_handoff_ignores_blocks_from_prior_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.agent.settings.TASK_V3_BLOCK_HANDOFF", True)
    monkeypatch.setattr("skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_attempt_number", lambda _run_id: 2)
    previous_attempt_row = _make_predecessor_run_block(
        workflow_run_block_id="wrb_attempt_1",
        attempt_number=1,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
        label="old_shipping",
        status=BlockStatus.failed,
        finish_reason="prior attempt failure",
    )
    current_attempt_row = _make_predecessor_run_block(
        workflow_run_block_id="wrb_attempt_2",
        attempt_number=2,
        task_id="task-123",
        created_at=datetime.now(UTC),
        label="shipping",
        status=BlockStatus.running,
        finish_reason=None,
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks",
        AsyncMock(return_value=[previous_attempt_row, current_attempt_row]),
    )
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", AsyncMock())

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        workflow_run_id="wr_handoff",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    goal = loop_mock.await_args.kwargs["goal"]
    assert "Workflow context" not in goal
    assert "prior attempt failure" not in goal


@pytest.mark.asyncio
async def test_execute_task_v3_handoff_flag_off_skips_lookup_and_goal_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])

    get_blocks_with_rows = AsyncMock(return_value=[_make_predecessor_run_block()])
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", get_blocks_with_rows)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", AsyncMock())
    _step, _task, loop_mock_with_rows, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=_make_block(TaskBlock, label="shipping"),
        workflow_run_id="wr_handoff",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    get_blocks_with_rows.assert_not_awaited()

    get_blocks_no_rows = AsyncMock(return_value=[])
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", get_blocks_no_rows)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", AsyncMock())
    _step2, _task2, loop_mock_no_rows, _post2 = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=_make_block(TaskBlock, label="shipping"),
        workflow_run_id="wr_handoff",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    get_blocks_no_rows.assert_not_awaited()

    assert loop_mock_with_rows.await_args.kwargs["goal"] == loop_mock_no_rows.await_args.kwargs["goal"]


def _make_own_block_row(task_id: str = "task-123", **overrides: Any) -> WorkflowRunBlock:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "workflow_run_block_id": "wrb_current",
        "workflow_run_id": "wr_handoff",
        "organization_id": "org-123",
        "block_type": BlockType.TASK,
        "task_id": task_id,
        "created_at": now,
        "modified_at": now,
    }
    base.update(overrides)
    return WorkflowRunBlock(**base)


@pytest.mark.asyncio
async def test_execute_task_v3_skips_persist_without_run_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default helper state: no workflow run context has been registered in this process for any
    # workflow_run_id, so has_workflow_run_context is False here without needing a monkeypatch.
    # Masking needs the run's registered secrets, so with no run context the handoff must not
    # persist at all — not even the own-block lookup should run.
    assert agent_module.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context("wr_handoff") is False

    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)

    outcome = LoopOutcome(status="completed", reason="done here", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")
    final_page = MagicMock()
    final_page.url = "https://example.test/confirmation"
    final_page.is_closed = MagicMock(return_value=False)

    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_row=_make_own_block_row(),
        workflow_run_id="wr_handoff",
        get_working_page_side_effect=[final_page, final_page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    loop_mock.get_own_block_mock.assert_not_awaited()
    update_block_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_v3_persists_finish_reason_and_final_url_with_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    workflow_run_context.mask_secrets_in_data = lambda v, **_k: v
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )

    outcome = LoopOutcome(status="completed", reason="done here", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")
    final_page = MagicMock()
    final_page.url = "https://example.test/confirmation"
    final_page.is_closed = MagicMock(return_value=False)

    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_row=_make_own_block_row(),
        workflow_run_id="wr_handoff",
        # One call from the completion gate, one from the handoff-persist fingerprint.
        get_working_page_side_effect=[final_page, final_page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    loop_mock.get_own_block_mock.assert_awaited_once_with(task_id="task-123", organization_id="org-123")
    update_block_mock.assert_awaited_once()
    assert update_block_mock.await_args.kwargs["workflow_run_block_id"] == "wrb_current"
    assert update_block_mock.await_args.kwargs["finish_reason"] == "done here"
    assert update_block_mock.await_args.kwargs["final_url"] == "https://example.test/confirmation"


@pytest.mark.asyncio
async def test_execute_task_v3_redacts_a_runtime_secret_from_a_completed_runs_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A completed run leaves failure_reason None, so the handoff falls to raw outcome.reason and is
    # sanitized only by mask_secrets_in_data -- which covers the static workflow secrets dict but
    # NOT the runtime-minted values (a resolved verification code) that run_secrets carries.
    # finish_reason is persisted and API-visible, so that value leaves the system.
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    # The masker only knows the static secrets dict, so it is a no-op on a runtime OTP.
    workflow_run_context.mask_secrets_in_data = lambda v, **_k: v
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )

    outcome = LoopOutcome(
        status="completed", reason="entered the code sk4829137765 and submitted", billable_actions=["click"]
    )
    block = _make_block(TaskBlock, label="shipping")
    final_page = MagicMock()
    final_page.url = "https://example.test/confirmation"
    final_page.is_closed = MagicMock(return_value=False)

    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_row=_make_own_block_row(),
        workflow_run_id="wr_handoff",
        get_working_page_side_effect=[final_page, final_page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    persisted = update_block_mock.await_args.kwargs["finish_reason"]
    assert "sk4829137765" not in persisted
    assert REDACTED_SECRET_PLACEHOLDER in persisted


@pytest.mark.asyncio
async def test_execute_task_v3_redacts_runtime_secrets_from_extraction_with_the_gate_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The extraction scrub did its own lookup gated on the Mask Secrets opt-in, with no fallback to
    # runtime_secret_values_for_artifacts(). That toggle is on for 0.6% of workflows, so an
    # engine-minted value echoed into extracted_information shipped raw almost always -- while the
    # same value in failure_reason is redacted through the hoisted run_secrets.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
        lambda *_a, **_k: {"482913"},
    )
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=["type"],
        extracted_output={"confirmation": "verification code 482913 accepted"},
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the confirmation number",
        extracted_information_schema=None,
    )
    assert "482913" not in task.extracted_information["confirmation"]


@pytest.mark.asyncio
async def test_execute_task_v3_redacts_a_secret_inside_a_non_string_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LoopOutcome is a plain class, so `reason` is unvalidated and a model answering with an object
    # lands here as a dict. The redactor would raise on it, and the enclosing contained_effect would
    # swallow that -- dropping finish_reason AND final_url for the block rather than crashing, which
    # is the kind of silent loss that reads as "the handoff just never ran".
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    workflow_run_context.mask_secrets_in_data = lambda v, **_k: v
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_secret_values_for_run",
        lambda *_a, **_k: {"sk4829137765"},
    )

    outcome = LoopOutcome(
        status="completed", reason={"summary": "entered code sk4829137765"}, billable_actions=["click"]
    )
    block = _make_block(TaskBlock, label="shipping")
    final_page = MagicMock()
    final_page.url = "https://example.test/confirmation"
    final_page.is_closed = MagicMock(return_value=False)

    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_row=_make_own_block_row(),
        workflow_run_id="wr_handoff",
        get_working_page_side_effect=[final_page, final_page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    # The persist still happens AND the secret inside the object is redacted. Skipping redaction for
    # a non-string reason would publish it through the repr, which is the leak this PR exists to close.
    update_block_mock.assert_awaited_once()
    assert update_block_mock.await_args.kwargs["final_url"] == "https://example.test/confirmation"
    persisted = update_block_mock.await_args.kwargs["finish_reason"]
    assert "sk4829137765" not in persisted
    assert REDACTED_SECRET_PLACEHOLDER in persisted


@pytest.mark.asyncio
async def test_execute_task_v3_short_numeric_otp_does_not_scrub_returned_extraction_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Runtime OTP values skip the numeric-length floor that workflow secrets get, and the extraction
    # scrub matches on all-length boundaries -- so a 4-digit code would rewrite the "2024" inside a
    # date the customer actually asked for. Diagnostics can afford that; returned data cannot.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
        lambda *_a, **_k: {"2024"},
    )
    outcome = LoopOutcome(
        status="completed",
        reason="done",
        billable_actions=["type"],
        extracted_output={"invoice_date": "2024-05-01"},
    )
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        data_extraction_goal="Extract the invoice date",
        extracted_information_schema=None,
    )
    assert task.extracted_information["invoice_date"] == "2024-05-01"


@pytest.mark.asyncio
async def test_execute_task_v3_persists_masked_final_url_when_workflow_run_context_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    workflow_run_context.mask_secrets_in_data.return_value = "https://example.test/confirmation?otp=*****"
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )

    outcome = LoopOutcome(status="completed", reason="done here", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")
    final_page = MagicMock()
    final_page.url = "https://example.test/confirmation?otp=123456"
    final_page.is_closed = MagicMock(return_value=False)

    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_row=_make_own_block_row(),
        workflow_run_id="wr_handoff",
        get_working_page_side_effect=[final_page, final_page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    update_block_mock.assert_awaited_once()
    # Masking runs first (asserted above via the mock call), then the persist strips to the bare URL.
    assert update_block_mock.await_args.kwargs["final_url"] == "https://example.test/confirmation"
    # Masking is applied to both handoff fields, not just the URL.
    assert update_block_mock.await_args.kwargs["finish_reason"] == "https://example.test/confirmation?otp=*****"
    masked_inputs = [call.args[0] for call in workflow_run_context.mask_secrets_in_data.call_args_list]
    unmasked_url = "https://example.test/confirmation?otp=123456"
    assert unmasked_url in masked_inputs  # nosemgrep: incomplete-url-substring-sanitization
    assert "done here" in masked_inputs


@pytest.mark.asyncio
async def test_execute_task_v3_persists_gate_rejection_reason_when_completion_vetoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    workflow_run_context.mask_secrets_in_data = lambda v, **_k: v
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )

    # The loop itself reports completion; the deployment completion gate vetoes it, so the
    # persisted handoff must carry the gate's rejection reason, not the loop's "done".
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")

    _step, task, _loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_row=_make_own_block_row(),
        workflow_run_id="wr_handoff",
        completion_gate_vetoes=True,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    assert task.status == TaskStatus.failed
    update_block_mock.assert_awaited_once()
    assert "completion gate rejected" in update_block_mock.await_args.kwargs["finish_reason"]


@pytest.mark.asyncio
async def test_execute_task_v3_own_block_lookup_not_found_skips_persist_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)
    # A run context must be present for this to exercise the real skip-on-not-found path rather
    # than short-circuiting on the (also-valid) no-run-context skip.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    workflow_run_context.mask_secrets_in_data = lambda v, **_k: v
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )

    outcome = LoopOutcome(status="completed", reason="done here", billable_actions=["click"])
    block = _make_block(TaskBlock, label="shipping")

    _step, task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=block,
        own_block_lookup_raises=NotFoundError(),
        workflow_run_id="wr_handoff",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    loop_mock.get_own_block_mock.assert_awaited_once_with(task_id="task-123", organization_id="org-123")
    update_block_mock.assert_not_awaited()
    assert task.status == TaskStatus.completed


@pytest.mark.asyncio
async def test_execute_task_v3_bare_task_does_not_persist_block_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=None,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )

    update_block_mock.assert_not_awaited()
    loop_mock.get_own_block_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_task_v3_handoff_flag_on_reports_last_block_position(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("skyvern.forge.agent.settings.TASK_V3_BLOCK_HANDOFF", True)
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.get_workflow_run_blocks", AsyncMock(return_value=[]))
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", AsyncMock())
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )

    task_block = _make_block(TaskBlock, label="last_block")
    other_block = _make_block(TaskBlock, label="other_block")
    workflow_run_context = MagicMock()
    workflow_run_context.workflow.workflow_definition.blocks = [other_block, task_block]
    workflow_run_context.workflow.workflow_definition.finally_block_label = None
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, _task, loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=task_block,
        workflow_run_id="wr_position",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert "last block of the workflow" in loop_mock.await_args.kwargs["goal"]

    # Reversed order: the same block is now first, so other blocks run after it.
    workflow_run_context.workflow.workflow_definition.blocks = [task_block, other_block]
    _step2, _task2, loop_mock2, _post2 = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=task_block,
        workflow_run_id="wr_position",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert "not the last block" in loop_mock2.await_args.kwargs["goal"]


@pytest.mark.asyncio
async def test_execute_task_v3_persist_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    # A DB or page error while recording the handoff must never fail a run that finished cleanly.
    # A run context must be present so the failing lookup is actually reached (without one, the
    # persist is skipped before the lookup runs at all, and this would pass vacuously).
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    workflow_run_context = MagicMock()
    workflow_run_context.mask_secrets_in_data = lambda v, **_k: v
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context",
        lambda *_a, **_k: workflow_run_context,
    )

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, task, _loop_mock, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=_make_block(TaskBlock, label="shipping"),
        own_block_lookup_raises=RuntimeError("db down"),
        workflow_run_id="wr_handoff",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed


@pytest.mark.asyncio
async def test_execute_task_v3_empty_reason_clears_stale_finish_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retries reuse the block row: a successful attempt with an empty reason must clear (not keep)
    # the failure text a prior attempt persisted, via the explicit "" clear.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    run_context = MagicMock()
    run_context.mask_secrets_in_data = lambda value, **_kwargs: value
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context", lambda *_a, **_k: run_context
    )
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)

    outcome = LoopOutcome(status="completed", reason="", billable_actions=["click"])
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=_make_block(TaskBlock, label="shipping"),
        workflow_run_id="wr_handoff",
        own_block_row=_make_own_block_row(),
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert update_block_mock.await_args.kwargs["finish_reason"] == ""


@pytest.mark.asyncio
async def test_execute_task_v3_persisted_final_url_is_stripped_to_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    # Persisted final_url must keep only scheme://host/path: an OAuth callback token in the query
    # is server-minted, so secret-registry masking alone cannot catch it.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.has_workflow_run_context", lambda *_a, **_k: True
    )
    run_context = MagicMock()
    run_context.mask_secrets_in_data = lambda value, **_kwargs: value
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context", lambda *_a, **_k: run_context
    )
    update_block_mock = AsyncMock()
    monkeypatch.setattr("skyvern.forge.agent.app.DATABASE.observer.update_workflow_run_block", update_block_mock)

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    final_page = MagicMock()
    final_page.url = "https://user:pw@example.test/callback?code=oauth-code#frag"
    final_page.is_closed = MagicMock(return_value=False)
    await _run_execute_task_v3(
        monkeypatch,
        outcome,
        task_block=_make_block(TaskBlock, label="shipping"),
        workflow_run_id="wr_handoff",
        own_block_row=_make_own_block_row(),
        get_working_page_side_effect=[final_page, final_page],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert update_block_mock.await_args.kwargs["final_url"] == "https://example.test/callback"


@pytest.mark.asyncio
async def test_execute_task_v3_floors_runtime_secrets_when_redaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skyvern.forge import agent as agent_mod

    # Org opted out of artifact redaction — runtime-resolved secrets (e.g. a verification code)
    # must still be floored out of the persisted turn text.
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.app.WORKFLOW_CONTEXT_MANAGER.runtime_secret_values_for_artifacts",
        lambda *_a, **_k: {"73914268"},
    )
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["type"])
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[RoundAction("type", {"selector": "#otp", "text": "73914268"}, True, billable=True)]],
        action_round_texts=["typing the verification code 73914268 into the field"],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    # The type action's own row, not the terminal decision row appended after it.
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    assert "73914268" not in (persisted.reasoning or "")
    assert REDACTED_SECRET_PLACEHOLDER in (persisted.reasoning or "")


@pytest.mark.asyncio
async def test_execute_task_v3_caps_persisted_reasoning_length(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import agent as agent_mod

    # A long outcome reason exercises the cap on the decision row too, alongside the action row.
    outcome = LoopOutcome(status="completed", reason="y" * 5000, billable_actions=["click"])
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[RoundAction("click", {"selector": "#a"}, True, billable=True)]],
        action_round_texts=["x" * 5000],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    action_row, decision_row = (
        c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list
    )
    assert len(action_row.reasoning or "") == agent_mod._TASKV3_REASONING_MAX_CHARS
    assert len(decision_row.reasoning or "") == agent_mod._TASKV3_REASONING_MAX_CHARS


@pytest.mark.asyncio
async def test_execute_task_v3_persists_reload_row_with_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.forge import agent as agent_mod

    # Guards the reasoning kwarg-pop in the reload branch: a regression there raises inside the
    # per-action try and the reload row silently vanishes behind a warning.
    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    _step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[RoundAction("reload_page", {"reason": "a page-level handler requested a refresh"}, True)]],
        action_round_texts=["retrying after the handler asked for a refresh"],
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert task.status == TaskStatus.completed
    # The reload row plus the terminal decision row appended after it.
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 2
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args_list[0].kwargs["action"]
    assert persisted.reasoning == "a page-level handler requested a refresh"
