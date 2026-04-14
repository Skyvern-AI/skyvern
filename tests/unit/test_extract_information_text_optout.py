"""Tests for the include_extracted_text opt-out chain (SKY-8920 Phase A)."""

from __future__ import annotations


def test_task_base_has_include_extracted_text_field_with_default_true() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskBase

    assert "include_extracted_text" in TaskBase.model_fields
    field = TaskBase.model_fields["include_extracted_text"]
    assert field.default is True


def test_task_base_accepts_include_extracted_text_false() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskBase

    task = TaskBase(url="https://example.test", include_extracted_text=False)
    assert task.include_extracted_text is False


def test_task_base_defaults_include_extracted_text_true() -> None:
    from skyvern.forge.sdk.schemas.tasks import TaskBase

    task = TaskBase(url="https://example.test")
    assert task.include_extracted_text is True


def test_base_task_block_has_include_extracted_text_field_default_true() -> None:
    from skyvern.forge.sdk.workflow.models.block import BaseTaskBlock

    assert "include_extracted_text" in BaseTaskBlock.model_fields
    assert BaseTaskBlock.model_fields["include_extracted_text"].default is True


def test_extraction_block_overrides_include_extracted_text_to_false() -> None:
    from skyvern.forge.sdk.workflow.models.block import ExtractionBlock

    assert "include_extracted_text" in ExtractionBlock.model_fields
    assert ExtractionBlock.model_fields["include_extracted_text"].default is False


def _make_scraped_page_refreshed(extracted_text: str):
    from unittest.mock import MagicMock

    refreshed = MagicMock()
    refreshed.extracted_text = extracted_text
    refreshed.url = "https://example.test"
    refreshed.screenshots = []
    refreshed.build_element_tree = MagicMock(return_value="<a href='/d.pdf'>Doc</a>")
    refreshed.support_economy_elements_tree = MagicMock(return_value=False)
    return refreshed


def _make_task_for_extract_information(include_extracted_text: bool):
    from unittest.mock import MagicMock

    task = MagicMock()
    task.navigation_goal = None
    task.navigation_payload = None
    task.extracted_information = None
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = {"type": "object"}
    task.error_code_mapping = None
    task.llm_key = None
    task.workflow_run_id = None
    task.task_id = "tsk_test"
    task.include_extracted_text = include_extracted_text
    return task


def _capture_extract_information_kwargs(monkeypatch, include_extracted_text: bool):
    """Run the handler with monkeypatches that capture what's passed to load_prompt_with_elements."""
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

    # The handler calls compute_cache_key (may raise), LOG, LLMAPIHandlerFactory,
    # service_utils.is_cua_task. Monkey-patch just enough to reach load_prompt_with_elements
    # and the handler call.
    monkeypatch.setattr(handler, "load_prompt_with_elements", fake_load_prompt_with_elements)
    monkeypatch.setattr(handler, "ensure_context", lambda: MagicMock(tz_info=None))
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_handler_call,
    )
    # Short-circuit the extraction_cache so we always fall through to the LLM path.
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: None)

    refreshed = _make_scraped_page_refreshed("PROHIBITED_TEXT_MARKER")
    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []

    task = _make_task_for_extract_information(include_extracted_text=include_extracted_text)

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=MagicMock(), scraped_page=scraped_page))

    return captured


def test_handler_omits_extracted_text_when_task_flag_is_false(monkeypatch) -> None:
    captured = _capture_extract_information_kwargs(monkeypatch, include_extracted_text=False)
    assert captured["extracted_text"] is None


def test_handler_passes_extracted_text_when_task_flag_is_true(monkeypatch) -> None:
    captured = _capture_extract_information_kwargs(monkeypatch, include_extracted_text=True)
    assert captured["extracted_text"] == "PROHIBITED_TEXT_MARKER"


def _render_extract_information(**kwargs) -> str:
    from skyvern.forge.prompts import prompt_engine

    base_kwargs = {
        "data_extraction_goal": "Extract documents",
        "extracted_information_schema": {"type": "object"},
        "current_url": "https://example.test",
        "elements": "<a>link</a>",
        "extracted_text": None,
        "error_code_mapping_str": None,
        "navigation_payload": None,
        "previous_extracted_information": None,
        "local_datetime": "2026-04-14T12:00:00",
    }
    base_kwargs.update(kwargs)
    return prompt_engine.load_prompt("extract-information", **base_kwargs)


def test_extract_information_template_omits_text_line_when_extracted_text_is_none() -> None:
    rendered = _render_extract_information(extracted_text=None)
    assert "Text extracted from the webpage" not in rendered


