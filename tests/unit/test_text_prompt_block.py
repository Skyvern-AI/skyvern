import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

import skyvern.forge.sdk.workflow.models.block as _block_mod
from skyvern.config import settings as base_settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TextPromptBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import TextPromptBlockYAML, WorkflowRequest

block_module = sys.modules["skyvern.forge.sdk.workflow.models.block"]


def _make_workflow_run_context(values: dict | None = None) -> WorkflowRunContext:
    """Create a minimal WorkflowRunContext with given values for template rendering tests."""
    ctx = WorkflowRunContext(
        workflow_title="test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )
    if values:
        ctx.values.update(values)
    return ctx


def _make_text_prompt_block(
    prompt: str = "test prompt",
    json_schema: dict | None = None,
) -> TextPromptBlock:
    now = datetime.now(timezone.utc)
    return TextPromptBlock(
        label="test-block",
        llm_key=None,
        prompt=prompt,
        parameters=[],
        json_schema=json_schema,
        output_parameter=OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key="text_prompt_output",
            description=None,
            output_parameter_id="output-test",
            workflow_id="workflow-1",
            created_at=now,
            modified_at=now,
            deleted_at=None,
        ),
        model=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_name",
    ["gemini-2.5-flash", "gemini-3-pro-preview"],
)
async def test_text_prompt_block_uses_selected_model(monkeypatch, model_name):
    # Reset SettingsManager to base settings so cloud overrides from earlier tests don't leak
    monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)
    expected_llm_key = base_settings.get_model_name_to_llm_key()[model_name]["llm_key"]
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


@pytest.mark.asyncio
async def test_text_prompt_block_bad_llm_key_uses_same_runtime_path_as_no_override(monkeypatch):
    monkeypatch.setattr(SettingsManager, "_SettingsManager__instance", base_settings)
    now = datetime.now(timezone.utc)

    normalized_bad = TextPromptBlockYAML(
        label="bad_key",
        prompt="Summarize status.",
        llm_key="ANTHROPIC_CLAUDE_3_5_SONNET",
    )
    no_override = TextPromptBlockYAML(
        label="no_override",
        prompt="Summarize status.",
        llm_key=None,
    )

    blocks = []
    for idx, yaml_block in enumerate((normalized_bad, no_override), start=1):
        output_parameter = OutputParameter(
            parameter_type=ParameterType.OUTPUT,
            key=f"text_prompt_output_{idx}",
            description=None,
            output_parameter_id=f"output-{idx}",
            workflow_id="workflow-1",
            created_at=now,
            modified_at=now,
            deleted_at=None,
        )
        blocks.append(
            TextPromptBlock(
                label=yaml_block.label,
                llm_key=yaml_block.llm_key,
                prompt=yaml_block.prompt,
                parameters=[],
                json_schema=None,
                output_parameter=output_parameter,
                model=yaml_block.model,
            )
        )

    captured: list[tuple[str | None, object]] = []
    fake_secondary_handler = AsyncMock(return_value={"llm_response": "secondary"})

    async def fake_prompt_type_handler(*args, **kwargs):
        return None

    def fake_get_override_handler(llm_key: str | None, *, default):
        captured.append((llm_key, default))
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

    for block in blocks:
        response = await block.send_prompt(block.prompt, {}, workflow_run_id="workflow-run", organization_id="org-1")
        assert response == {"llm_response": "secondary"}

    assert captured == [
        (None, fake_secondary_handler),
        (None, fake_secondary_handler),
    ]


@pytest.mark.asyncio
async def test_text_prompt_block_uses_explicit_internal_llm_key_override(monkeypatch):
    now = datetime.now(timezone.utc)
    output_parameter = OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="text_prompt_output_internal",
        description=None,
        output_parameter_id="output-internal",
        workflow_id="workflow-1",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )

    block = TextPromptBlock(
        label="text-block",
        llm_key="SPECIAL_INTERNAL_KEY",
        prompt="Summarize status.",
        parameters=[],
        json_schema=None,
        output_parameter=output_parameter,
        model=None,
    )

    captured: dict[str, object] = {}
    fake_default_handler = AsyncMock()
    fake_override_handler = AsyncMock(return_value={"llm_response": "override"})

    async def fake_resolve_default_llm_handler(*args, **kwargs):
        return fake_default_handler

    def fake_get_override_handler(llm_key: str | None, *, default):
        captured["llm_key"] = llm_key
        captured["default_handler"] = default
        return fake_override_handler

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

    assert captured["llm_key"] == "SPECIAL_INTERNAL_KEY"
    assert captured["default_handler"] == fake_default_handler
    fake_override_handler.assert_awaited_once()
    assert response == {"llm_response": "override"}


