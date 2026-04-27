"""Regression test for SKY-8992 — cross-run cache wpid fallback.

``task.workflow_permanent_id`` is ``None`` on almost every fetch path (the
``tasks`` DB table has no such column; only ``get_tasks()`` populates it via
an outer join). Without a fallback, every cross-run cache call received
``workflow_permanent_id=None`` and the cloud override's guard short-circuited
before writing to Redis. Assert the handler now falls back to
``skyvern_context`` so the cache calls carry a non-None wpid.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _run_extract_information_and_capture_cache_calls(
    monkeypatch,
    *,
    task_wpid: str | None,
    context_wpid: str | None,
) -> dict[str, object]:
    from skyvern.webeye.actions import handler

    captured: dict[str, object] = {}

    def fake_load_prompt_with_elements_tracked(**kwargs):
        return "rendered-prompt", {
            "extracted_text": kwargs.get("extracted_text"),
            "previous_extracted_information": kwargs.get("previous_extracted_information"),
            "extracted_information_schema": kwargs.get("extracted_information_schema"),
        }

    async def fake_llm_handler(**_kwargs):
        # Return a dict so ``isinstance(json_response, (dict, list, str))`` is True
        # and the dual-write block is entered.
        return {"result": "ok"}

    lookup_mock = AsyncMock(return_value=None)
    store_mock = AsyncMock(return_value=None)
    agent_function_stub = MagicMock()
    agent_function_stub.lookup_cross_run_extraction_cache = lookup_mock
    agent_function_stub.store_cross_run_extraction_cache = store_mock

    monkeypatch.setattr(handler, "load_prompt_with_elements_tracked", fake_load_prompt_with_elements_tracked)
    monkeypatch.setattr(
        handler,
        "ensure_context",
        lambda: MagicMock(tz_info=None, workflow_permanent_id=context_wpid),
    )
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_llm_handler,
    )
    # Force a non-None cache_key so the lookup + store paths both execute.
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: "cachekey_test")
    monkeypatch.setattr(handler.extraction_cache, "store", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handler.app, "AGENT_FUNCTION", agent_function_stub)

    refreshed = MagicMock()
    refreshed.extracted_text = "TXT"
    refreshed.url = "https://example.test"
    refreshed.screenshots = []
    refreshed.last_used_element_tree_html = "<a>link</a>"
    refreshed.build_element_tree = MagicMock(return_value="<a>link</a>")
    refreshed.support_economy_elements_tree = MagicMock(return_value=False)

    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []

    task = MagicMock()
    task.navigation_goal = None
    task.navigation_payload = None
    task.extracted_information = None
    task.extracted_information_schema = {"type": "object"}
    task.data_extraction_goal = "Extract documents"
    task.error_code_mapping = None
    task.llm_key = None
    task.workflow_run_id = "wr_test"
    task.workflow_permanent_id = task_wpid
    task.task_id = "tsk_test"
    task.include_extracted_text = True

    asyncio.run(
        handler.extract_information_for_navigation_goal(
            task=task, step=MagicMock(retry_index=0), scraped_page=scraped_page
        )
    )

    captured["lookup_calls"] = lookup_mock.await_args_list
    captured["store_calls"] = store_mock.await_args_list
    return captured


def test_cache_calls_use_context_wpid_when_task_wpid_is_none(monkeypatch) -> None:
    captured = _run_extract_information_and_capture_cache_calls(
        monkeypatch,
        task_wpid=None,
        context_wpid="wpid_from_context",
    )
    # Both lookup and store should receive the context wpid as the first positional arg.
    assert captured["lookup_calls"], "lookup_cross_run_extraction_cache was not awaited"
    assert captured["lookup_calls"][0].args[0] == "wpid_from_context"
    assert captured["store_calls"], "store_cross_run_extraction_cache was not awaited"
    assert captured["store_calls"][0].args[0] == "wpid_from_context"


def test_cache_calls_prefer_task_wpid_when_both_are_set(monkeypatch) -> None:
    # Belt-and-suspenders: if a caller ever does populate task.workflow_permanent_id,
    # the task value wins (matches the ``a or b`` precedence).
    captured = _run_extract_information_and_capture_cache_calls(
        monkeypatch,
        task_wpid="wpid_from_task",
        context_wpid="wpid_from_context",
    )
    assert captured["lookup_calls"][0].args[0] == "wpid_from_task"
    assert captured["store_calls"][0].args[0] == "wpid_from_task"
