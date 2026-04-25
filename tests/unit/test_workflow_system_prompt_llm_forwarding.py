"""Regression tests: workflow-level workflow_system_prompt must reach the LLM handler.

Complements ``test_block_workflow_system_prompt_inheritance.py`` (which asserts
that the workflow prompt flows onto the block model). These tests assert the
next hop — that each block/function that makes an LLM call actually forwards
``system_prompt`` as a kwarg to the handler. Without this, inheritance passes
quietly but the prompt has no effect on output (the bug the user hit on the
extraction path).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from skyvern.forge.sdk.cache import extraction_cache
from skyvern.forge.sdk.workflow.models.block import FileParserBlock, PDFParserBlock, TextPromptBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import FileType

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="task1_output",
        description="test output",
        output_parameter_id="op_task1",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


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


def _make_task(*, system_prompt: str | None) -> MagicMock:
    task = MagicMock()
    task.navigation_goal = None
    task.navigation_payload = None
    task.extracted_information = None
    task.extracted_information_schema = {"type": "object"}
    task.data_extraction_goal = "Extract documents"
    task.error_code_mapping = None
    task.llm_key = None
    task.workflow_run_id = "wfr_sysprompt"
    task.task_id = "tsk_sysprompt"
    task.workflow_permanent_id = "wpid_sysprompt"
    task.organization_id = "o_sysprompt"
    task.include_extracted_text = True
    task.workflow_system_prompt = system_prompt
    return task


# ---------------------------------------------------------------------------
# extract-information handler (the OP's exact failure path)
# ---------------------------------------------------------------------------


def test_extract_information_forwards_system_prompt(monkeypatch) -> None:
    """Regression for the OP's bug: an ExtractionBlock whose Task carries a
    ``system_prompt`` (inherited from workflow.workflow_system_prompt) must
    forward it to the extract-information LLM call."""
    from skyvern.webeye.actions import handler

    extraction_cache._reset_for_tests()

    captured: dict = {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return {"quotes": "SHOUTED QUOTE"}

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
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "lookup_cross_run_extraction_cache",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "store_cross_run_extraction_cache",
        AsyncMock(return_value=None),
    )

    task = _make_task(system_prompt="Respond only in uppercase.")
    step = MagicMock(step_id="stp_sp", retry_index=0)
    scraped_page = _make_scraped_page()

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page))

    assert captured.get("system_prompt") == "Respond only in uppercase."
    extraction_cache._reset_for_tests()


def test_extract_information_passes_none_system_prompt_when_task_has_none(monkeypatch) -> None:
    """No workflow_system_prompt → task.workflow_system_prompt is None → handler receives None.
    Locks in that we don't accidentally invent a default or drop the kwarg."""
    from skyvern.webeye.actions import handler

    extraction_cache._reset_for_tests()

    captured: dict = {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return {"quotes": "x"}

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
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "lookup_cross_run_extraction_cache",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        handler.app.AGENT_FUNCTION,
        "store_cross_run_extraction_cache",
        AsyncMock(return_value=None),
    )

    task = _make_task(system_prompt=None)
    step = MagicMock(step_id="stp_none", retry_index=0)
    scraped_page = _make_scraped_page()

    asyncio.run(handler.extract_information_for_navigation_goal(task=task, step=step, scraped_page=scraped_page))

    assert "system_prompt" in captured
    assert captured["system_prompt"] is None
    extraction_cache._reset_for_tests()


# ---------------------------------------------------------------------------
# data-extraction-summary (agent.py path)
# ---------------------------------------------------------------------------


def test_data_extraction_summary_forwards_system_prompt(monkeypatch) -> None:
    from skyvern.forge import agent as agent_module

    captured: dict = {}

    async def fake_handler(**kwargs):
        captured.update(kwargs)
        return {"summary": "ok"}

    monkeypatch.setattr(agent_module.app, "EXTRACTION_LLM_API_HANDLER", fake_handler)
    monkeypatch.setattr(
        agent_module.skyvern_context,
        "ensure_context",
        lambda: MagicMock(tz_info=None, workflow_run_id="wr_sp"),
    )
    monkeypatch.setattr(agent_module.extraction_cache, "compute_cache_key", lambda **_: None)
    monkeypatch.setattr(agent_module.extraction_cache, "lookup", lambda *a, **k: None)

    task = _make_task(system_prompt="Answer in French.")
    step = MagicMock(step_id="stp_sp", order=0)
    scraped_page = MagicMock(url="https://example.test")

    asyncio.run(agent_module.ForgeAgent.create_extract_action(task=task, step=step, scraped_page=scraped_page))

    assert captured.get("system_prompt") == "Answer in French."


# ---------------------------------------------------------------------------
# Extraction cache key — system_prompt must be part of the key
# ---------------------------------------------------------------------------


