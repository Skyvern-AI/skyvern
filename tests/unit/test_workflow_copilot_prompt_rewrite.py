from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools.banned_blocks import _code_only_browser_authoring_prompt
from skyvern.forge.sdk.copilot.tools.mcp_hooks import (
    _get_block_schema_post_hook,
    _get_workflow_knowledge_post_hook,
)


def _code_only_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.code_only_code_schema_seen = False
    return ctx


@pytest.mark.asyncio
async def test_code_schema_is_the_discoverable_home_for_runtime_helpers() -> None:
    result = {"ok": True, "data": {"block_type": "code"}}

    rendered = await _get_block_schema_post_hook(result, {"block_type": "code"}, _code_only_ctx())

    guidance = "\n".join(rendered["data"]["code_only_guidance"])
    assert "await solve_captcha(page)" in guidance
    assert "<key>.username" in guidance
    assert "<key>.password" in guidance


@pytest.mark.asyncio
async def test_workflow_knowledge_marks_code_only_policy_as_authoritative() -> None:
    ctx = _code_only_ctx()
    result = {"ok": True, "data": {"sections": {"choosing_a_block": {"content": "all block types"}}}}

    rendered = await _get_workflow_knowledge_post_hook(result, {}, ctx)

    note = rendered["data"]["active_policy_note"]
    assert "author browser work with code blocks only" in note
    assert "get_block_schema is authoritative" in note


def test_code_only_policy_is_short_and_contains_no_settled_block_conversion_steering() -> None:
    rendered = _code_only_browser_authoring_prompt()

    assert "before authoring the first `code` block" in rendered
    assert "call `get_block_schema` with `block_type: code`" in rendered
    assert "solve_captcha" not in rendered
    assert "<key>.username" not in rendered
    assert "timeout=90000" not in rendered
    assert "ASK_QUESTION to confirm" not in rendered
    assert "derive a typed `extraction_schema`" in rendered
    assert len(rendered) < 3_500


def test_rendered_prompt_keeps_security_ask_telemetry_and_workflow_wide_edit_scope() -> None:
    from skyvern.forge.sdk.copilot.agent import _build_dynamic_system_prompt, _build_tool_usage_guide
    from skyvern.forge.sdk.copilot.tools import NATIVE_TOOLS, _build_skyvern_mcp_overlays

    config = CopilotConfig(
        security_rules="CUSTOM SECURITY RULE",
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
    )
    overlays = _build_skyvern_mcp_overlays(config.block_authoring_policy)
    tool_info = [(tool.name, tool.description or "") for tool in NATIVE_TOOLS]
    tool_info.extend((name, overlay.description or "") for name, overlay in overlays.items())
    instructions = _build_dynamic_system_prompt(tool_usage_guide=_build_tool_usage_guide(tool_info), config=config)

    def render(workflow_yaml: str) -> str:
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

    new_workflow_prompt = render("")
    settled_login_prompt = render(
        "workflow_definition:\n  blocks:\n    - block_type: login\n      label: existing_login\n"
    )

    assert new_workflow_prompt == settled_login_prompt
    assert "CUSTOM SECURITY RULE" in new_workflow_prompt
    assert '"ask_subject"' in new_workflow_prompt
    assert "ACTIVE BLOCK AUTHORING POLICY: CODE-ONLY BROWSER MODE" in new_workflow_prompt
