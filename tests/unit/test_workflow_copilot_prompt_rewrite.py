from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge import app
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot.agent import _build_dynamic_system_prompt, _build_tool_usage_guide
from skyvern.forge.sdk.copilot.config import (
    AGENT_BLOCKS_ONLY,
    ALL_BLOCK_FAMILIES,
    CODE_BLOCKS_ONLY,
    CopilotConfig,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import _copilot_to_call_tool_result
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS, _build_skyvern_mcp_overlays
from skyvern.forge.sdk.copilot.tools.banned_blocks import (
    AUTHORING_FAMILY_GUIDANCE,
    _code_only_browser_schema_guidance,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import (
    _get_block_schema_post_hook,
    _get_workflow_knowledge_post_hook,
)
from skyvern.schemas.workflows import CodeBlockYAML


# _build_dynamic_system_prompt stamps the current time once per call, so two renders
# built separately never compare equal.
@lru_cache(maxsize=1)
def _production_instructions() -> Callable[..., object]:
    config = CopilotConfig(security_rules="CUSTOM SECURITY RULE")
    config.authoring_capability = CODE_BLOCKS_ONLY
    overlays = _build_skyvern_mcp_overlays(config.authoring_capability)
    tool_info = [(tool.name, tool.description or "") for tool in NATIVE_TOOLS]
    tool_info.extend((name, overlay.description or "") for name, overlay in overlays.items())
    return _build_dynamic_system_prompt(tool_usage_guide=_build_tool_usage_guide(tool_info), config=config)


def _render_production_prompt(workflow_yaml: str = "") -> str:
    instructions = _production_instructions()
    ctx = CopilotContext(
        organization_id="org_test",
        workflow_id="workflow_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml=workflow_yaml,
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_copilot_chat_id="chat_test",
        request_policy=RequestPolicy(),
    )
    return str(instructions(SimpleNamespace(context=ctx), None))


def _sentence_containing(prompt: str, needle: str) -> str:
    return next((sentence for sentence in prompt.split(". ") if needle in sentence), "")


def _code_only_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        organization_id="o_test",
        workflow_permanent_id="wpid_test",
        authoring_capability=CODE_BLOCKS_ONLY,
        scout_trajectory=[],
    )


def _code_schema_result() -> dict[str, Any]:
    return {"ok": True, "data": {"block_type": "code", "schema": CodeBlockYAML.model_json_schema()}}


@pytest.mark.asyncio
async def test_code_only_code_schema_requires_a_non_null_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    code_only = await _get_block_schema_post_hook(_code_schema_result(), {}, _code_only_ctx())

    code_only_schema = code_only["data"]["schema"]
    prompt_schema = code_only_schema["properties"]["prompt"]
    assert "prompt" in code_only_schema["required"]
    assert prompt_schema["type"] == "string"
    assert "anyOf" not in prompt_schema
    assert "oneOf" not in prompt_schema

    model_result = _copilot_to_call_tool_result(code_only, "get_block_schema")
    model_payload = json.loads(model_result.content[0].text)
    model_schema = model_payload["data"]["schema"]
    assert "prompt" in model_schema["required"]
    assert model_schema["properties"]["prompt"]["type"] == "string"


@pytest.mark.asyncio
async def test_code_schema_is_not_shaped_when_code_cannot_be_authored() -> None:
    agent_only_ctx = SimpleNamespace(authoring_capability=AGENT_BLOCKS_ONLY)
    agent_only = await _get_block_schema_post_hook(_code_schema_result(), {}, agent_only_ctx)
    agent_only_schema = agent_only["data"]["schema"]

    assert "prompt" not in agent_only_schema["required"]
    assert "data_schema" not in agent_only_schema["required"]
    assert "code_only_guidance" not in agent_only["data"]


@pytest.mark.asyncio
async def test_code_only_code_schema_requires_a_return_data_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    code_only = await _get_block_schema_post_hook(_code_schema_result(), {}, _code_only_ctx())

    schema = code_only["data"]["schema"]
    assert schema["required"].count("data_schema") == 1
    assert schema["required"].count("prompt") == 1
    data_schema = schema["properties"]["data_schema"]
    assert data_schema["type"] == ["object", "null"]
    assert "return" in data_schema["description"]

    model_schema = json.loads(_copilot_to_call_tool_result(code_only, "get_block_schema").content[0].text)["data"][
        "schema"
    ]
    assert "data_schema" in model_schema["required"]


def test_legacy_code_block_yaml_does_not_synthesize_an_omitted_prompt() -> None:
    legacy_block = CodeBlockYAML(block_type="code", label="legacy_code", code="return None")

    assert legacy_block.prompt is None
    assert "prompt" not in legacy_block.model_dump(exclude_none=True)


@pytest.mark.asyncio
async def test_code_schema_is_the_discoverable_home_for_runtime_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "AGENT_FUNCTION", AgentFunction())
    result = {"ok": True, "data": {"block_type": "code"}}

    rendered = await _get_block_schema_post_hook(result, {"block_type": "code"}, _code_only_ctx())

    guidance = "\n".join(rendered["data"]["code_only_guidance"])
    assert "await solve_captcha(page)" in guidance
    assert "<key>.username" in guidance
    assert "<key>.password" in guidance

    model_result = _copilot_to_call_tool_result(rendered, "get_block_schema")
    model_payload = json.loads(model_result.content[0].text)
    model_guidance = "\n".join(model_payload["data"]["code_only_guidance"])
    assert "await clear_browser_data(page)" in model_guidance
    clear_helper = model_payload["data"]["clear_browser_data_helper_contract"]
    assert clear_helper["shadowed_by_parameter"] == "clear_browser_data"
    assert clear_helper["on_parameter_collision"]
    open_page_helper = model_payload["data"]["open_page_helper_contract"]
    assert open_page_helper["call"] == "await open_page(page, url)"
    assert open_page_helper["shadowed_by_parameter"] == "open_page"
    assert open_page_helper["on_parameter_collision"]


