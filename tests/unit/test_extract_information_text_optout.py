"""Tests for the include_extracted_text opt-out chain (SKY-8920 Phase A) and the
neutral virtualized-grid-collection seam the extraction handler drives."""

from __future__ import annotations

import inspect

_GRID_ROWS = 'Total rows: 2 (complete)\n[{"col0":"first"},{"col0":"second"}]'


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


def test_extract_information_requires_keyword_only_page() -> None:
    from playwright.async_api import Page

    from skyvern.webeye.actions import handler

    parameter = inspect.signature(handler.extract_information_for_navigation_goal).parameters["page"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is Page


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
    task.organization_id = ""
    task.workflow_permanent_id = None
    task.workflow_system_prompt = None
    task.include_extracted_text = include_extracted_text
    return task


def _stub_downstream_extraction(monkeypatch) -> dict:
    """Stub the heavy handler dependencies (prompt render, LLM, cache, context,
    LOG) so a test can drive the extraction path and observe the rendered-prompt
    kwargs and LLM call count without external services."""
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    captured: dict = {}
    log = MagicMock()
    monkeypatch.setattr(handler, "LOG", log)
    captured["log"] = log

    def fake_load_prompt_with_elements_tracked(**kwargs):
        # Drop the non-kwargs args (element_tree_builder, prompt_engine, etc.)
        # so captured reflects the rendered-prompt template vars only.
        captured.update(
            {
                k: v
                for k, v in kwargs.items()
                if k not in {"element_tree_builder", "prompt_engine", "template_name", "html_need_skyvern_attrs"}
            }
        )
        return "rendered-prompt", dict(captured)

    async def fake_handler_call(**kwargs):
        captured["prompt"] = kwargs.get("prompt")
        captured["llm_calls"] = captured.get("llm_calls", 0) + 1
        return {}

    monkeypatch.setattr(handler, "load_prompt_with_elements_tracked", fake_load_prompt_with_elements_tracked)
    monkeypatch.setattr(handler, "ensure_context", lambda: MagicMock(tz_info=None, workflow_permanent_id=None))
    monkeypatch.setattr(handler.service_utils, "is_cua_task", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handler.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_handler_call,
    )
    # Short-circuit the extraction_cache so we always fall through to the LLM path.
    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", lambda **_: None)
    return captured


def _run_extract_information(
    monkeypatch,
    *,
    include_extracted_text: bool = False,
    page=None,
    seam_return: str | None = None,
    seam_error: Exception | None = None,
):
    """Run the handler with the grid-collection seam
    (``app.AGENT_FUNCTION.collect_virtualized_grid_rows``) patched to return a
    canned serialized string, return None, or raise — and capture what reached
    ``load_prompt_with_elements``."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    if page is None:
        page = MagicMock()

    captured = _stub_downstream_extraction(monkeypatch)

    if seam_error is not None:
        seam = AsyncMock(side_effect=seam_error)
    else:
        seam = AsyncMock(return_value=seam_return)
    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "collect_virtualized_grid_rows", seam)
    captured["seam"] = seam

    refreshed = _make_scraped_page_refreshed("PROHIBITED_TEXT_MARKER")
    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []

    task = _make_task_for_extract_information(include_extracted_text=include_extracted_text)

    asyncio.run(
        handler.extract_information_for_navigation_goal(
            task=task,
            step=MagicMock(retry_index=0),
            scraped_page=scraped_page,
            page=page,
        )
    )

    return captured


def test_handle_extract_action_forwards_real_page_into_collection(monkeypatch) -> None:
    """Production-call-path regression: handle_extract_action must forward the
    real page through extract_information_for_navigation_goal into the grid
    seam, which receives that exact page (and task) object — not a copy."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    _stub_downstream_extraction(monkeypatch)

    captured_calls: list[dict] = []

    async def spy_collect(*, task, page):
        captured_calls.append({"task": task, "page": page})
        return None

    monkeypatch.setattr(handler.app.AGENT_FUNCTION, "collect_virtualized_grid_rows", spy_collect)

    sentinel_page = MagicMock(name="real_page")
    refreshed = _make_scraped_page_refreshed("text")
    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []

    task = _make_task_for_extract_information(include_extracted_text=False)
    step = MagicMock(retry_index=0)
    action = MagicMock()

    result = asyncio.run(handler.handle_extract_action(action, sentinel_page, scraped_page, task, step))

    assert len(captured_calls) == 1
    assert captured_calls[0]["page"] is sentinel_page
    assert captured_calls[0]["task"] is task
    assert isinstance(result, list) and result


def test_handler_omits_extracted_text_when_task_flag_is_false(monkeypatch) -> None:
    captured = _run_extract_information(monkeypatch, include_extracted_text=False)
    assert captured["extracted_text"] is None


def test_handler_passes_extracted_text_when_task_flag_is_true(monkeypatch) -> None:
    captured = _run_extract_information(monkeypatch, include_extracted_text=True)
    assert captured["extracted_text"] == "PROHIBITED_TEXT_MARKER"


def test_handler_injects_serialized_grid_rows_and_keeps_one_llm_call(monkeypatch) -> None:
    captured = _run_extract_information(monkeypatch, seam_return=_GRID_ROWS)

    assert captured["virtualized_grid_rows"] == _GRID_ROWS
    assert captured["llm_calls"] == 1
    captured["seam"].assert_awaited_once()


def test_handler_omits_grid_rows_when_seam_returns_none(monkeypatch) -> None:
    captured = _run_extract_information(monkeypatch, seam_return=None)

    assert captured["virtualized_grid_rows"] is None
    assert captured["llm_calls"] == 1


def test_handler_preserves_extraction_when_seam_raises(monkeypatch) -> None:
    captured = _run_extract_information(monkeypatch, seam_error=RuntimeError("collector boom"))

    assert captured["virtualized_grid_rows"] is None
    assert captured["llm_calls"] == 1
    warned = [call.args[0] for call in captured["log"].warning.call_args_list]
    assert "virtualized_grid_collection_failed" in warned


def test_handler_warns_when_grid_rows_dropped_after_prompt_ceiling(monkeypatch) -> None:
    """The prompt ceiling may drop the optional grid rows (they are large). When
    the pre-render value was set but the post-ceiling value is None, the handler
    must emit the neutral drop warning."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    captured = _stub_downstream_extraction(monkeypatch)

    def drop_grid_rows_post_ceiling(**kwargs):
        post = {
            k: v
            for k, v in kwargs.items()
            if k not in {"element_tree_builder", "prompt_engine", "template_name", "html_need_skyvern_attrs"}
        }
        captured.update(post)
        post["virtualized_grid_rows"] = None
        return "rendered-prompt", post

    monkeypatch.setattr(handler, "load_prompt_with_elements_tracked", drop_grid_rows_post_ceiling)
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "collect_virtualized_grid_rows",
        AsyncMock(return_value=_GRID_ROWS),
    )

    refreshed = _make_scraped_page_refreshed("text")
    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []

    asyncio.run(
        handler.extract_information_for_navigation_goal(
            task=_make_task_for_extract_information(include_extracted_text=False),
            step=MagicMock(retry_index=0),
            scraped_page=scraped_page,
            page=MagicMock(),
        )
    )

    warned = [call.args[0] for call in captured["log"].warning.call_args_list]
    assert "virtualized_grid_rows_dropped_from_prompt" in warned


def test_handler_passes_post_ceiling_grid_rows_into_cache_key(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from skyvern.webeye.actions import handler

    _stub_downstream_extraction(monkeypatch)

    cache_kwargs: dict = {}

    def capture_compute_cache_key(**kwargs):
        cache_kwargs.update(kwargs)
        return None

    monkeypatch.setattr(handler.extraction_cache, "compute_cache_key", capture_compute_cache_key)
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "collect_virtualized_grid_rows",
        AsyncMock(return_value=_GRID_ROWS),
    )

    refreshed = _make_scraped_page_refreshed("text")
    scraped_page = MagicMock()
    scraped_page.refresh = AsyncMock(return_value=refreshed)
    scraped_page.screenshots = []

    asyncio.run(
        handler.extract_information_for_navigation_goal(
            task=_make_task_for_extract_information(include_extracted_text=False),
            step=MagicMock(retry_index=0),
            scraped_page=scraped_page,
            page=MagicMock(),
        )
    )

    assert cache_kwargs["virtualized_grid_rows"] == _GRID_ROWS


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
        "virtualized_grid_rows": None,
    }
    base_kwargs.update(kwargs)
    return prompt_engine.load_prompt("extract-information", **base_kwargs)


def test_virtualized_grid_rows_render_inside_untrusted_fence() -> None:
    marker = 'Total rows: 1 (complete)\n[{"col0":"GRID_MARKER"}]'

    rendered = _render_extract_information(virtualized_grid_rows=marker)

    assert rendered.index("BEGIN_UNTRUSTED_WEB_PAGE_DATA") < rendered.index("GRID_MARKER")
    assert rendered.index("GRID_MARKER") < rendered.index("END_UNTRUSTED_WEB_PAGE_DATA")


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

    def fake_load_prompt_with_elements_tracked(**kwargs):
        captured.update(
            {
                k: v
                for k, v in kwargs.items()
                if k not in {"element_tree_builder", "prompt_engine", "template_name", "html_need_skyvern_attrs"}
            }
        )
        return "rendered-prompt", dict(captured)

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

    async def fake_handler(*, prompt, step, screenshots, prompt_name, force_dict, **_ignored):
        return {}

    monkeypatch.setattr(module, "load_prompt_with_elements_tracked", fake_load_prompt_with_elements_tracked)
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

    def fake_load_prompt_with_elements_tracked(**kwargs):
        captured.update(
            {
                k: v
                for k, v in kwargs.items()
                if k not in {"element_tree_builder", "prompt_engine", "template_name", "html_need_skyvern_attrs"}
            }
        )
        return "rendered-prompt", dict(captured)

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

    async def fake_handler(*, prompt, step, screenshots, prompt_name, force_dict, **_ignored):
        return {}

    monkeypatch.setattr(module, "load_prompt_with_elements_tracked", fake_load_prompt_with_elements_tracked)
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