def test_workflow_request_deserialization_strips_invalid_text_prompt_llm_key() -> None:
    """Verify FastAPI deserialization (not just explicit model_validate) strips bad keys.

    Moved from tests/scenario/ — this test only validates Pydantic behavior and
    does not require a database connection.
    """
    raw_request = {
        "json_definition": {
            "title": "Deserialization test",
            "workflow_definition": {
                "blocks": [
                    {
                        "block_type": "text_prompt",
                        "label": "summarize",
                        "prompt": "Summarize the result",
                        "llm_key": "ANTHROPIC_CLAUDE_3_5_SONNET",
                    }
                ],
                "parameters": [],
            },
        }
    }
    workflow_request = WorkflowRequest.model_validate(raw_request)

    block = workflow_request.json_definition.workflow_definition.blocks[0]
    assert block.llm_key is None
    assert block.model is None


# --- json_schema Jinja template rendering tests (SKY-6479) ---


def test_render_schema_templates_resolves_variables():
    """json_schema description fields with {{ }} should be rendered."""
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "Add 14 days to {{ start_date }}",
                }
            },
        },
    )
    ctx = _make_workflow_run_context({"start_date": "2025-01-15"})
    rendered = block._render_schema_templates(block.json_schema, ctx)

    assert rendered["properties"]["result"]["description"] == "Add 14 days to 2025-01-15"
    # non-template values preserved
    assert rendered["type"] == "object"
    assert rendered["properties"]["result"]["type"] == "string"


def test_render_schema_templates_nested_list():
    """Templates inside lists within the schema should also be rendered."""
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "required": ["{{ field_name }}"],
        },
    )
    ctx = _make_workflow_run_context({"field_name": "invoice_date"})
    rendered = block._render_schema_templates(block.json_schema, ctx)

    assert rendered["required"] == ["invoice_date"]


def test_render_schema_templates_failure_preserves_original():
    """When a single value fails to render, it should be preserved and a warning logged."""
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "properties": {
                "good": {"description": "value is {{ known }}"},
                "bad": {"description": "value is {{ unknown }}"},
            },
        },
    )
    strict_env = SandboxedEnvironment(undefined=StrictUndefined)
    with (
        patch.object(base_settings, "WORKFLOW_TEMPLATING_STRICTNESS", "strict"),
        patch.object(_block_mod, "jinja_sandbox_env", strict_env),
    ):
        ctx = _make_workflow_run_context({"known": "hello"})
        rendered = block._render_schema_templates(block.json_schema, ctx)

        # good value rendered
        assert rendered["properties"]["good"]["description"] == "value is hello"
        # bad value preserved as-is
        assert rendered["properties"]["bad"]["description"] == "value is {{ unknown }}"


def test_format_potential_template_parameters_renders_json_schema():
    """format_potential_template_parameters should render json_schema templates."""
    block = _make_text_prompt_block(
        prompt="Calculate dates for {{ start_date }}",
        json_schema={
            "type": "object",
            "properties": {
                "date_plus_14": {
                    "type": "string",
                    "description": "14 days after {{ start_date }}",
                }
            },
        },
    )
    ctx = _make_workflow_run_context({"start_date": "2025-01-15"})
    block.format_potential_template_parameters(ctx)

    assert block.prompt == "Calculate dates for 2025-01-15"
    assert block.json_schema["properties"]["date_plus_14"]["description"] == "14 days after 2025-01-15"


def test_format_potential_template_parameters_no_json_schema():
    """When json_schema is None, format_potential_template_parameters should not fail."""
    block = _make_text_prompt_block(prompt="simple prompt", json_schema=None)
    ctx = _make_workflow_run_context()
    block.format_potential_template_parameters(ctx)

    assert block.json_schema is None
    assert block.prompt == "simple prompt"