def test_extract_information_template_includes_text_line_when_extracted_text_is_set() -> None:
    rendered = _render_extract_information(extracted_text="RENDERED_MARKER")
    assert "RENDERED_MARKER" in rendered
    assert "Text extracted from the webpage: RENDERED_MARKER" in rendered


def _capture_ai_extract_kwargs(monkeypatch, include_extracted_text: bool):
    """Run RealSkyvernPageAi.ai_extract with monkeypatches that capture the kwargs passed
    to load_prompt_with_elements."""
    import asyncio
    from unittest.mock import MagicMock

    from skyvern.core.script_generations import real_skyvern_page_ai as module

    captured: dict = {}

    def fake_load_prompt_with_elements(**kwargs):
        captured.update(kwargs)
        return "rendered-prompt"

    scraped_page = MagicMock()
    scraped_page.url = "https://example.test"
    scraped_page.extracted_text = "PROHIBITED_MARKER"
    scraped_page.screenshots = []
    scraped_page.build_element_tree = MagicMock(return_value="<a>link</a>")
    scraped_page.support_economy_elements_tree = MagicMock(return_value=False)

    page = module.RealSkyvernPageAi.__new__(module.RealSkyvernPageAi)
    page.scraped_page = scraped_page
    page.current_label = None

    async def fake_refresh(*_args, **_kwargs):
        return None

    async def fake_handler(*, prompt, step, screenshots, prompt_name, force_dict):
        return {}

    monkeypatch.setattr(module, "load_prompt_with_elements", fake_load_prompt_with_elements)
    monkeypatch.setattr(module.app, "EXTRACTION_LLM_API_HANDLER", fake_handler)
    monkeypatch.setattr(module.extraction_cache, "compute_cache_key", lambda **_: None)
    monkeypatch.setattr(page, "_refresh_scraped_page", fake_refresh)
    monkeypatch.setattr(module.skyvern_context, "current", lambda: None)

    asyncio.run(
        page.ai_extract(
            prompt="Extract documents",
            schema={"type": "object"},
            include_extracted_text=include_extracted_text,
        )
    )

    return captured


def test_ai_extract_omits_extracted_text_when_flag_is_false(monkeypatch) -> None:
    captured = _capture_ai_extract_kwargs(monkeypatch, include_extracted_text=False)
    assert captured["extracted_text"] is None


def test_ai_extract_passes_extracted_text_when_flag_is_true(monkeypatch) -> None:
    captured = _capture_ai_extract_kwargs(monkeypatch, include_extracted_text=True)
    assert captured["extracted_text"] == "PROHIBITED_MARKER"


def _capture_ai_extract_kwargs_with_schema(monkeypatch, schema):
    import asyncio
    from unittest.mock import MagicMock

    from skyvern.core.script_generations import real_skyvern_page_ai as module

    captured: dict = {}

    def fake_load_prompt_with_elements(**kwargs):
        captured.update(kwargs)
        return "rendered-prompt"

    scraped_page = MagicMock()
    scraped_page.url = "https://example.test"
    scraped_page.extracted_text = "TXT"
    scraped_page.screenshots = []
    scraped_page.build_element_tree = MagicMock(return_value="<a>link</a>")
    scraped_page.support_economy_elements_tree = MagicMock(return_value=False)

    page = module.RealSkyvernPageAi.__new__(module.RealSkyvernPageAi)
    page.scraped_page = scraped_page
    page.current_label = None

    async def fake_refresh(*_args, **_kwargs):
        return None

    async def fake_handler(*, prompt, step, screenshots, prompt_name, force_dict):
        return {}

    monkeypatch.setattr(module, "load_prompt_with_elements", fake_load_prompt_with_elements)
    monkeypatch.setattr(module.app, "EXTRACTION_LLM_API_HANDLER", fake_handler)
    monkeypatch.setattr(module.extraction_cache, "compute_cache_key", lambda **_: None)
    monkeypatch.setattr(page, "_refresh_scraped_page", fake_refresh)
    monkeypatch.setattr(module.skyvern_context, "current", lambda: None)

    asyncio.run(page.ai_extract(prompt="Extract documents", schema=schema, include_extracted_text=True))
    return captured


def test_ai_extract_caps_huge_schema(monkeypatch) -> None:
    big_props = {f"field_{i}": {"type": "string", "description": "x" * 200} for i in range(500)}
    huge_schema = {"type": "object", "properties": big_props}
    captured = _capture_ai_extract_kwargs_with_schema(monkeypatch, huge_schema)
    assert captured["extracted_information_schema"].get("_skyvern_schema_truncated") is True


def test_ai_extract_passes_small_schema_unchanged(monkeypatch) -> None:
    small_schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    captured = _capture_ai_extract_kwargs_with_schema(monkeypatch, small_schema)
    assert captured["extracted_information_schema"] == small_schema
