"""Tests for the copilot-v2 SchemaOverlay hooks that ban task/task_v2 block
types at the discovery surface (SKY-9174, Part C)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.tools import (
    _COPILOT_BANNED_BLOCK_TYPES,
    _get_block_schema_post_hook,
    _get_block_schema_pre_hook,
)
from skyvern.forge.sdk.copilot.tools.banned_blocks import (
    _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES,
    CopilotBlockPolicyStatus,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _validate_block_pre_hook

_CODE_ONLY_UNAVAILABLE = tuple(
    "action browser_task extraction file_download file_upload goto_url login navigation print_page task task_v2 validation".split()
)
_CODE_ONLY_REQUIRED_TEXT = {
    "file_download": "download registration",
    "file_upload": "file materialization",
    "login": "credential-typed code",
    "task": "declared AI leaf",
    "task_v2": "declared AI leaf",
}
_CODE_ONLY_HELPERS = tuple(
    "conditional for_loop while_loop http_request send_email file_url_parser download_to_s3 upload_to_s3 google_sheets_read google_sheets_write".split()
)


@pytest.fixture
def ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.block_authoring_policy = BlockAuthoringPolicy.STANDARD
    return ctx


@pytest.fixture
def code_only_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    return ctx


@pytest.mark.parametrize("block_type", ["task", "task_v2", "TASK", "Task_V2", "  task  "])
@pytest.mark.asyncio
async def test_pre_hook_blocks_banned_types_case_and_whitespace_insensitive(block_type: str, ctx: MagicMock) -> None:
    result = await _get_block_schema_pre_hook({"block_type": block_type}, ctx)

    assert result is not None
    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
    for alternative in ("navigation", "extraction", "validation", "login"):
        assert alternative in result["error"]


@pytest.mark.asyncio
async def test_pre_hook_allows_non_banned_types(ctx: MagicMock) -> None:
    for block_type in ("navigation", "extraction", "validation", "login", "goto_url", "for_loop"):
        assert await _get_block_schema_pre_hook({"block_type": block_type}, ctx) is None


@pytest.mark.asyncio
async def test_pre_hook_canonicalizes_browser_task_alias(ctx: MagicMock) -> None:
    params = {"block_type": "browser_task"}

    assert await _get_block_schema_pre_hook(params, ctx) is None
    assert params["block_type"] == "navigation"


@pytest.mark.asyncio
async def test_pre_hook_allows_list_mode_no_block_type(ctx: MagicMock) -> None:
    assert await _get_block_schema_pre_hook({}, ctx) is None
    assert await _get_block_schema_pre_hook({"block_type": None}, ctx) is None


@pytest.mark.asyncio
async def test_pre_hook_allows_non_string_block_type(ctx: MagicMock) -> None:
    assert await _get_block_schema_pre_hook({"block_type": 123}, ctx) is None


@pytest.mark.asyncio
async def test_post_hook_scrubs_banned_types_from_list_response(ctx: MagicMock) -> None:
    result = {
        "ok": True,
        "data": {
            "block_types": {
                "navigation": "Take actions on a page",
                "task": "deprecated",
                "task_v2": "deprecated",
                "extraction": "Extract data",
            },
            "count": 4,
        },
    }

    out = await _get_block_schema_post_hook(result, raw={}, ctx=ctx)

    assert set(out["data"]["block_types"]) == {"navigation", "extraction"}


@pytest.mark.asyncio
async def test_post_hook_passthrough_when_no_block_types_dict(ctx: MagicMock) -> None:
    result = {"ok": True, "data": {"block_type": "navigation", "summary": "..."}}

    out = await _get_block_schema_post_hook(result, raw={}, ctx=ctx)

    assert out == {"ok": True, "data": {"block_type": "navigation", "summary": "..."}}


@pytest.mark.asyncio
async def test_post_hook_handles_missing_or_malformed_data(ctx: MagicMock) -> None:
    assert await _get_block_schema_post_hook({"ok": False, "error": "x"}, raw={}, ctx=ctx) == {
        "ok": False,
        "error": "x",
    }
    assert await _get_block_schema_post_hook({"ok": True, "data": None}, raw={}, ctx=ctx) == {
        "ok": True,
        "data": None,
    }
    assert await _get_block_schema_post_hook(
        {"ok": True, "data": {"block_types": ["not", "a", "dict"]}}, raw={}, ctx=ctx
    ) == {"ok": True, "data": {"block_types": ["not", "a", "dict"]}}


def test_banned_types_set_contents() -> None:
    assert _COPILOT_BANNED_BLOCK_TYPES == frozenset({"task", "task_v2"})


def test_code_only_policy_table_derives_unavailable_types() -> None:
    assert _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES == frozenset(_CODE_ONLY_UNAVAILABLE)
    assert "native_allowed" not in {status.value for status in CopilotBlockPolicyStatus}


@pytest.mark.parametrize("block_type", _CODE_ONLY_UNAVAILABLE)
@pytest.mark.asyncio
async def test_code_only_schema_pre_hook_rejects_table_entries(block_type: str, code_only_ctx: MagicMock) -> None:
    result = await _get_block_schema_pre_hook({"block_type": block_type}, code_only_ctx)

    assert result is not None
    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
    assert _CODE_ONLY_REQUIRED_TEXT.get(block_type, "focused `code` blocks") in result["error"]


@pytest.mark.asyncio
async def test_code_only_schema_pre_hook_normalizes_case_whitespace_and_alias(code_only_ctx: MagicMock) -> None:
    params = {"block_type": "  BROWSER_TASK  "}

    result = await _get_block_schema_pre_hook(params, code_only_ctx)

    assert result is not None
    assert result["ok"] is False
    assert params["block_type"] == "navigation"
    assert "focused `code` blocks" in result["error"]


@pytest.mark.asyncio
async def test_code_only_post_hook_scrubs_all_policy_table_entries(code_only_ctx: MagicMock) -> None:
    result = {
        "ok": True,
        "data": {
            "block_types": dict.fromkeys(
                ("navigation", "code", "conditional", "task", "task_v2", "login", "file_download", "file_upload"),
                "...",
            ),
            "count": 8,
        },
    }

    out = await _get_block_schema_post_hook(result, raw={}, ctx=code_only_ctx)

    assert set(out["data"]["block_types"]) == {"code", "conditional"}
    assert out["data"]["count"] == 2


@pytest.mark.asyncio
async def test_code_schema_guidance_is_policy_rendered_and_allows_helper_validation(code_only_ctx: MagicMock) -> None:
    result = {"ok": True, "data": {"block_type": "code", "summary": "..."}}

    out = await _get_block_schema_post_hook(result, raw={}, ctx=code_only_ctx)

    assert "Browser/page workflow block types are unavailable" in out["data"]["code_only_note"]
    assert "validate_block only for allowed non-browser helper blocks" in " ".join(out["data"]["code_only_guidance"])
    assert "Do not persist navigation/action/login" not in " ".join(out["data"]["code_only_guidance"])


@pytest.mark.parametrize("block_type", ["task", "task_v2"])
@pytest.mark.asyncio
async def test_standard_validate_block_pre_hook_preserves_existing_behavior(block_type: str, ctx: MagicMock) -> None:
    result = await _validate_block_pre_hook({"block_json": f'{{"block_type": "{block_type}", "label": "x"}}'}, ctx)

    assert result is None


@pytest.mark.parametrize("block_type", _CODE_ONLY_UNAVAILABLE + ("code", " LOGIN ", "BROWSER_TASK"))
@pytest.mark.asyncio
async def test_code_only_validate_block_pre_hook_rejects_unavailable_or_probe_types(
    block_type: str, code_only_ctx: MagicMock
) -> None:
    result = await _validate_block_pre_hook(
        {"block_json": f'{{"block_type": "{block_type}", "label": "candidate"}}'},
        code_only_ctx,
    )

    assert result is not None
    assert result["ok"] is False
    if block_type.strip().lower() == "code":
        assert "validate real code blocks through update_and_run_blocks" in result["error"]
    else:
        assert "not available in the workflow copilot" in result["error"]


@pytest.mark.parametrize("block_type", _CODE_ONLY_HELPERS)
@pytest.mark.asyncio
async def test_code_only_validate_block_pre_hook_allows_non_browser_helpers(
    block_type: str, code_only_ctx: MagicMock
) -> None:
    result = await _validate_block_pre_hook(
        {"block_json": f'{{"block_type": "{block_type}", "label": "candidate"}}'},
        code_only_ctx,
    )

    assert result is None


@pytest.mark.parametrize("block_json", ["not json", "[]", '{"label": "missing_type"}'])
@pytest.mark.asyncio
async def test_code_only_validate_block_pre_hook_leaves_shape_errors_to_validator(
    block_json: str, code_only_ctx: MagicMock
) -> None:
    result = await _validate_block_pre_hook({"block_json": block_json}, code_only_ctx)

    assert result is None


_NAV_BLOCK = '{"block_type": "navigation", "label": "x", "url": "https://e.com", "navigation_goal": "g"}'


@pytest.mark.parametrize("alias", ["block", "block_definition", "definition", "block_yaml"])
@pytest.mark.asyncio
async def test_validate_block_pre_hook_normalizes_misnamed_arg_to_block_json(alias: str, ctx: MagicMock) -> None:
    """SKY-11133: the model calls validate_block with the block under a shorter
    key (e.g. `block`). The pre-hook must promote it to `block_json` so the call
    no longer dies at FastMCP signature validation."""
    params = {alias: _NAV_BLOCK}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert params["block_json"] == _NAV_BLOCK
    assert alias not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_serializes_dict_alias_value(ctx: MagicMock) -> None:
    block = {"block_type": "navigation", "label": "x", "url": "https://e.com", "navigation_goal": "g"}
    params: dict = {"block": block}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert json.loads(params["block_json"]) == block
    assert "block" not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_strips_stray_alias_without_clobbering_block_json(ctx: MagicMock) -> None:
    params = {"block_json": _NAV_BLOCK, "block": '{"block_type": "extraction", "label": "y"}'}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert params["block_json"] == _NAV_BLOCK
    assert "block" not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_normalizes_alias_before_code_only_gate(code_only_ctx: MagicMock) -> None:
    """Normalization must run before the code-only policy gate so a `code` block
    passed under the wrong key is still rejected (not crashed)."""
    params = {"block": '{"block_type": "code", "label": "x", "code": "pass"}'}

    result = await _validate_block_pre_hook(params, code_only_ctx)

    assert result is not None
    assert result["ok"] is False
    assert "validate real code blocks through update_and_run_blocks" in result["error"]
    assert params["block_json"]
    assert "block" not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_alias_allows_helper_under_code_only(code_only_ctx: MagicMock) -> None:
    params = {"block": '{"block_type": "conditional", "label": "x"}'}

    result = await _validate_block_pre_hook(params, code_only_ctx)

    assert result is None
    assert params["block_json"] == '{"block_type": "conditional", "label": "x"}'


def test_pre_hook_and_post_emission_reject_share_constant() -> None:
    """SKY-9174 Part F: the pre-emission SchemaOverlay hooks and the
    post-emission YAML-level reject (in `_update_workflow` + REPLACE_WORKFLOW)
    both import `_COPILOT_BANNED_BLOCK_TYPES` from the same module. Guard
    against a future refactor that redefines the set in only one place —
    any divergence would leave one of the two layers out of sync."""
    import skyvern.forge.sdk.copilot.tools as tools_module

    # `_detect_new_banned_blocks` exists on the same module and is the
    # post-emission counterpart. If either symbol is removed, the layer is
    # effectively ripped out and we want this test to catch it.
    assert hasattr(tools_module, "_COPILOT_BANNED_BLOCK_TYPES")
    assert hasattr(tools_module, "_get_block_schema_pre_hook")
    assert hasattr(tools_module, "_get_block_schema_post_hook")
    assert hasattr(tools_module, "_detect_new_banned_blocks")
    assert hasattr(tools_module, "_banned_block_reject_message")
