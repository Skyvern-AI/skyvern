import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy.exc import IntegrityError, OperationalError

import skyvern.forge.sdk.workflow.models.block as _block_mod
from skyvern.config import settings as base_settings
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.exceptions import InvalidLLMResponseFormat
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TextPromptBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockStatus, TextPromptBlockYAML, WorkflowRequest

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

    response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["llm_key"] == expected_llm_key
    assert captured["prompt_name"] == "text-prompt"
    assert response == {"llm_response": "ok"}
    assert block.json_schema is None


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

    response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")

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

    response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")

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
        response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")
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

    response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["llm_key"] == "SPECIAL_INTERNAL_KEY"
    assert captured["default_handler"] == fake_default_handler
    fake_override_handler.assert_awaited_once()
    assert response == {"llm_response": "override"}


@pytest.mark.asyncio
async def test_text_prompt_block_array_schema_does_not_force_dict(monkeypatch):
    block = _make_text_prompt_block(
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                },
                "required": ["first_name", "last_name"],
            },
        },
    )

    captured: dict[str, object] = {}

    async def fake_handler(*, prompt: str, prompt_name: str, force_dict: bool, **kwargs):
        captured["prompt"] = prompt
        captured["prompt_name"] = prompt_name
        captured["force_dict"] = force_dict
        return [{"first_name": "Zachary", "last_name": "Leibrand"}]

    async def fake_resolve_default_llm_handler(*args, **kwargs):
        return fake_handler

    def fake_get_override_handler(llm_key: str | None, *, default):
        return default

    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
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

    response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["prompt_name"] == "text-prompt"
    assert captured["force_dict"] is False
    assert response == [{"first_name": "Zachary", "last_name": "Leibrand"}]


@pytest.mark.asyncio
async def test_text_prompt_block_object_schema_does_not_force_dict_before_validation(monkeypatch):
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "properties": {
                "invoice_search_string": {"type": "string"},
            },
            "required": ["invoice_search_string"],
        },
    )

    captured: dict[str, object] = {}

    async def fake_handler(*, prompt: str, prompt_name: str, force_dict: bool, **kwargs):
        captured["force_dict"] = force_dict
        return {"invoice_search_string": "062026"}

    async def fake_resolve_default_llm_handler(*args, **kwargs):
        return fake_handler

    def fake_get_override_handler(llm_key: str | None, *, default):
        return default

    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
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

    response = await block.send_prompt(block.prompt, workflow_run_id="workflow-run", organization_id="org-1")

    assert captured["force_dict"] is False
    assert response == {"invoice_search_string": "062026"}


@pytest.mark.asyncio
async def test_text_prompt_block_retry_feedback_is_not_rendered_as_template(monkeypatch):
    # send_prompt receives the already-rendered prompt and must not render it again, so
    # schema-validation retry feedback (which may contain `{{ }}`) reaches the model verbatim.
    block = _make_text_prompt_block(
        prompt="Hello Alice",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )
    captured: dict[str, object] = {}

    async def fake_handler(*, prompt: str, prompt_name: str, force_dict: bool, **kwargs):
        captured["prompt"] = prompt
        return {"answer": "ok"}

    async def fake_resolve_default_llm_handler(*args, **kwargs):
        return fake_handler

    def fake_get_override_handler(llm_key: str | None, *, default):
        return default

    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
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

    await block.send_prompt(
        block.prompt,
        workflow_run_id="workflow-run",
        organization_id="org-1",
        workflow_run_block_id=None,
        schema_validation_failure="root: has 1 unexpected properties {{ dangerous_lookup }}",
    )

    sent_prompt = captured["prompt"]
    assert isinstance(sent_prompt, str)
    assert "Hello Alice" in sent_prompt
    assert "previous response failed JSON schema validation" in sent_prompt
    assert "{{ dangerous_lookup }}" in sent_prompt


