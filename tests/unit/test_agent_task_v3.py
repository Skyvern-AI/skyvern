"""Executor-level tests for the Task V3 dispatch path (`ForgeAgent._execute_task_v3`).

The engine tool-loop itself is unit-tested in test_taskv3_*; here we mock it out and
assert the wiring around it: the loop runs once for the whole task, its outcome maps
onto task/step status, the browser actions it reports are emitted as billable
action-results, and the per-step billing hook is invoked with that step so a v3 run
meters per action exactly like the step engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.taskv3.loop import LoopOutcome
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task


async def _run_execute_task_v3(
    monkeypatch: pytest.MonkeyPatch,
    outcome: LoopOutcome,
    post_step_side_effect: BaseException | None = None,
    **task_overrides: Any,
) -> tuple[Step, Any, AsyncMock, AsyncMock]:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, **task_overrides)
    step = make_step(now, task, step_id="step-v3", status=StepStatus.created, order=0, output=None)

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)

    loop_mock = AsyncMock(return_value=outcome)
    monkeypatch.setattr("skyvern.forge.taskv3.engine.run_task_v3_agent_loop", loop_mock)
    monkeypatch.setattr("skyvern.forge.agent.LLMCaller", MagicMock())
    monkeypatch.setattr("skyvern.forge.sdk.api.files.resolve_run_download_id", lambda *_a, **_k: "download-1")
    monkeypatch.setattr("skyvern.forge.sdk.api.files.get_download_dir", lambda *_a, **_k: "/tmp/taskv3-test")

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
        return task

    agent.update_step = AsyncMock(side_effect=fake_update_step)
    agent.update_task = AsyncMock(side_effect=fake_update_task)
    agent.clean_up_task = AsyncMock()

    post_step_mock = AsyncMock(side_effect=post_step_side_effect)
    monkeypatch.setattr("skyvern.forge.agent.app.AGENT_FUNCTION.post_step_execution", post_step_mock)

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=step.step_id,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
    )
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
        )
    finally:
        skyvern_context.reset()

    return out_step, out_task, loop_mock, post_step_mock


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

    # The whole task runs as one loop invocation.
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
    outcome = LoopOutcome(status="budget_exhausted", reason="max_turns reached", billable_actions=["click"])
    step, task, _loop, post_step_mock = await _run_execute_task_v3(monkeypatch, outcome)

    assert step.status == StepStatus.failed
    # The hook is still called; it self-guards on completed status, so a failed step bills nothing.
    post_step_mock.assert_awaited_once()


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
    assert step.status == StepStatus.failed
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


# ---------------------------------------------------------------------------
# Task V3 model selection: task.llm_key -> TASK_V3_LLM_NAME (PostHog) -> TASK_V3_LLM_KEY -> LLM_KEY
# ---------------------------------------------------------------------------


def _v3_task(llm_key: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(llm_key=llm_key, task_id="tsk_x", organization_id="o_test")


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