def test_extraction_cache_key_changes_with_system_prompt() -> None:
    """Two calls that differ only in system_prompt must produce different
    digests — otherwise a user switching workflow_system_prompt mid-run would
    get stale output from a prior key's cached value."""
    base_kwargs: dict = dict(
        call_path="handler",
        element_tree="<a>link</a>",
        extracted_text="text",
        current_url="https://example.test",
        data_extraction_goal="Extract docs",
        extracted_information_schema={"type": "object"},
        navigation_payload=None,
        error_code_mapping=None,
        previous_extracted_information=None,
        llm_key=None,
    )

    key_no_sp = extraction_cache.compute_cache_key(**base_kwargs, workflow_system_prompt=None)
    key_a = extraction_cache.compute_cache_key(**base_kwargs, workflow_system_prompt="Answer in Spanish.")
    key_b = extraction_cache.compute_cache_key(**base_kwargs, workflow_system_prompt="Answer in French.")

    assert key_no_sp != key_a
    assert key_a != key_b
    # Same prompt → same key (determinism).
    key_a2 = extraction_cache.compute_cache_key(**base_kwargs, workflow_system_prompt="Answer in Spanish.")
    assert key_a == key_a2


# ---------------------------------------------------------------------------
# TextPromptBlock.send_prompt
# ---------------------------------------------------------------------------


def test_text_prompt_block_forwards_system_prompt(monkeypatch) -> None:
    from skyvern.forge.sdk.workflow.models import block as block_module

    captured: dict = {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return {"llm_response": "hi"}

    async def fake_default_handler(*args, **kwargs):
        return None

    async def fake_resolve(self, workflow_run_id, organization_id):
        return fake_default_handler

    monkeypatch.setattr(TextPromptBlock, "_resolve_default_llm_handler", fake_resolve)
    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_llm,
    )

    block = TextPromptBlock(
        label="p",
        output_parameter=_make_output_parameter(),
        prompt="What is the meaning of life?",
        workflow_system_prompt="Respond as Shakespeare.",
    )

    asyncio.run(
        block.send_prompt(
            prompt="What is the meaning of life?",
            parameter_values={},
            workflow_run_id="wfr_sp",
            organization_id="o_sp",
            workflow_run_block_id=None,
        )
    )

    assert captured.get("system_prompt") == "Respond as Shakespeare."


# ---------------------------------------------------------------------------
# FileParserBlock._extract_with_ai
# ---------------------------------------------------------------------------


def test_file_parser_block_forwards_system_prompt(monkeypatch) -> None:
    from skyvern.forge.sdk.workflow.models import block as block_module

    captured: dict = {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return {"output": {}}

    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, default: fake_llm,
    )

    block = FileParserBlock(
        label="fp",
        output_parameter=_make_output_parameter(),
        file_url="https://example.com/file.csv",
        file_type=FileType.CSV,
        workflow_system_prompt="Parse as JSON only.",
    )

    asyncio.run(block._extract_with_ai("hello,world\n1,2", workflow_run_context=MagicMock()))

    assert captured.get("system_prompt") == "Parse as JSON only."


# ---------------------------------------------------------------------------
# PDFParserBlock.execute — system_prompt forwarded to LLM call
# ---------------------------------------------------------------------------


def test_pdf_parser_block_forwards_system_prompt(monkeypatch) -> None:
    from skyvern.forge.sdk.workflow.models import block as block_module

    captured: dict = {}

    async def fake_llm(**kwargs):
        captured.update(kwargs)
        return {"output": {}}

    # Patch the app-level handler directly since PDFParserBlock uses app.LLM_API_HANDLER.
    monkeypatch.setattr(block_module.app, "LLM_API_HANDLER", fake_llm)
    monkeypatch.setattr(
        block_module,
        "download_file",
        AsyncMock(return_value="/tmp/file.pdf"),
    )
    monkeypatch.setattr(block_module, "extract_pdf_file", lambda *a, **k: "extracted text")

    workflow_run_context = MagicMock()
    workflow_run_context.has_parameter = MagicMock(return_value=False)
    workflow_run_context.has_value = MagicMock(return_value=False)
    workflow_run_context.workflow = None
    workflow_run_context.resolve_effective_workflow_system_prompt = MagicMock(return_value=None)

    monkeypatch.setattr(
        PDFParserBlock,
        "get_workflow_run_context",
        staticmethod(lambda workflow_run_id: workflow_run_context),
    )
    monkeypatch.setattr(
        PDFParserBlock,
        "record_output_parameter_value",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        PDFParserBlock,
        "build_block_result",
        AsyncMock(return_value=MagicMock()),
    )

    block = PDFParserBlock(
        label="pdf",
        output_parameter=_make_output_parameter(),
        file_url="https://example.com/file.pdf",
        workflow_system_prompt="Summarize in one sentence.",
    )

    asyncio.run(
        block.execute(
            workflow_run_id="wfr_sp_pdf",
            workflow_run_block_id="wrb_sp_pdf",
            organization_id="o_sp_pdf",
        )
    )

    assert captured.get("system_prompt") == "Summarize in one sentence."
