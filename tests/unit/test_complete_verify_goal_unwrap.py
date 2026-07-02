"""Tests for SKY-11295: complete_verify and the after-click verifier thread
MINI_GOAL_TEMPLATE-unwrapped goal fields (mini goal + big_goal_context) into
the check-user-goal prompts, and pass unwrapped goals through untouched."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.copilot.block_goal_wrapping import compose_mini_goal
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.handler import _build_after_click_verify_prompt
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task

COMPLETE_VERIFY_SPAN_NAME = "skyvern.agent.complete_verify"

MAIN_GOAL = "Open the example site, find the pricing page, and report the plan names"
MINI_GOAL = "Click the link that leads to the pricing page"
TERMINATE_MINI = "The site shows a permanent maintenance page"
ACTION_HISTORY_STUB = '[{"action": "click", "result": "success"}]'


def _span_by_name(spans: list, name: str):
    return next((s for s in spans if s.name == name), None)


async def _call_complete_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_overrides: dict[str, Any],
    use_termination_prompt: bool,
) -> dict[str, Any]:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, **task_overrides)
    step = make_step(
        now,
        task,
        step_id="step-verify",
        status=StepStatus.running,
        order=0,
        output=None,
    )
    _, scraped_page, page = make_browser_state()

    scraped_page_refreshed = AsyncMock()
    scraped_page_refreshed.screenshots = [b"image"]
    scraped_page.refresh = AsyncMock(return_value=scraped_page_refreshed)

    monkeypatch.setattr(
        "skyvern.forge.agent.service_utils.is_cua_task",
        AsyncMock(return_value=False),
    )

    async def feature_flag_side_effect(flag_name: str, *_args, **_kwargs) -> bool:
        if flag_name == "USE_TERMINATION_AWARE_COMPLETE_VERIFICATION":
            return use_termination_prompt
        return False

    monkeypatch.setattr(
        "skyvern.forge.agent.app.EXPERIMENTATION_PROVIDER.is_feature_enabled_cached",
        AsyncMock(side_effect=feature_flag_side_effect),
    )

    captured_kwargs: dict[str, Any] = {}

    def capture_prompt(**kwargs: Any) -> str:
        captured_kwargs.update(kwargs)
        return "rendered prompt"

    monkeypatch.setattr("skyvern.forge.agent.load_prompt_with_elements", capture_prompt)
    monkeypatch.setattr(ForgeAgent, "_get_action_results", AsyncMock(return_value=ACTION_HISTORY_STUB))

    llm_response = (
        {"status": "complete", "thoughts": "done", "page_info": "ok", "failure_categories": []}
        if use_termination_prompt
        else {"user_goal_achieved": True, "thoughts": "done", "page_info": "ok"}
    )
    llm_handler = AsyncMock(return_value=llm_response)
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        lambda *_args, **_kwargs: llm_handler,
    )

    context = SkyvernContext(
        task_id=task.task_id,
        step_id=None,
        organization_id=task.organization_id,
        workflow_run_id=task.workflow_run_id,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    try:
        await agent.complete_verify(
            page=page,
            scraped_page=scraped_page,
            task=task,
            step=step,
            verification_trigger="periodic_after_step",
        )
    finally:
        skyvern_context.reset()
    return captured_kwargs


@pytest.mark.asyncio
async def test_wrapped_goal_threads_mini_and_context_legacy_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _call_complete_verify(
        monkeypatch,
        task_overrides={"navigation_goal": compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=MINI_GOAL)},
        use_termination_prompt=False,
    )
    assert captured["navigation_goal"] == MINI_GOAL
    assert captured["big_goal_context"] == MAIN_GOAL
    assert captured["template_name"] == "check-user-goal"
    # Step-scale mini goals are often action-phrased; the verifier gets the
    # action history even though include_action_history_in_verification is off.
    assert captured["action_history"] == ACTION_HISTORY_STUB


@pytest.mark.asyncio
async def test_wrapped_goal_and_criterion_thread_under_termination_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _call_complete_verify(
        monkeypatch,
        task_overrides={
            "navigation_goal": compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=MINI_GOAL),
            "terminate_criterion": compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=TERMINATE_MINI),
        },
        use_termination_prompt=True,
    )
    assert captured["navigation_goal"] == MINI_GOAL
    assert captured["terminate_criterion"] == TERMINATE_MINI
    assert captured["big_goal_context"] == MAIN_GOAL
    assert captured["template_name"] == "check-user-goal-with-termination"


@pytest.mark.asyncio
async def test_unwrapped_goal_passes_through_with_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _call_complete_verify(
        monkeypatch,
        task_overrides={
            "navigation_goal": "Submit the contact form",
            "complete_criterion": "A thank-you banner is visible",
        },
        use_termination_prompt=False,
    )
    assert captured["navigation_goal"] == "Submit the contact form"
    assert captured["complete_criterion"] == "A thank-you banner is visible"
    assert captured["terminate_criterion"] is None
    assert captured["big_goal_context"] is None
    assert captured["action_history"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [True, False])
async def test_span_carries_goal_unwrapped_attribute(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter, wrapped: bool
) -> None:
    goal = compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=MINI_GOAL) if wrapped else "Submit the contact form"
    await _call_complete_verify(
        monkeypatch,
        task_overrides={"navigation_goal": goal},
        use_termination_prompt=False,
    )
    span = _span_by_name(span_exporter.get_finished_spans(), COMPLETE_VERIFY_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("verification.goal_unwrapped") is wrapped


async def _call_after_click_prompt_build(monkeypatch: pytest.MonkeyPatch, *, navigation_goal: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal=navigation_goal)

    captured_kwargs: dict[str, Any] = {}

    def capture_prompt(**kwargs: Any) -> str:
        captured_kwargs.update(kwargs)
        return "rendered prompt"

    monkeypatch.setattr("skyvern.webeye.actions.handler.load_prompt_with_elements", capture_prompt)
    monkeypatch.setattr(
        "skyvern.webeye.actions.handler.get_slim_output_template_value",
        AsyncMock(return_value=None),
    )

    context = SkyvernContext(
        task_id=task.task_id,
        organization_id=task.organization_id,
        tz_info=ZoneInfo("UTC"),
    )
    skyvern_context.set(context)
    try:
        await _build_after_click_verify_prompt(task, MagicMock(), {"1"}, "[]")
    finally:
        skyvern_context.reset()
    return captured_kwargs


@pytest.mark.asyncio
async def test_after_click_verifier_unwraps_wrapped_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _call_after_click_prompt_build(
        monkeypatch,
        navigation_goal=compose_mini_goal(main_goal=MAIN_GOAL, mini_goal=MINI_GOAL),
    )
    assert captured["navigation_goal"] == MINI_GOAL
    assert captured["big_goal_context"] == MAIN_GOAL
    assert captured["template_name"] == "check-user-goal"


@pytest.mark.asyncio
async def test_after_click_verifier_passes_plain_goal_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _call_after_click_prompt_build(monkeypatch, navigation_goal="Pick the first dropdown option")
    assert captured["navigation_goal"] == "Pick the first dropdown option"
    assert captured["big_goal_context"] is None
