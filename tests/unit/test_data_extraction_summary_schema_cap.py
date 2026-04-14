"""Tests for the extraction-schema cap at the data-extraction-summary call site (SKY-8920 Phase D)."""

from __future__ import annotations


def _run_create_extract_action(monkeypatch, extracted_information_schema):
    import asyncio
    from unittest.mock import MagicMock

    from skyvern.forge import agent as agent_module

    captured: dict = {}

    original_load_prompt = agent_module.prompt_engine.load_prompt

    def capturing_load_prompt(template_name, **kwargs):
        if template_name == "data-extraction-summary":
            captured.update(kwargs)
        return original_load_prompt(template_name, **kwargs)

    async def fake_handler(*, prompt, step, prompt_name):
        captured["prompt"] = prompt
        return {"summary": "ok"}

    monkeypatch.setattr(agent_module.prompt_engine, "load_prompt", capturing_load_prompt)
    monkeypatch.setattr(agent_module.app, "EXTRACTION_LLM_API_HANDLER", fake_handler)
    monkeypatch.setattr(
        agent_module.skyvern_context,
        "ensure_context",
        lambda: MagicMock(tz_info=None, workflow_run_id="wr_test"),
    )
    monkeypatch.setattr(
        agent_module.extraction_cache,
        "compute_cache_key",
        lambda **_: None,
    )
    monkeypatch.setattr(
        agent_module.extraction_cache,
        "lookup",
        lambda *a, **k: None,
    )

    task = MagicMock()
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = extracted_information_schema
    task.task_id = "tsk_test"
    task.workflow_run_id = "wr_test"
    task.organization_id = "o_test"

    step = MagicMock(step_id="stp_test", order=0)
    scraped_page = MagicMock(url="https://example.test")
    # Avoid attribute errors from AsyncMock
    step.step_id = "stp_test"
    step.order = 0

    asyncio.run(agent_module.ForgeAgent.create_extract_action(task=task, step=step, scraped_page=scraped_page))
    return captured


def test_create_extract_action_caps_huge_schema(monkeypatch) -> None:
    huge_schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string", "description": "lorem ipsum " * 40} for i in range(1000)},
    }

    captured = _run_create_extract_action(monkeypatch, huge_schema)

    schema_passed = captured["data_extraction_schema"]
    assert isinstance(schema_passed, dict)
    assert schema_passed.get("_skyvern_schema_truncated") is True
    assert schema_passed.get("type") == "object"


def test_create_extract_action_passes_small_schema_unchanged(monkeypatch) -> None:
    small_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    captured = _run_create_extract_action(monkeypatch, small_schema)

    assert captured["data_extraction_schema"] == small_schema
