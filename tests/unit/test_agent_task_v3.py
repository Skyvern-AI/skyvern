"""Executor-level tests for the Task V3 dispatch path (`ForgeAgent._execute_task_v3`).

The engine tool-loop itself is unit-tested in test_taskv3_*; here we mock it out and
assert the wiring around it: the loop runs once for the whole task, its outcome maps
onto task/step status, the browser actions it reports are emitted as billable
action-results, and the per-step billing hook is invoked with that step so a v3 run
meters per action exactly like the step engine.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.taskv3.engine import MIN_ACTION_STEPS
from skyvern.forge.taskv3.loop import LoopOutcome
from skyvern.webeye.actions.actions import ActionType
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task


async def _run_execute_task_v3(
    monkeypatch: pytest.MonkeyPatch,
    outcome: LoopOutcome,
    post_step_side_effect: BaseException | None = None,
    action_rounds: list[list[tuple[str, dict[str, Any]]]] | None = None,
    screenshot_raises: bool = False,
    context_overrides: dict[str, Any] | None = None,
    **task_overrides: Any,
) -> tuple[Step, Any, AsyncMock, AsyncMock]:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, **task_overrides)
    step = make_step(now, task, step_id="step-v3", status=StepStatus.created, order=0, output=None)

    browser_state, _, page = make_browser_state()
    browser_state.must_get_working_page = AsyncMock(return_value=page)
    browser_state.take_post_action_screenshot = AsyncMock(
        return_value=b"png-bytes",
        side_effect=RuntimeError("screenshot boom") if screenshot_raises else None,
    )

    async def _loop(**kwargs: Any) -> LoopOutcome:
        cb = kwargs.get("on_action_round")
        if cb is not None and action_rounds:
            for round_actions in action_rounds:
                await cb(round_actions)
        return outcome

    loop_mock = AsyncMock(side_effect=_loop)
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
    # _execute_task_v3 builds auth tools, whose credential-candidate gate reads the workflow-run
    # context; stub it out so executor tests (which don't exercise auth) don't hit that lookup.
    monkeypatch.setattr("skyvern.forge.taskv3.auth_tools.has_credential_totp_candidate", lambda *_a, **_k: False)

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
async def test_execute_task_v3_leaves_extraction_unscrubbed_when_redaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Secret values are available, but the gate itself is off — the scrub must not run at all.
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


@pytest.mark.asyncio
async def test_execute_task_v3_persists_per_action_screenshots_and_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    # The loop reports two successful action rounds; the closure must capture one screenshot per
    # round and persist one actions-table row per action (FK'ing the round's screenshot), so the
    # Task API's action_screenshot_urls and GET /tasks/{id}/actions are populated for v3.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click", "type", "click"])
    rounds = [
        [("click", {"selector": "#a"}), ("type", {"selector": "#b", "text": "x"})],
        [("click", {"selector": "#submit"})],
    ]
    step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=rounds,
        workflow_run_id="wr_v3test",
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert step.status == StepStatus.completed
    # One SCREENSHOT_ACTION artifact per action round; one actions-table row per action.
    assert agent_mod.app.ARTIFACT_MANAGER.create_artifact.await_count == 2
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 3
    persisted = [c.kwargs["action"] for c in agent_mod.app.DATABASE.workflow_params.create_action.await_args_list]
    # organization_id/task_id/step_id must be set, or GET /tasks/{id}/actions filters the rows out;
    # workflow_run_id must carry through for workflow-level action attribution (parity with the step engine).
    assert all(a.organization_id == task.organization_id for a in persisted)
    assert all(a.workflow_run_id == task.workflow_run_id for a in persisted)
    assert all(a.task_id == task.task_id and a.step_id == step.step_id for a in persisted)
    assert all(a.screenshot_artifact_id == "artifact-1" for a in persisted)
    assert [a.action_type for a in persisted] == [ActionType.CLICK, ActionType.INPUT_TEXT, ActionType.CLICK]


@pytest.mark.asyncio
async def test_execute_task_v3_persists_action_row_when_screenshot_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A screenshot-capture failure must not lose the action row: it persists with a null screenshot FK.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="completed", reason="done", billable_actions=["click"])
    step, task, _loop, _post = await _run_execute_task_v3(
        monkeypatch,
        outcome,
        action_rounds=[[("click", {"selector": "#a"})]],
        screenshot_raises=True,
        data_extraction_goal=None,
        extracted_information_schema=None,
    )
    assert step.status == StepStatus.completed
    assert agent_mod.app.ARTIFACT_MANAGER.create_artifact.await_count == 0  # capture raised before create
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 1
    persisted = agent_mod.app.DATABASE.workflow_params.create_action.await_args.kwargs["action"]
    assert persisted.screenshot_artifact_id is None
    assert persisted.organization_id == task.organization_id


@pytest.mark.asyncio
async def test_execute_task_v3_no_action_rounds_persists_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run with no successful action rounds (e.g. terminate) persists no per-action artifacts/rows.
    from skyvern.forge import agent as agent_mod

    outcome = LoopOutcome(status="terminated", reason="blocked", billable_actions=[])
    await _run_execute_task_v3(monkeypatch, outcome, action_rounds=None)
    assert agent_mod.app.ARTIFACT_MANAGER.create_artifact.await_count == 0
    assert agent_mod.app.DATABASE.workflow_params.create_action.await_count == 0


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
        ):
            mock_app.DATABASE.tasks.get_task = AsyncMock(return_value=None)
            mock_app.DATABASE.tasks.update_task = AsyncMock(return_value=task)
            mock_app.AGENT_FUNCTION.validate_step_execution = AsyncMock()
            mock_app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached = AsyncMock(return_value=False)
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
