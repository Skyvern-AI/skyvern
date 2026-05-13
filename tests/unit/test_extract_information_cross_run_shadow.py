"""Handler-level tests for cross-run + in-run extraction cache hit shadow scheduling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from skyvern.forge.sdk.cache import extraction_cache
from skyvern.webeye.actions import handler
from tests.unit.test_extract_information_retry_bypass import (
    _make_scraped_page,
    _make_task,
    _stub_handler_dependencies,
)


def _install_capturing_llm_stub(monkeypatch) -> list[dict]:
    # Override the shared `_stub_handler_dependencies` LLM stub (which discards
    # kwargs) so prompt/system_prompt forwarding through the helper closure is
    # observable.
    captured: list[dict] = []

    async def capturing_llm(**kwargs):
        captured.append(kwargs)
        return {"extracted_info": "shadow_response"}

    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: capturing_llm,
    )
    return captured


def test_cross_run_hit_schedules_shadow_check_with_sentinel_age(monkeypatch) -> None:
    # Cross-run scheduler kwargs must use the unknown-age sentinel; closure must
    # forward the rendered prompt and task system_prompt.
    extraction_cache._reset_for_tests()
    workflow_run_id = "wfr_cross_run_shadow_args"
    workflow_permanent_id = "wpid_cross_run_shadow_args"
    cache_key = "synthetic_cache_key_cross_run_args"
    cross_run_value = {"docs": ["from_redis.pdf"]}

    llm_calls: list[int] = []
    _stub_handler_dependencies(monkeypatch, llm_calls, cache_key)
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "lookup_cross_run_extraction_cache",
        AsyncMock(return_value=cross_run_value),
    )
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=True),
    )
    schedule_mock = MagicMock(return_value=None)
    monkeypatch.setattr(handler.extraction_shadow, "schedule_shadow_check", schedule_mock)
    captured_llm_kwargs = _install_capturing_llm_stub(monkeypatch)

    scraped_page = _make_scraped_page()
    task = _make_task(workflow_run_id, workflow_permanent_id=workflow_permanent_id)
    step = MagicMock(step_id="stp_cross_run_args", retry_index=0)

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page))

    assert schedule_mock.call_count == 1
    kwargs = schedule_mock.call_args.kwargs
    assert kwargs["cached_value"] == cross_run_value
    assert kwargs["cached_age_seconds"] == -1.0
    assert kwargs["cache_key"] == cache_key
    assert kwargs["workflow_run_id"] == workflow_run_id
    assert kwargs["schema"] == task.extracted_information_schema

    # The prompt/system_prompt are captured inside the llm_call closure, not
    # surfaced as scheduler kwargs — invoke the closure to observe them.
    asyncio.run(kwargs["llm_call"]())
    assert len(captured_llm_kwargs) == 1
    assert captured_llm_kwargs[0]["prompt"] == "rendered-prompt"
    assert captured_llm_kwargs[0]["system_prompt"] == task.workflow_system_prompt
    assert captured_llm_kwargs[0]["screenshots"] == list(scraped_page.screenshots)
    assert captured_llm_kwargs[0]["step"] is None
    assert captured_llm_kwargs[0]["prompt_name"] == "extract-information"

    extraction_cache._reset_for_tests()


def test_in_run_hit_schedules_shadow_check_with_real_age(monkeypatch) -> None:
    # In-run scheduler kwargs must propagate the LookupResult's real age, not
    # the cross-run-only sentinel; pins helper-call wiring on the in-run branch.
    extraction_cache._reset_for_tests()
    workflow_run_id = "wfr_in_run_shadow_args"
    workflow_permanent_id = "wpid_in_run_shadow_args"
    cache_key = "synthetic_cache_key_in_run_args"
    in_run_value: dict[str, Any] = {"docs": ["from_in_run.pdf"]}
    real_age_seconds = 42.0

    llm_calls: list[int] = []
    _stub_handler_dependencies(monkeypatch, llm_calls, cache_key)
    in_run_lookup_result = extraction_cache.LookupResult(
        hit=True,
        value=in_run_value,
        scope=extraction_cache.SCOPE_RUN,
        age_seconds=real_age_seconds,
        fallback_reason=None,
    )
    monkeypatch.setattr(handler.extraction_cache, "lookup", lambda wfr_id, key: in_run_lookup_result)
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=True),
    )
    schedule_mock = MagicMock(return_value=None)
    monkeypatch.setattr(handler.extraction_shadow, "schedule_shadow_check", schedule_mock)
    captured_llm_kwargs = _install_capturing_llm_stub(monkeypatch)

    scraped_page = _make_scraped_page()
    task = _make_task(workflow_run_id, workflow_permanent_id=workflow_permanent_id)
    step = MagicMock(step_id="stp_in_run_args", retry_index=0)

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page))

    assert schedule_mock.call_count == 1
    kwargs = schedule_mock.call_args.kwargs
    assert kwargs["cached_value"] == in_run_value
    assert kwargs["cached_age_seconds"] == real_age_seconds
    assert kwargs["cache_key"] == cache_key
    assert kwargs["workflow_run_id"] == workflow_run_id
    assert kwargs["schema"] == task.extracted_information_schema

    asyncio.run(kwargs["llm_call"]())
    assert len(captured_llm_kwargs) == 1
    assert captured_llm_kwargs[0]["prompt"] == "rendered-prompt"
    assert captured_llm_kwargs[0]["system_prompt"] == task.workflow_system_prompt
    assert captured_llm_kwargs[0]["screenshots"] == list(scraped_page.screenshots)
    assert captured_llm_kwargs[0]["step"] is None
    assert captured_llm_kwargs[0]["prompt_name"] == "extract-information"

    extraction_cache._reset_for_tests()


def test_cross_run_hit_shadow_gate_runs_in_background_not_on_hot_path(monkeypatch) -> None:
    # A deliberately-slow gate must not block the handler's cache-hit return.
    # gate_calls is empty when the handler returns and populated after the
    # captured background task is awaited.
    extraction_cache._reset_for_tests()
    workflow_run_id = "wfr_cross_run_shadow_bg"
    workflow_permanent_id = "wpid_cross_run_shadow_bg"
    cache_key = "synthetic_cache_key_cross_run_bg"
    cross_run_value = {"docs": ["from_redis.pdf"]}

    llm_calls: list[int] = []
    _stub_handler_dependencies(monkeypatch, llm_calls, cache_key)
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "lookup_cross_run_extraction_cache",
        AsyncMock(return_value=cross_run_value),
    )
    captured_llm_kwargs = _install_capturing_llm_stub(monkeypatch)

    gate_calls: list[int] = []

    async def _slow_gate(_task) -> bool:
        await asyncio.sleep(0.5)
        gate_calls.append(1)
        return True

    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "should_shadow_extraction_cache_hit", _slow_gate)

    real_schedule = handler.extraction_shadow.schedule_shadow_check
    captured_tasks: list[asyncio.Task] = []

    def _capturing_schedule(**kwargs):
        task = real_schedule(**kwargs)
        if task is not None:
            captured_tasks.append(task)
        return task

    monkeypatch.setattr(handler.extraction_shadow, "schedule_shadow_check", _capturing_schedule)

    scraped_page = _make_scraped_page()
    task = _make_task(workflow_run_id, workflow_permanent_id=workflow_permanent_id)
    step = MagicMock(step_id="stp_cross_run_bg", retry_index=0)

    async def _run_and_assert() -> Any:
        result = await handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page)
        # The slow gate appends to gate_calls only AFTER its sleep, so an empty
        # list here proves the handler returned without awaiting it.
        assert gate_calls == []
        for bg_task in captured_tasks:
            await bg_task
        return result

    result = asyncio.run(_run_and_assert())

    assert result.scraped_data == cross_run_value
    assert len(captured_tasks) == 1
    assert gate_calls == [1]
    assert len(captured_llm_kwargs) == 1
    assert captured_llm_kwargs[0]["prompt"] == "rendered-prompt"
    extraction_cache._reset_for_tests()
