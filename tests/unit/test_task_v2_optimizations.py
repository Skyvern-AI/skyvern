from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.services import task_v2_optimizations
from skyvern.services.task_v2_optimizations import (
    FLAG_NAME_BY_FIELD,
    TaskV2OptimizationFlags,
    compact_task_history,
    generated_loop_item_limit,
    normalize_loop_values,
    resolve_task_v2_optimization_flags,
    should_run_completion_check,
)
from skyvern.services.task_v2_service import _resolve_max_iterations


@pytest.mark.asyncio
async def test_flag_snapshot_resolves_once_per_context(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SimpleNamespace(is_feature_enabled_cached=AsyncMock(return_value=True))
    monkeypatch.setattr(task_v2_optimizations.app, "EXPERIMENTATION_PROVIDER", provider)
    context = SkyvernContext(task_v2_id="tv2_1", organization_id="org_1")

    first = await resolve_task_v2_optimization_flags(context)
    second = await resolve_task_v2_optimization_flags(context)

    assert first == second == TaskV2OptimizationFlags(**dict.fromkeys(FLAG_NAME_BY_FIELD, True))
    assert provider.is_feature_enabled_cached.await_count == len(FLAG_NAME_BY_FIELD)


def test_loop_values_are_deduplicated_and_bounded() -> None:
    values = [" a ", "a", "b", 3, "", "c"]

    assert normalize_loop_values(values, requested_limit=2, apply_guardrail=True) == ["a", "b"]
    assert generated_loop_item_limit(None) == 25
    assert generated_loop_item_limit("1000") == 50


def test_compact_history_does_not_mutate_source() -> None:
    history = [{"task": f"task-{index}", "data": "x" * 3000} for index in range(6)]

    compacted = compact_task_history(history)

    assert [item["task"] for item in compacted] == ["task-2", "task-3", "task-4", "task-5"]
    assert compacted[0]["data"].endswith("...[truncated]")
    assert len(history[0]["data"]) == 3000


@pytest.mark.parametrize(
    ("iteration", "task_type", "has_data", "expected"),
    [
        (0, "navigate", False, True),
        (1, "navigate", False, False),
        (2, "navigate", False, True),
        (1, "extract", False, True),
        (1, "navigate", True, True),
    ],
)
def test_completion_scheduler(iteration: int, task_type: str, has_data: bool, expected: bool) -> None:
    assert (
        should_run_completion_check(
            iteration=iteration,
            task_type=task_type,
            has_extracted_data=has_data,
        )
        is expected
    )


def test_lower_iteration_override_is_ablatable() -> None:
    assert _resolve_max_iterations(10) == 50
    assert _resolve_max_iterations(10, honor_lower_override=True) == 10
    assert _resolve_max_iterations(0, honor_lower_override=True) == 50


@pytest.mark.asyncio
async def test_loop_replay_injects_cached_actions_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from skyvern.webeye.actions import caching

    cached_actions = [SimpleNamespace(action_type="click")]
    retrieve = AsyncMock(side_effect=[cached_actions, []])
    monkeypatch.setattr(caching, "retrieve_action_plan", retrieve)
    browser_state = SimpleNamespace(scrape_website=AsyncMock(return_value=SimpleNamespace()))
    agent_function = AgentFunction()
    context = SkyvernContext(
        task_v2_id="tv2_1",
        task_v2_loop_replay_active=True,
        task_v2_loop_replay_source_task_id="task_first_item",
    )

    with skyvern_context.scoped(context):
        first = await agent_function.prepare_step_execution(
            organization=None,
            task=SimpleNamespace(task_id="task_second", url="https://example.com"),
            step=SimpleNamespace(step_id="step_1"),
            browser_state=browser_state,
        )
        fallback = await agent_function.prepare_step_execution(
            organization=None,
            task=SimpleNamespace(task_id="task_third", url="https://example.com"),
            step=SimpleNamespace(step_id="step_2"),
            browser_state=browser_state,
        )

    assert first == cached_actions
    assert fallback is None
    assert retrieve.await_count == 2


@pytest.mark.asyncio
async def test_loop_replay_parameterizes_single_input_without_llm() -> None:
    from skyvern.webeye.actions.caching import get_user_detail_answers

    context = SkyvernContext(
        task_v2_loop_replay_active=True,
        task_v2_loop_replay_current_value="CVPR2023",
    )
    with skyvern_context.scoped(context):
        answers = await get_user_detail_answers(
            task=SimpleNamespace(),
            step=SimpleNamespace(),
            scraped_page=SimpleNamespace(),
            queries_and_answers={"search term": None},
        )

    assert answers == {"search term": "CVPR2023"}