def test_text_prompt_block_schema_validation_rejects_wrong_root_type() -> None:
    block = _make_text_prompt_block(
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                },
                "required": ["first_name", "last_name"],
            },
        },
    )

    failure_reason = block._validate_response_against_json_schema(
        {"first_name": "f_name_var", "last_name": "l_name_var\n```python\n..."}
    )

    assert failure_reason is not None
    assert "does not match text prompt JSON schema" in failure_reason
    assert "root" in failure_reason
    assert "expected type array, got object" in failure_reason


def test_text_prompt_block_schema_validation_feedback_does_not_echo_invalid_response() -> None:
    secret_like_value = "customer-private-value-" + ("x" * 300)
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["values"],
        },
    )

    failure_reason = block._validate_response_against_json_schema({"values": secret_like_value})

    assert failure_reason is not None
    assert "root.values: expected type array, got string" in failure_reason
    assert secret_like_value not in failure_reason
    assert "customer-private-value" not in failure_reason


def test_text_prompt_block_additional_properties_feedback_does_not_echo_response_keys() -> None:
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "properties": {"safe": {"type": "string"}},
            "additionalProperties": False,
        },
    )

    failure_reason = block._validate_response_against_json_schema({"safe": "ok", "customer@example.com": "private"})

    assert failure_reason is not None
    assert "has 1 unexpected properties" in failure_reason
    assert "customer@example.com" not in failure_reason
    assert "private" not in failure_reason


def test_text_prompt_block_map_value_path_does_not_echo_response_key() -> None:
    block = _make_text_prompt_block(
        json_schema={
            "type": "object",
            "additionalProperties": {"type": "number"},
        },
    )

    failure_reason = block._validate_response_against_json_schema({"customer@example.com": "not-a-number"})

    assert failure_reason is not None
    assert "root.<map value>: expected type number, got string" in failure_reason
    assert "customer@example.com" not in failure_reason
    assert "not-a-number" not in failure_reason


def test_text_prompt_block_unresolvable_ref_fails_schema_validation_gracefully() -> None:
    block = _make_text_prompt_block(json_schema={"$ref": "#/$defs/missing"})

    failure_reason = block._validate_response_against_json_schema({"value": "ok"})

    assert failure_reason is not None
    assert "Text prompt JSON schema validation failed" in failure_reason


@pytest.mark.asyncio
async def test_text_prompt_block_execute_fails_schema_validation_before_recording(monkeypatch):
    block = _make_text_prompt_block(
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                },
                "required": ["first_name", "last_name"],
            },
        },
    )
    workflow_run_context = _make_workflow_run_context()
    bad_response = {"first_name": "f_name_var", "last_name": "code fragment"}
    record_output_parameter_value = AsyncMock()
    send_prompt = AsyncMock(return_value=bad_response)

    monkeypatch.setattr(TextPromptBlock, "send_prompt", send_prompt)
    monkeypatch.setattr(
        TextPromptBlock,
        "get_workflow_run_context",
        staticmethod(lambda workflow_run_id: workflow_run_context),
    )
    monkeypatch.setattr(TextPromptBlock, "record_output_parameter_value", record_output_parameter_value)

    result = await block.execute(
        workflow_run_id="workflow-run",
        workflow_run_block_id="workflow-run-block",
        organization_id="org-1",
    )

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.output_parameter_value is None
    assert result.failure_reason is not None
    assert "does not match text prompt JSON schema" in result.failure_reason
    record_output_parameter_value.assert_not_awaited()
    assert send_prompt.await_count == block.schema_validation_max_attempts


