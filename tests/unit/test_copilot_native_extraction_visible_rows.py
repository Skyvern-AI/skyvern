"""Regression coverage for SKY-15259's native extraction producer boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

VISIBLE_RESULT_ROWS = [
    {
        "record_id": "result-001",
        "name": "Sanitized visible result",
        "status": "ready",
    }
]

REQUESTED_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "name": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        }
    },
    "required": ["records"],
}


def _run_native_extraction(monkeypatch, llm_response: object, *, schema: dict = REQUESTED_RECORD_SCHEMA):
    from skyvern.webeye.actions import handler

    refreshed_page = MagicMock()
    refreshed_page.extracted_text = ""
    refreshed_page.url = "https://example.test/results"
    refreshed_page.build_element_tree.return_value = (
        "<table><tr><td>Sanitized visible result</td><td>ready</td></tr></table>"
    )

    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed_page)
    scraped_page.screenshots = []

    task = SimpleNamespace(
        navigation_goal=None,
        navigation_payload=None,
        extracted_information=None,
        data_extraction_goal="Extract the visible result rows.",
        extracted_information_schema=schema,
        error_code_mapping=None,
        llm_key=None,
        workflow_run_id=None,
        task_id="tsk_sky_15259",
        organization_id="",
        workflow_permanent_id=None,
        workflow_system_prompt=None,
        include_extracted_text=False,
    )

    monkeypatch.setattr(handler, "ensure_context", lambda: SimpleNamespace(tz_info=None, workflow_permanent_id=None))
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "collect_virtualized_grid_rows", AsyncMock(return_value=None))
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: None)
    monkeypatch.setattr(
        handler,
        "load_prompt_with_elements_tracked",
        lambda **kwargs: (
            "sanitized native extraction prompt",
            {
                "extracted_text": kwargs["extracted_text"],
                "virtualized_grid_rows": kwargs["virtualized_grid_rows"],
                "previous_extracted_information": kwargs["previous_extracted_information"],
                "extracted_information_schema": kwargs["extracted_information_schema"],
            },
        ),
    )

    async def extraction_llm(**_: object) -> object:
        return llm_response

    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda _llm_key, default: extraction_llm,
    )

    return asyncio.run(
        handler.extract_information_for_navigation_goal(
            task=task,
            step=SimpleNamespace(retry_index=0),
            scraped_page=scraped_page,
            page=MagicMock(),
        )
    )


def test_native_extraction_does_not_replace_visible_row_response_with_empty_schema_default(monkeypatch) -> None:
    """A populated producer response must not be laundered into ``{"records": []}``."""
    result = _run_native_extraction(monkeypatch, VISIBLE_RESULT_ROWS)

    assert result.scraped_data == VISIBLE_RESULT_ROWS


def test_native_extraction_keeps_matching_empty_table_response_empty(monkeypatch) -> None:
    result = _run_native_extraction(monkeypatch, {"records": []})

    assert result.scraped_data == {"records": []}


def test_native_extraction_still_fills_missing_fields_for_matching_response(monkeypatch) -> None:
    schema = {
        "type": "object",
        "properties": {
            "records": {"type": "array"},
            "report_title": {"type": "string"},
        },
        "required": ["records", "report_title"],
    }

    result = _run_native_extraction(monkeypatch, {"records": []}, schema=schema)

    assert result.scraped_data == {"records": [], "report_title": None}


def test_shadow_extraction_uses_the_same_shape_guard_as_the_native_miss_path(monkeypatch) -> None:
    from skyvern.webeye.actions import handler

    scheduled: dict[str, object] = {}
    monkeypatch.setattr(
        handler.extraction_shadow,
        "schedule_shadow_check",
        lambda **kwargs: scheduled.update(kwargs),
    )

    async def extraction_llm(**_: object) -> object:
        return VISIBLE_RESULT_ROWS

    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda _llm_key, default: extraction_llm,
    )

    task = SimpleNamespace(extracted_information_schema=REQUESTED_RECORD_SCHEMA, workflow_system_prompt=None)
    handler._schedule_extraction_shadow_check_for_hit(
        task=task,
        workflow_run_id="wr_sky_15259",
        cache_key="cache-key",
        cached_value=VISIBLE_RESULT_ROWS,
        cached_age_seconds=1.0,
        scraped_page=SimpleNamespace(screenshots=[]),
        llm_key_override=None,
        extract_information_prompt="sanitized native extraction prompt",
    )

    assert asyncio.run(scheduled["llm_call"]()) == VISIBLE_RESULT_ROWS
