"""Tests for the `skyvern.agent.complete_verify` OTEL span attributes (SKY-9174).

The verification span carries two attributes that power the logfire signal used
to measure SKY-9174's acceptance criterion post-rollout:

- ``verification.status``: ``"complete" | "terminate" | "continue"``
- ``verification.template``: ``"check-user-goal" | "check-user-goal-with-termination"``

These tests assert the attributes are set correctly across the three result
shapes and under both prompt-template selections. No behavioral change is being
verified — just the observability plumbing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from tests.unit.helpers import make_browser_state, make_organization, make_step, make_task

COMPLETE_VERIFY_SPAN_NAME = "skyvern.agent.complete_verify"


def _span_by_name(spans: list, name: str):
    return next((s for s in spans if s.name == name), None)


async def _call_complete_verify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_response: dict,
    use_termination_prompt: bool,
) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization, navigation_goal="Submit the contact form")
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

    monkeypatch.setattr(
        "skyvern.forge.agent.load_prompt_with_elements",
        lambda **_kwargs: "rendered prompt",
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
        await agent.complete_verify(page=page, scraped_page=scraped_page, task=task, step=step)
    finally:
        skyvern_context.reset()


@pytest.mark.asyncio
async def test_span_attrs_for_complete_status_legacy_prompt(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    await _call_complete_verify(
        monkeypatch,
        llm_response={"user_goal_achieved": True, "thoughts": "done", "page_info": "ok"},
        use_termination_prompt=False,
    )
    span = _span_by_name(span_exporter.get_finished_spans(), COMPLETE_VERIFY_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("verification.status") == "complete"
    assert attrs.get("verification.template") == "check-user-goal"


@pytest.mark.asyncio
async def test_span_attrs_for_continue_status_legacy_prompt(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    await _call_complete_verify(
        monkeypatch,
        llm_response={"user_goal_achieved": False, "thoughts": "still loading", "page_info": "spinner"},
        use_termination_prompt=False,
    )
    span = _span_by_name(span_exporter.get_finished_spans(), COMPLETE_VERIFY_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("verification.status") == "continue"
    assert attrs.get("verification.template") == "check-user-goal"


@pytest.mark.asyncio
async def test_span_attrs_for_terminate_status_termination_aware_prompt(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    await _call_complete_verify(
        monkeypatch,
        llm_response={
            "status": "terminate",
            "thoughts": "blocked by captcha",
            "page_info": "cloudflare",
            "failure_categories": [{"category": "ANTI_BOT_DETECTION", "confidence_float": 0.9, "reasoning": "cf"}],
        },
        use_termination_prompt=True,
    )
    span = _span_by_name(span_exporter.get_finished_spans(), COMPLETE_VERIFY_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("verification.status") == "terminate"
    assert attrs.get("verification.template") == "check-user-goal-with-termination"


@pytest.mark.asyncio
async def test_span_attrs_for_complete_status_termination_aware_prompt(
    monkeypatch: pytest.MonkeyPatch, span_exporter: InMemorySpanExporter
) -> None:
    await _call_complete_verify(
        monkeypatch,
        llm_response={
            "status": "complete",
            "thoughts": "thank-you page visible",
            "page_info": "thank-you",
            "failure_categories": [],
        },
        use_termination_prompt=True,
    )
    span = _span_by_name(span_exporter.get_finished_spans(), COMPLETE_VERIFY_SPAN_NAME)
    assert span is not None
    attrs = span.attributes or {}
    assert attrs.get("verification.status") == "complete"
    assert attrs.get("verification.template") == "check-user-goal-with-termination"