@pytest.mark.asyncio
async def test_text_prompt_block_execute_invalid_schema_fails_without_llm_retry(monkeypatch):
    block = _make_text_prompt_block(json_schema={"type": 123})
    workflow_run_context = _make_workflow_run_context()
    record_output_parameter_value = AsyncMock()
    send_prompt = AsyncMock(return_value={"llm_response": "ok"})

    monkeypatch.setattr(TextPromptBlock, "send_prompt", send_prompt)
    monkeypatch.setattr(
        TextPromptBlock,
        "get_workflow_run_context",
        staticmethod(lambda workflow_run_id: workflow_run_context),
    )
    monkeypatch.setattr(TextPromptBlock, "record_output_parameter_value", record_output_parameter_value)

    result = await block.execute(
        workflow_run_id="workflow-run",
        workflow_run_block_id="workflow-run-block",
        organization_id="org-1",
    )

    assert result.success is False
    assert result.status == BlockStatus.failed
    assert result.failure_reason == "Text prompt JSON schema is invalid."
    send_prompt.assert_not_awaited()
    record_output_parameter_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_prompt_block_execute_retries_schema_validation_failure(monkeypatch):
    block = _make_text_prompt_block(
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                },
                "required": ["first_name", "last_name"],
            },
        },
    )
    workflow_run_context = _make_workflow_run_context()
    bad_response = {"first_name": "f_name_var", "last_name": "code fragment"}
    good_response = [{"first_name": "Zachary", "last_name": "Leibrand"}]
    responses = [bad_response, good_response]
    prompts: list[str] = []
    retry_failures: list[str | None] = []
    record_output_parameter_value = AsyncMock()

    async def fake_send_prompt(self, prompt, *args, **kwargs):
        prompts.append(prompt)
        retry_failures.append(kwargs.get("schema_validation_failure"))
        return responses.pop(0)

    monkeypatch.setattr(TextPromptBlock, "send_prompt", fake_send_prompt)
    monkeypatch.setattr(
        TextPromptBlock,
        "get_workflow_run_context",
        staticmethod(lambda workflow_run_id: workflow_run_context),
    )
    monkeypatch.setattr(TextPromptBlock, "record_output_parameter_value", record_output_parameter_value)

    result = await block.execute(
        workflow_run_id="workflow-run",
        workflow_run_block_id="workflow-run-block",
        organization_id="org-1",
    )

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value == good_response
    assert prompts == [block.prompt, block.prompt]
    assert retry_failures[0] is None
    assert retry_failures[1] is not None
    assert "expected type array, got object" in retry_failures[1]
    record_output_parameter_value.assert_awaited_once_with(workflow_run_context, "workflow-run", good_response)


@pytest.mark.asyncio
async def test_text_prompt_block_execute_retries_response_format_failure(monkeypatch):
    block = _make_text_prompt_block(
        json_schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                },
                "required": ["first_name", "last_name"],
            },
        },
    )
    workflow_run_context = _make_workflow_run_context()
    raw_bad_response = "not-json-customer-private-value-" + ("x" * 300)
    good_response = [{"first_name": "Zachary", "last_name": "Leibrand"}]
    prompts: list[str] = []
    retry_failures: list[str | None] = []
    record_output_parameter_value = AsyncMock()

    async def fake_send_prompt(self, prompt, *args, **kwargs):
        prompts.append(prompt)
        retry_failures.append(kwargs.get("schema_validation_failure"))
        if len(prompts) == 1:
            raise InvalidLLMResponseFormat(raw_bad_response)
        return good_response

    monkeypatch.setattr(TextPromptBlock, "send_prompt", fake_send_prompt)
    monkeypatch.setattr(
        TextPromptBlock,
        "get_workflow_run_context",
        staticmethod(lambda workflow_run_id: workflow_run_context),
    )
    monkeypatch.setattr(TextPromptBlock, "record_output_parameter_value", record_output_parameter_value)

    result = await block.execute(
        workflow_run_id="workflow-run",
        workflow_run_block_id="workflow-run-block",
        organization_id="org-1",
    )

    assert result.success is True
    assert result.status == BlockStatus.completed
    assert result.output_parameter_value == good_response
    assert prompts == [block.prompt, block.prompt]
    assert retry_failures[0] is None
    assert retry_failures[1] is not None
    assert "InvalidLLMResponseFormat" in retry_failures[1]
    assert raw_bad_response not in retry_failures[1]
    assert "customer-private-value" not in retry_failures[1]
    record_output_parameter_value.assert_awaited_once_with(workflow_run_context, "workflow-run", good_response)


