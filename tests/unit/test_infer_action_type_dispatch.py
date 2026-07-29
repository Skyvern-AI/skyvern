"""Tests for the TaskType.action action-type dispatch in
``ForgeAgent._build_extract_action_prompt`` (SKY-12817)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.exceptions import FailedToParseActionInstruction
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.db.enums import TaskType
from skyvern.forge.sdk.models import StepStatus
from tests.unit.helpers import make_organization, make_step, make_task


def _make_scraped_page() -> MagicMock:
    sp = MagicMock()
    sp.build_element_tree.return_value = "<div data-skyvern='1'>mock</div>"
    sp.build_lean_elements_tree.return_value = "<div>mock</div>"
    sp.screenshots = [b"img"]
    sp.elements = []
    sp.html = "<html><body><div>mock</div></body></html>"
    sp.last_used_element_tree_html = None
    return sp


def _make_browser_state() -> MagicMock:
    bs = MagicMock()
    page = AsyncMock()
    bs.get_working_page = AsyncMock(return_value=page)
    return bs


@pytest.fixture
def patched_agent(monkeypatch: pytest.MonkeyPatch) -> ForgeAgent:
    agent = ForgeAgent()
    monkeypatch.setattr(agent, "_get_action_results", AsyncMock(return_value=""))
    monkeypatch.setattr(agent, "_build_navigation_payload", MagicMock(return_value={}))
    monkeypatch.setattr(
        "skyvern.forge.agent.SkyvernFrame.evaluate",
        AsyncMock(return_value="https://example.com/path"),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.build_open_tabs_context",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(agent, "_get_prompt_caching_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "skyvern.forge.agent.get_slim_output_template_value",
        AsyncMock(return_value=False),
    )
    return agent


def _patch_infer_response(monkeypatch: pytest.MonkeyPatch, json_response: dict) -> None:
    fake_llm = AsyncMock(return_value=json_response)
    monkeypatch.setattr(
        "skyvern.forge.agent.LLMAPIHandlerFactory.get_override_llm_api_handler",
        MagicMock(return_value=fake_llm),
    )


async def _build_for_action_task(patched_agent: ForgeAgent, navigation_goal: str):
    now = datetime.now(UTC)
    org = make_organization(now)
    task = make_task(now, org, task_type=TaskType.action, navigation_goal=navigation_goal)
    step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)

    ctx = SkyvernContext(tz_info=None)
    token = skyvern_context._context.set(ctx)
    try:
        return await patched_agent._build_extract_action_prompt(
            task,
            step,
            _make_browser_state(),
            _make_scraped_page(),
        )
    finally:
        skyvern_context._context.reset(token)


@pytest.mark.asyncio
async def test_inferred_hover_dispatches_to_single_hover_template(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_infer_response(
        monkeypatch,
        {"inferred_actions": [{"action_type": "HOVER", "confidence_float": 0.9}]},
    )

    build_result = await _build_for_action_task(patched_agent, "Hover over the account menu")

    assert build_result.prompt_name == "single-hover-action"
    assert '"HOVER"' in build_result.prompt


@pytest.mark.asyncio
async def test_unknown_action_still_raises_failed_to_parse(
    patched_agent: ForgeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_infer_response(
        monkeypatch,
        {"error": "UNKNOWN_ACTION", "thought": "The instruction asks to drag an item."},
    )

    with pytest.raises(FailedToParseActionInstruction):
        await _build_for_action_task(patched_agent, "Drag the card to the other column")