@pytest.mark.asyncio
async def test_choosing_a_block_carries_the_family_rule_only_when_both_families_are_authorable() -> None:
    both = SimpleNamespace(authoring_capability=ALL_BLOCK_FAMILIES)
    rendered = await _get_workflow_knowledge_post_hook(
        {"ok": True, "data": {"sections": {"choosing_a_block": {"content": "all block types"}}}}, {}, both
    )
    assert rendered["data"]["sections"]["choosing_a_block"]["content"].startswith(AUTHORING_FAMILY_GUIDANCE)

    code_only = await _get_workflow_knowledge_post_hook(
        {"ok": True, "data": {"sections": {"choosing_a_block": {"content": "all block types"}}}},
        {},
        _code_only_ctx(),
    )
    assert code_only["data"]["sections"]["choosing_a_block"]["content"] == "all block types"
    assert "active_policy_note" not in code_only["data"]


def test_code_block_schema_guidance_states_the_runtime_without_prescribing_a_procedure() -> None:
    rendered = "\n".join(_code_only_browser_schema_guidance())

    assert "no `import` statements" in rendered
    assert "derive a typed `extraction_schema`" in rendered
    assert "ASK_QUESTION to confirm" not in rendered


def test_rendered_prompt_keeps_security_ask_telemetry_and_workflow_wide_edit_scope() -> None:
    new_workflow_prompt = _render_production_prompt("")
    settled_login_prompt = _render_production_prompt(
        "workflow_definition:\n  blocks:\n    - block_type: login\n      label: existing_login\n"
    )

    assert new_workflow_prompt == settled_login_prompt
    assert "CUSTOM SECURITY RULE" in new_workflow_prompt
    assert '"ask_subject"' in new_workflow_prompt
    assert "AUTHORING POLICY" not in new_workflow_prompt


def test_verbatim_synthesized_code_is_scoped_to_the_code_block_schema() -> None:
    guidance = "\n".join(_code_only_browser_schema_guidance())

    assert "SYNTHESIZED CODE BLOCK" in guidance
    assert "must not use page.evaluate" in guidance
    assert "SYNTHESIZED CODE BLOCK" not in _render_production_prompt()


def test_ask_carve_out_gates_money_and_destruction_and_never_a_site_sent_message() -> None:
    prompt = _render_production_prompt()
    carve_out = _sentence_containing(prompt, "spends money")

    assert "spends money" in carve_out
    assert "destroys something the user cannot restore" in carve_out
    # A page click that makes the site email its own account holder is not the workflow
    # sending anything, and no permission clause may read it as one.
    assert "message or email" not in prompt