def _patch_send_prompt_deps(monkeypatch, *, handler) -> None:
    async def fake_resolve(*args, **kwargs):
        return handler

    monkeypatch.setattr(TextPromptBlock, "_resolve_default_llm_handler", fake_resolve, raising=False)
    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
        "get_override_llm_api_handler",
        lambda llm_key, *, default: default,
        raising=False,
    )
    monkeypatch.setattr(block_module.asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
async def test_text_prompt_block_retries_transient_llm_provider_error(monkeypatch):
    """A transient provider error retries with backoff and recovers instead of failing."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    calls = {"n": 0}

    async def flaky(*, prompt, prompt_name, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise block_module.LLMProviderErrorRetryableTask("transient provider error")
        return {"llm_response": "recovered"}

    _patch_send_prompt_deps(monkeypatch, handler=flaky)

    response = await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert calls["n"] == 3
    assert response == {"llm_response": "recovered"}


@pytest.mark.asyncio
async def test_text_prompt_block_retries_transient_db_failure(monkeypatch):
    """A transient DB connection error (OperationalError) during the LLM call is retried."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    calls = {"n": 0}

    async def flaky(*, prompt, prompt_name, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT 1", None, Exception("server closed the connection"))
        return {"llm_response": "ok-after-db-retry"}

    _patch_send_prompt_deps(monkeypatch, handler=flaky)

    response = await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert calls["n"] == 2
    assert response == {"llm_response": "ok-after-db-retry"}


@pytest.mark.asyncio
async def test_text_prompt_block_retries_empty_response(monkeypatch):
    """A bad/empty first response no longer fails on the first attempt; a retry recovers."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    calls = {"n": 0}

    async def flaky(*, prompt, prompt_name, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise block_module.EmptyLLMResponseError("")
        return {"llm_response": "second-attempt"}

    _patch_send_prompt_deps(monkeypatch, handler=flaky)

    response = await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert calls["n"] == 2
    assert response == {"llm_response": "second-attempt"}


@pytest.mark.asyncio
async def test_text_prompt_block_raises_after_exhausting_retries(monkeypatch):
    """A persistently failing retriable error propagates after the max attempts."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    calls = {"n": 0}

    async def always_fails(*, prompt, prompt_name, **kwargs):
        calls["n"] += 1
        raise block_module.EmptyLLMResponseError("still empty")

    _patch_send_prompt_deps(monkeypatch, handler=always_fails)

    with pytest.raises(block_module.EmptyLLMResponseError):
        await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert calls["n"] == block_module.TEXT_PROMPT_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_text_prompt_block_non_retriable_error_not_retried(monkeypatch):
    """A non-retriable error propagates on the first attempt without re-issuing the call."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    calls = {"n": 0}

    async def value_fail(*, prompt, prompt_name, **kwargs):
        calls["n"] += 1
        raise ValueError("non-retriable")

    _patch_send_prompt_deps(monkeypatch, handler=value_fail)

    with pytest.raises(ValueError):
        await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_text_prompt_block_non_transient_db_error_not_retried(monkeypatch):
    """A non-transient DB error (IntegrityError) is not retried into a re-issued paid LLM call."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    calls = {"n": 0}

    async def integrity_fail(*, prompt, prompt_name, **kwargs):
        calls["n"] += 1
        raise IntegrityError("INSERT ...", None, Exception("duplicate key value"))

    _patch_send_prompt_deps(monkeypatch, handler=integrity_fail)

    with pytest.raises(IntegrityError):
        await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_text_prompt_block_uses_fallback_llm_key_when_available(monkeypatch):
    """send_prompt upgrades the block's key to a fallback-capable key when one exists."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    block.llm_key = "VERTEX_GEMINI_2.5_FLASH"
    captured: dict[str, str | None] = {}
    handler = AsyncMock(return_value={"llm_response": "ok"})

    async def fake_resolve(*args, **kwargs):
        return handler

    def fake_override(llm_key, *, default):
        captured["llm_key"] = llm_key
        return default

    monkeypatch.setattr(TextPromptBlock, "_resolve_default_llm_handler", fake_resolve, raising=False)
    monkeypatch.setattr(block_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", fake_override, raising=False)
    monkeypatch.setattr(
        block_module.app.AGENT_FUNCTION,
        "get_fallback_llm_key",
        lambda llm_key: "GEMINI_2_5_FLASH_WITH_FALLBACK",
        raising=False,
    )

    await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert captured["llm_key"] == "GEMINI_2_5_FLASH_WITH_FALLBACK"


@pytest.mark.asyncio
async def test_text_prompt_block_org_default_derives_key_for_fallback(monkeypatch):
    """Org-default runs (block sets no llm_key) derive the effective key from the resolved handler."""
    block = _make_text_prompt_block(prompt="Summarize status.")
    block.llm_key = None
    captured: dict[str, str | None] = {}
    handler = AsyncMock(return_value={"llm_response": "ok"})
    handler.llm_key = "VERTEX_GEMINI_2.5_FLASH"

    async def fake_resolve(*args, **kwargs):
        return handler

    def fake_override(llm_key, *, default):
        captured["llm_key"] = llm_key
        return default

    monkeypatch.setattr(TextPromptBlock, "_resolve_default_llm_handler", fake_resolve, raising=False)
    monkeypatch.setattr(block_module.LLMAPIHandlerFactory, "get_override_llm_api_handler", fake_override, raising=False)
    monkeypatch.setattr(
        block_module.app.AGENT_FUNCTION,
        "get_fallback_llm_key",
        lambda llm_key: f"{llm_key}_WITH_FALLBACK" if llm_key else None,
        raising=False,
    )

    await block.send_prompt(block.prompt, workflow_run_id="wr", organization_id="org-1")

    assert captured["llm_key"] == "VERTEX_GEMINI_2.5_FLASH_WITH_FALLBACK"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,forbidden",
    [
        ("{{ 7*7 }}", "49"),
        ("{{ (1).__class__.__name__ }}", "int"),
    ],
)
async def test_text_prompt_block_does_not_evaluate_template_in_parameter_value(monkeypatch, payload, forbidden):
    """A template expression delivered as a parameter *value* must reach the model as a
    literal and must never be evaluated by a second render.

    The prompt is rendered once (sandboxed) to substitute parameter values as inert text;
    it must not be rendered a second time, or the substituted value would be interpreted as
    a live Jinja template (server-side template injection).
    """
    block = _make_text_prompt_block(prompt="{{ payload }}")
    ctx = _make_workflow_run_context({"payload": payload})

    captured: dict[str, str] = {}

    async def fake_handler(*, prompt: str, prompt_name: str, **kwargs):
        captured["prompt"] = prompt
        return {"answer": "ok"}

    async def fake_resolve_default_llm_handler(*args, **kwargs):
        return fake_handler

    def fake_get_override_handler(llm_key: str | None, *, default):
        return default

    monkeypatch.setattr(
        block_module.LLMAPIHandlerFactory,
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

    # Real render #1 (sandboxed): the parameter value is substituted as inert text.
    block.format_potential_template_parameters(ctx)
    assert block.prompt == payload

    # Real send path, no monkeypatching of the render sink: the literal must survive.
    await block.send_prompt(
        block.prompt,
        workflow_run_id="wr_test",
        organization_id="org-1",
        workflow_run_block_id=None,
    )

    sent_prompt = captured["prompt"]
    assert payload in sent_prompt
    assert forbidden not in sent_prompt
