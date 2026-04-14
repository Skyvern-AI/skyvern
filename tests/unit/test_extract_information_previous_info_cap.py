"""Tests for previous_extracted_information capping (SKY-8920 Phase B + D)."""

from __future__ import annotations


def _make_scraped_page(refreshed_extracted_text: str = "small"):
    from unittest.mock import AsyncMock, MagicMock

    refreshed = MagicMock()
    refreshed.extracted_text = refreshed_extracted_text
    refreshed.url = "https://example.test"
    refreshed.screenshots = []
    refreshed.build_element_tree = MagicMock(return_value="<a>link</a>")
    refreshed.support_economy_elements_tree = MagicMock(return_value=False)

    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []
    return scraped_page


def _capture_handler_kwargs(monkeypatch, previous_extracted_information):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    captured: dict = {}

    def fake_load_prompt_with_elements(**kwargs):
        captured.update(kwargs)
        return "rendered-prompt"

    async def fake_handler_call(**kwargs):
        captured["prompt"] = kwargs.get("prompt")
        return {}

    monkeypatch.setattr(handler, "load_prompt_with_elements", fake_load_prompt_with_elements)
    monkeypatch.setattr(handler, "ensure_context", lambda: MagicMock(tz_info=None))
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_handler_call,
    )
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: None)

    scraped_page = _make_scraped_page()

    task = MagicMock()
    task.navigation_goal = None
    task.navigation_payload = None
    task.extracted_information = previous_extracted_information
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = {"type": "object"}
    task.error_code_mapping = None
    task.llm_key = None
    task.workflow_run_id = None
    task.task_id = "tsk_test"
    task.include_extracted_text = True

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=MagicMock(), scraped_page=scraped_page))

    return captured


def test_handler_truncates_huge_previous_extracted_information(monkeypatch) -> None:
    import json

    from skyvern.utils.token_counter import count_tokens

    huge_prev = [{"iter": i, "blob": "x" * 2_000} for i in range(500)]

    captured = _capture_handler_kwargs(monkeypatch, previous_extracted_information=huge_prev)

    capped = captured["previous_extracted_information"]
    assert capped is not None
    assert isinstance(capped, list)
    # Recent iterations survive; early ones are dropped.
    assert capped[-1]["iter"] == 499
    assert capped[0]["iter"] != 0
    # Capped result fits inside the 20k-token budget.
    assert count_tokens(json.dumps(capped)) <= 20_500


def test_handler_passes_small_previous_extracted_information_unchanged(monkeypatch) -> None:
    small_prev = [{"iter": 0, "blob": "small"}]
    captured = _capture_handler_kwargs(monkeypatch, previous_extracted_information=small_prev)
    assert captured["previous_extracted_information"] == small_prev


def _capture_handler_schema(monkeypatch, extracted_information_schema):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    captured: dict = {}

    def fake_load_prompt_with_elements(**kwargs):
        captured.update(kwargs)
        return "rendered-prompt"

    async def fake_handler_call(**kwargs):
        captured["prompt"] = kwargs.get("prompt")
        return {}

    monkeypatch.setattr(handler, "load_prompt_with_elements", fake_load_prompt_with_elements)
    monkeypatch.setattr(handler, "ensure_context", lambda: MagicMock(tz_info=None))
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_handler_call,
    )
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: None)

    scraped_page = _make_scraped_page()

    task = MagicMock()
    task.navigation_goal = None
    task.navigation_payload = None
    task.extracted_information = None
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = extracted_information_schema
    task.error_code_mapping = None
    task.llm_key = None
    task.workflow_run_id = None
    task.task_id = "tsk_test"
    task.include_extracted_text = True

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=MagicMock(), scraped_page=scraped_page))

    return captured


def test_handler_caps_huge_extraction_schema(monkeypatch) -> None:
    huge_schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string", "description": "lorem ipsum " * 40} for i in range(1000)},
    }

    captured = _capture_handler_schema(monkeypatch, huge_schema)

    schema_passed = captured["extracted_information_schema"]
    assert isinstance(schema_passed, dict)
    assert schema_passed.get("_skyvern_schema_truncated") is True


def test_handler_passes_small_schema_unchanged(monkeypatch) -> None:
    small_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    captured = _capture_handler_schema(monkeypatch, small_schema)

    assert captured["extracted_information_schema"] == small_schema
