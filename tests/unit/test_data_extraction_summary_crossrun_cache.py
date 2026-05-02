"""Regression tests for the data-extraction-summary cross-run cache wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _run_create_extract_action(
    monkeypatch,
    *,
    cross_run_lookup_value,
    task_wpid: str | None,
    context_wpid: str | None,
):
    from skyvern.forge import agent as agent_module

    llm_calls: list[dict] = []

    async def fake_llm_handler(**kwargs):
        llm_calls.append(kwargs)
        return {"summary": "fresh-from-llm"}

    lookup_mock = AsyncMock(return_value=cross_run_lookup_value)
    store_mock = AsyncMock(return_value=None)
    in_run_store_mock = MagicMock()

    agent_function_stub = MagicMock()
    agent_function_stub.lookup_cross_run_extraction_cache = lookup_mock
    agent_function_stub.store_cross_run_extraction_cache = store_mock

    monkeypatch.setattr(agent_module.app, "EXTRACTION_LLM_API_HANDLER", fake_llm_handler)
    monkeypatch.setattr(agent_module.app, "AGENT_FUNCTION", agent_function_stub)
    monkeypatch.setattr(
        agent_module.skyvern_context,
        "ensure_context",
        lambda: MagicMock(tz_info=None, workflow_run_id="wr_test", workflow_permanent_id=context_wpid),
    )
    monkeypatch.setattr(agent_module.extraction_cache, "compute_cache_key", lambda **_: "cachekey_test")
    monkeypatch.setattr(agent_module.extraction_cache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(agent_module.extraction_cache, "store", in_run_store_mock)

    task = MagicMock()
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = {"type": "object"}
    task.task_id = "tsk_test"
    task.workflow_run_id = "wr_test"
    task.workflow_permanent_id = task_wpid
    task.organization_id = "o_test"
    task.workflow_system_prompt = None

    step = MagicMock(step_id="stp_test", order=0)
    scraped_page = MagicMock(url="https://example.test")

    result = asyncio.run(agent_module.ForgeAgent.create_extract_action(task=task, step=step, scraped_page=scraped_page))
    return {
        "result": result,
        "llm_calls": llm_calls,
        "cross_run_lookup": lookup_mock,
        "cross_run_store": store_mock,
        "in_run_store": in_run_store_mock,
    }


def test_cross_run_hit_skips_llm_and_backfills_in_run(monkeypatch) -> None:
    captured = _run_create_extract_action(
        monkeypatch,
        cross_run_lookup_value={"summary": "from-cross-run"},
        task_wpid=None,
        context_wpid="wpid_from_context",
    )

    assert captured["cross_run_lookup"].await_args.args[0] == "wpid_from_context"
    assert captured["llm_calls"] == [], "LLM was called despite cross-run hit"
    assert captured["in_run_store"].called, "Cross-run hit should backfill the in-run cache"
    assert not captured["cross_run_store"].called, "No store on the hit path"
    assert captured["result"].reasoning == "from-cross-run"


def test_cross_run_miss_calls_llm_and_dual_writes(monkeypatch) -> None:
    captured = _run_create_extract_action(
        monkeypatch,
        cross_run_lookup_value=None,
        task_wpid=None,
        context_wpid="wpid_from_context",
    )

    assert len(captured["llm_calls"]) == 1, "LLM should be called once on full miss"
    assert captured["in_run_store"].called, "In-run store should be called after LLM"
    assert captured["cross_run_store"].called, "Cross-run store should be called after LLM"
    assert captured["cross_run_store"].await_args.args[0] == "wpid_from_context"
    assert captured["cross_run_store"].await_args.args[2] == {"summary": "fresh-from-llm"}
    assert captured["result"].reasoning == "fresh-from-llm"


def test_task_wpid_takes_precedence_when_set(monkeypatch) -> None:
    captured = _run_create_extract_action(
        monkeypatch,
        cross_run_lookup_value=None,
        task_wpid="wpid_from_task",
        context_wpid="wpid_from_context",
    )

    assert captured["cross_run_lookup"].await_args.args[0] == "wpid_from_task"
    assert captured["cross_run_store"].await_args.args[0] == "wpid_from_task"
