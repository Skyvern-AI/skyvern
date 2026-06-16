"""Anthropic CUA first-turn prompt seeding invariants.

When a task has only ``data_extraction_goal``, the first CU call must seed
``prompt`` from it. Navigation tasks must keep seeding from ``navigation_goal``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.llm.api_handler_factory import LLMAPIHandlerFactory


def _make_scraped_page() -> MagicMock:
    scraped_page = MagicMock()
    scraped_page.screenshots = []
    scraped_page.window_dimension = None
    return scraped_page


def _make_llm_caller() -> MagicMock:
    llm_caller = MagicMock()
    llm_caller.current_tool_results = []
    llm_caller.message_history = []
    llm_caller.llm_key = "ANTHROPIC_CLAUDE_SONNET_4_6"
    llm_caller.llm_config = MagicMock()
    llm_caller.llm_config.model_name = "claude-sonnet-4-6"
    llm_caller.browser_window_dimension = None
    llm_caller.get_screenshot_resize_target_dimension = MagicMock(return_value=None)
    llm_caller.call = AsyncMock(return_value={"content": []})
    llm_caller.clear_tool_results = MagicMock()
    return llm_caller


@pytest.mark.asyncio
async def test_first_turn_extraction_task_seeds_with_data_extraction_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-turn Anthropic CU call for an extraction task must seed prompt from
    data_extraction_goal — never None."""
    extraction_goal = "Extract all visible row labels into a flat list."
    task = SimpleNamespace(
        task_id="tsk_extraction_first_turn",
        navigation_goal=None,
        data_extraction_goal=extraction_goal,
    )
    step = MagicMock()
    scraped_page = _make_scraped_page()
    llm_caller = _make_llm_caller()

    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "requires_adaptive_thinking",
        staticmethod(lambda model_name: False),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.parse_anthropic_actions",
        AsyncMock(return_value=[]),
    )

    agent = ForgeAgent.__new__(ForgeAgent)
    await agent._generate_anthropic_actions(
        task=task,
        step=step,
        scraped_page=scraped_page,
        llm_caller=llm_caller,
    )

    llm_caller.call.assert_awaited_once()
    seeded_prompt = llm_caller.call.await_args.kwargs["prompt"]
    assert seeded_prompt is not None, "First-turn Anthropic CU call must not seed prompt=None for an extraction task."
    assert seeded_prompt == extraction_goal, (
        f"Extraction task should seed with data_extraction_goal; got: {seeded_prompt!r}"
    )


@pytest.mark.asyncio
async def test_first_turn_navigation_task_keeps_seeding_with_navigation_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A navigation task must keep using navigation_goal — the fix must not
    regress the navigation path."""
    navigation_goal = "Open the help menu and click About."
    task = SimpleNamespace(
        task_id="tsk_navigation_first_turn",
        navigation_goal=navigation_goal,
        data_extraction_goal=None,
    )
    step = MagicMock()
    scraped_page = _make_scraped_page()
    llm_caller = _make_llm_caller()

    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "requires_adaptive_thinking",
        staticmethod(lambda model_name: False),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.parse_anthropic_actions",
        AsyncMock(return_value=[]),
    )

    agent = ForgeAgent.__new__(ForgeAgent)
    await agent._generate_anthropic_actions(
        task=task,
        step=step,
        scraped_page=scraped_page,
        llm_caller=llm_caller,
    )

    llm_caller.call.assert_awaited_once()
    seeded_prompt = llm_caller.call.await_args.kwargs["prompt"]
    assert seeded_prompt == navigation_goal, (
        f"Navigation task should keep seeding with navigation_goal; got: {seeded_prompt!r}"
    )


@pytest.mark.asyncio
async def test_first_turn_with_no_goal_seeds_prompt_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither goal is set the first CU call still fires with prompt=None."""
    task = SimpleNamespace(
        task_id="tsk_no_goal_first_turn",
        navigation_goal=None,
        data_extraction_goal=None,
    )
    step = MagicMock()
    scraped_page = _make_scraped_page()
    llm_caller = _make_llm_caller()

    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "requires_adaptive_thinking",
        staticmethod(lambda model_name: False),
    )
    monkeypatch.setattr(
        "skyvern.forge.agent.parse_anthropic_actions",
        AsyncMock(return_value=[]),
    )

    agent = ForgeAgent.__new__(ForgeAgent)
    await agent._generate_anthropic_actions(
        task=task,
        step=step,
        scraped_page=scraped_page,
        llm_caller=llm_caller,
    )

    llm_caller.call.assert_awaited_once()
    assert llm_caller.call.await_args.kwargs["prompt"] is None
