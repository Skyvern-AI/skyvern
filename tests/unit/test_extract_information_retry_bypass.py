"""Handler-level test for the extract-information retry self-heal path (SKY-8873).

Covers the cache-bypass decision: when ``step.retry_index > 1`` the handler
must NOT consult the in-run cache and MUST evict the matching key before
the fresh LLM call, so the dual-write after extraction overwrites the
suspect prior entry rather than accumulating alongside it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from skyvern.forge.sdk.cache import extraction_cache
from skyvern.webeye.actions import handler


def _make_scraped_page():
    refreshed = MagicMock()
    refreshed.extracted_text = "page text"
    refreshed.url = "https://example.test"
    refreshed.screenshots = []
    refreshed.build_element_tree = MagicMock(return_value="<a>link</a>")
    refreshed.support_economy_elements_tree = MagicMock(return_value=False)
    refreshed.last_used_element_tree_html = None

    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []
    return scraped_page


def _make_task(workflow_run_id: str, workflow_permanent_id: str | None = None):
    task = MagicMock()
    task.navigation_goal = None
    task.navigation_payload = None
    task.extracted_information = None
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = {"type": "object"}
    task.error_code_mapping = None
    task.llm_key = None
    task.workflow_run_id = workflow_run_id
    task.task_id = "tsk_test"
    task.workflow_permanent_id = workflow_permanent_id
    task.include_extracted_text = True
    return task


def _stub_handler_dependencies(monkeypatch, llm_call_counter: list[int], synthetic_cache_key: str):
    """Patch out the heavy handler deps and force ``compute_cache_key`` to
    return a deterministic value so the test can pre-populate the cache at
    that key and later assert eviction.

    Returns the cross-run lookup / store mocks so tests can assert call
    counts and arguments on the dual-write path.
    """

    async def fake_llm(**_kwargs):
        llm_call_counter.append(1)
        return {"docs": ["fresh.pdf"]}

    monkeypatch.setattr(
        handler,
        "load_prompt_with_elements_tracked",
        lambda **kwargs: ("rendered-prompt", dict(kwargs)),
    )
    monkeypatch.setattr(handler, "ensure_context", lambda: MagicMock(tz_info=None))
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_llm,
    )
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: synthetic_cache_key)
    # Shadow mode would fire a background LLM call on in-run cache hits and
    # pollute the counter — not the behavior under test.
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=False),
    )
    # Cross-run tier hooks — the lookup returns None so the test targets the
    # in-run / retry-bypass decision, not the cross-run hit path. The store
    # mock is handed back so tests can assert the dual-write fired on the
    # self-heal path.
    lookup_mock = AsyncMock(return_value=None)
    store_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "lookup_cross_run_extraction_cache", lookup_mock)
    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "store_cross_run_extraction_cache", store_mock)
    return lookup_mock, store_mock


def test_retry_index_gt_one_evicts_in_run_entry_and_calls_llm(monkeypatch) -> None:
    """On retry #2, the handler must drop the prior cached value, re-run the LLM,
    and dual-write both tiers so the cross-run Redis entry is overwritten."""
    extraction_cache._reset_for_tests()
    workflow_run_id = "wfr_retry_self_heal"
    workflow_permanent_id = "wpid_retry_self_heal"
    cache_key = "synthetic_cache_key_retry"

    # Prime the in-run cache with a stale/bad value — the retry bypass must
    # drop it. If the guard fires correctly, a post-call lookup returns miss.
    extraction_cache.store(workflow_run_id, cache_key, {"docs": ["stale.pdf"]})
    assert extraction_cache.lookup(workflow_run_id, cache_key).hit is True

    llm_calls: list[int] = []
    lookup_mock, store_mock = _stub_handler_dependencies(monkeypatch, llm_calls, cache_key)

    scraped_page = _make_scraped_page()
    task = _make_task(workflow_run_id, workflow_permanent_id=workflow_permanent_id)
    # retry_index = 2 → past the bypass threshold (> 1)
    step = MagicMock(step_id="stp_retry2", retry_index=2)

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page))

    # LLM must have been called (cache was bypassed, not consumed).
    assert llm_calls == [1], "retry bypass must force a fresh LLM call"
    # Post-call lookup returns the freshly-stored value, not the stale one —
    # the in-run side of the dual-write overwrote the evicted entry.
    after = extraction_cache.lookup(workflow_run_id, cache_key)
    assert after.hit is True
    assert after.value == {"docs": ["fresh.pdf"]}
    # Cross-run side of the dual-write: the Redis store hook must have been
    # called with the fresh value so the suspect Redis entry gets overwritten
    # at the same wpid/cache_key pair. This is the self-heal path's whole point.
    lookup_mock.assert_not_called()  # retry bypass must not consult cross-run tier
    store_mock.assert_awaited_once_with(workflow_permanent_id, cache_key, {"docs": ["fresh.pdf"]})
    extraction_cache._reset_for_tests()


def test_retry_index_one_still_uses_cache(monkeypatch) -> None:
    """Retry #1 is below the bypass threshold — the cache must still serve."""
    extraction_cache._reset_for_tests()
    workflow_run_id = "wfr_retry_first"
    cache_key = "synthetic_cache_key_first_retry"

    extraction_cache.store(workflow_run_id, cache_key, {"docs": ["cached.pdf"]})

    llm_calls: list[int] = []
    _stub_handler_dependencies(monkeypatch, llm_calls, cache_key)

    scraped_page = _make_scraped_page()
    task = _make_task(workflow_run_id)
    # retry_index = 1 — still below the bypass threshold
    step = MagicMock(step_id="stp_retry1", retry_index=1)

    result = asyncio.run(
        handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page)
    )

    # Cache hit path — no LLM call.
    assert llm_calls == [], "retry #1 must reuse the cached value (bypass threshold is retry_index > 1)"
    assert result.scraped_data == {"docs": ["cached.pdf"]}
    extraction_cache._reset_for_tests()
