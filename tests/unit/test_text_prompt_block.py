import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from skyvern.config import settings as base_settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.models.block import TextPromptBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType

block_module = sys.modules["skyvern.forge.sdk.workflow.models.block"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "expected_llm_key"),
    [
        ("gemini-2.5-flash", "VERTEX_GEMINI_2.5_FLASH"),
        ("gemini-3-pro-preview", "VERTEX_GEMINI_3_PRO"),
    ],
)
async def test_text_prompt_block_uses_selected_model(monkeypatch, model_name, expected_llm_key):
    # Reset SettingsManager to base settings so cloud overrides from earlier tests don't leak
    monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="text_prompt_output",
        description=None,
        output_parameter_id="output-1",
        workflow_id="workflow-1",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )

    block = TextPromptBlock(
        label="text-block",
        llm_key="AZURE_OPENAI_GPT4_1",
        prompt="Explain the status.",
        parameters=[],
        json_schema=None,
        output_parameter=output_parameter,
        model={"model_name": model_name},
    )

    captured: dict[str, str] = {}
    fake_default_handler = AsyncMock()

    async def fake_resolve_default_llm_handler(*args, **kwargs):
        return fake_default_handler

    async def fake_handler(*, prompt: str, prompt_name: str, **kwargs):
        captured["prompt"] = prompt
        captured["prompt_name"] = prompt_name
        return {"llm_response": "ok"}

    def fake_get_override_handler(llm_key: str | None, *, default):
        captured["llm_key"] = llm_key if llm_key else "default"
        return fake_handler if llm_key else default

    block_module.app.LLM_API_HANDLER = fake_default_handler
    LLMAPIHandlerFactory = block_module.LLMAPIHandlerFactory
    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        fake_get_override_handler,
        raising=False,
    )
    monkeypatch.setattr(
        TextPromptBlock,
        "_resolve_default_llm_handler",
        fake_resolve_default_llm_handler,
        raising=False,
    )
    monkeypatch.setattr(
        prompt_engine,
        "load_prompt_from_string",
        lambda template, **kwargs: template,
    )

    response = await block.send_prompt(block.prompt, {}, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["llm_key"] == expected_llm_key
    assert captured["prompt_name"] == "text-prompt"
    assert response == {"llm_response": "ok"}


@pytest.mark.asyncio
async def test_text_prompt_block_uses_workflow_handler_when_no_override(monkeypatch):
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="text_prompt_output",
        description=None,
        output_parameter_id="output-2",
        workflow_id="workflow-1",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )

    block = TextPromptBlock(
        label="text-block",
        llm_key=None,
        prompt="Summarize status.",
        parameters=[],
        json_schema=None,
        output_parameter=output_parameter,
        model=None,
    )

    captured: dict[str, str] = {}
    fake_secondary_handler = AsyncMock(return_value={"llm_response": "secondary"})

    async def fake_prompt_type_handler(*args, **kwargs):
        return None

    def fake_get_override_handler(llm_key: str | None, *, default):
        captured["llm_key"] = llm_key if llm_key else "default"
        captured["default_handler"] = default
        return default

    block_module.app.SECONDARY_LLM_API_HANDLER = fake_secondary_handler
    block_module.app.LLM_API_HANDLER = AsyncMock()
    LLMAPIHandlerFactory = block_module.LLMAPIHandlerFactory
    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        fake_get_override_handler,
        raising=False,
    )
    monkeypatch.setattr(
        block_module,
        "get_llm_handler_for_prompt_type",
        fake_prompt_type_handler,
        raising=False,
    )
    monkeypatch.setattr(
        prompt_engine,
        "load_prompt_from_string",
        lambda template, **kwargs: template,
    )

    response = await block.send_prompt(block.prompt, {}, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["llm_key"] == "default"
    assert captured["default_handler"] == fake_secondary_handler
    fake_secondary_handler.assert_awaited_once()
    assert response == {"llm_response": "secondary"}


@pytest.mark.asyncio
async def test_text_prompt_block_prefers_prompt_type_config_over_secondary(monkeypatch):
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="text_prompt_output",
        description=None,
        output_parameter_id="output-3",
        workflow_id="workflow-1",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )

    block = TextPromptBlock(
        label="text-block",
        llm_key=None,
        prompt="Provide summary.",
        parameters=[],
        json_schema=None,
        output_parameter=output_parameter,
        model=None,
    )

    captured: dict[str, str] = {}
    prompt_config_handler = AsyncMock(return_value={"llm_response": "config"})

    async def fake_prompt_type_handler(*args, **kwargs):
        return prompt_config_handler

    def fake_get_override_handler(llm_key: str | None, *, default):
        captured["default_handler"] = default
        return default

    block_module.app.SECONDARY_LLM_API_HANDLER = AsyncMock()
    block_module.app.LLM_API_HANDLER = AsyncMock()
    LLMAPIHandlerFactory = block_module.LLMAPIHandlerFactory
    monkeypatch.setattr(
        LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        fake_get_override_handler,
        raising=False,
    )
    monkeypatch.setattr(
        block_module,
        "get_llm_handler_for_prompt_type",
        fake_prompt_type_handler,
        raising=False,
    )
    monkeypatch.setattr(
        prompt_engine,
        "load_prompt_from_string",
        lambda template, **kwargs: template,
    )

    response = await block.send_prompt(block.prompt, {}, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["default_handler"] == prompt_config_handler
    prompt_config_handler.assert_awaited_once()
    assert response == {"llm_response": "config"}
