"""Tests for the copilot-v2 policy that bans ``task`` / ``task_v2`` (and, under
the code-only-browser policy, the wider browser block family) at every copilot
write surface (SKY-9174).

Two layers are covered here:

* Pre-emission — the ``SchemaOverlay`` pre / post hooks and ``validate_block``
  hook (Part C.1) block the types at the schema-lookup surface.
* Post-emission — the LLM can bypass the schema surface by writing YAML
  directly, so ``reject_authoring_violations`` + ``_update_workflow`` /
  ``REPLACE_WORKFLOW`` close the bypass with a YAML-level reject scoped to the
  blocks the turn introduces or changes, so legacy workflows with pre-existing
  ``task`` blocks can still be edited by the copilot.

Both layers import ``_COPILOT_BANNED_BLOCK_TYPES`` from the same module; the
cross-layer sync-guard test at the end asserts neither symbol is ripped out.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from skyvern.forge.sdk.copilot.config import (
    AGENT_BLOCKS_ONLY,
    ALL_BLOCK_FAMILIES,
    CODE_BLOCKS_ONLY,
    AuthoringCapability,
    CopilotConfig,
    authoring_capability_from_policy,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import (
    _COPILOT_BANNED_BLOCK_TYPES,
    _get_block_schema_post_hook,
    _get_block_schema_pre_hook,
    _proxy_location_trace_value,
    _raw_yaml_proxy_location,
    _update_workflow,
    copilot_native_tools,
    reject_authoring_violations,
)
from skyvern.forge.sdk.copilot.tools.banned_blocks import (
    _AGENT_FAMILY_BLOCK_TYPES,
    _CODE_BLOCKS_ONLY_BANNED_BLOCK_TYPES,
    AUTHORING_FAMILY_GUIDANCE,
    CODE_BLOCK_SUMMARY,
    CREDENTIAL_CODE_ACCESSORS,
    SCHEMA_FIRST_GUIDANCE,
    _banned_block_types_for_capability,
    _block_authoring_violations,
    _changed_blocks,
    _code_only_browser_schema_guidance,
    _copilot_authoring_capability,
    authoring_turn_summary,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import (
    _normalized_authoring_capability,
    _validate_block_pre_hook,
)
from skyvern.forge.sdk.workflow.models.block import _TASK_V3_SUPPORTED_BLOCK_TYPES
from skyvern.schemas.runs import ProxyLocation

_CODE_ONLY_UNAVAILABLE = tuple(
    "action browser_task extraction file_download file_upload goto_url login navigation print_page task task_v2 validation".split()
)
_CODE_ONLY_REQUIRED_TEXT = {
    "file_download": "download registration",
    "file_upload": "attach_authorized_file",
    "login": "credential-typed code",
    "task": "agent-block authoring",
    "task_v2": "declared AI leaf",
}
_CODE_ONLY_HELPERS = tuple(
    "conditional for_loop while_loop http_request send_email file_url_parser download_to_s3 upload_to_s3 google_sheets_read google_sheets_write".split()
)


def _yaml(*blocks: dict) -> str:
    return yaml.safe_dump(
        {"title": "wf", "workflow_definition": {"blocks": list(blocks)}},
        sort_keys=False,
    )


def _ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.authoring_capability = ALL_BLOCK_FAMILIES
    ctx.workflow_yaml = prior_yaml
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    ctx.code_authoring_guardrail_reject_count = 0
    ctx.recorded_build_test_outcome_history = []
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=True)
    ctx.credential_origin_recovery = None
    return ctx


def _code_only_ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = _ctx(prior_yaml=prior_yaml)
    ctx.authoring_capability = CODE_BLOCKS_ONLY
    return ctx


def _task_v3_pure_ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = _ctx(prior_yaml=prior_yaml)
    ctx.authoring_capability = AGENT_BLOCKS_ONLY
    return ctx


def _agent_only_violations(workflow_yaml: str) -> list:
    """Every block in the submitted YAML, validated as newly authored under agent-blocks-only."""
    return [
        violation
        for _label, block in _changed_blocks(workflow_yaml, None)
        for violation in _block_authoring_violations(block, AGENT_BLOCKS_ONLY, recurse=False)
    ]


@pytest.fixture
def ctx() -> MagicMock:
    return _ctx()


@pytest.fixture
def code_only_ctx() -> MagicMock:
    return _code_only_ctx()


# ---------- Pre-emission: SchemaOverlay hooks ----------


@pytest.mark.parametrize("block_type", ["task_v2", "Task_V2", "  task_v2  "])
@pytest.mark.asyncio
async def test_pre_hook_blocks_banned_types_case_and_whitespace_insensitive(block_type: str, ctx: MagicMock) -> None:
    result = await _get_block_schema_pre_hook({"block_type": block_type}, ctx)

    assert result is not None
    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
    for alternative in ("navigation", "extraction", "validation", "login"):
        assert alternative in result["error"]


@pytest.mark.parametrize("block_type", ["task", "TASK", "  task  "])
@pytest.mark.asyncio
async def test_pre_hook_serves_the_task_v3_schema_when_agent_blocks_are_authorable(
    block_type: str, ctx: MagicMock
) -> None:
    result = await _get_block_schema_pre_hook({"block_type": block_type}, ctx)

    assert result is not None
    assert result["ok"] is True
    engine = result["data"]["schema"]["properties"]["engine"]
    assert "const" not in engine and "engine" not in result["data"]["schema"].get("required", [])
    assert "skyvern-3.0" in engine["description"] and "keeps" in engine["description"]


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

    assert "task_v2" not in out["data"]["block_types"]
    assert set(_AGENT_FAMILY_BLOCK_TYPES).issubset(out["data"]["block_types"])
    assert out["data"]["choosing_a_block_family"] == AUTHORING_FAMILY_GUIDANCE


@pytest.mark.asyncio
async def test_the_code_schema_does_not_deny_the_agent_family_when_it_is_authorable(ctx: MagicMock) -> None:
    """The three write tools tell every turn to read this response, so it cannot answer in the voice of
    the mode this stack deletes: under both families it may not describe agent blocks as unavailable."""
    ctx.authoring_capability = ALL_BLOCK_FAMILIES
    result = {"ok": True, "data": {"block_type": "code", "schema": {"properties": {}}}}

    out = await _get_block_schema_post_hook(result, raw={}, ctx=ctx)
    guidance = " ".join(out["data"]["code_only_guidance"])

    assert "code_only_note" not in out["data"]
    assert "browser/page native block types" not in guidance
    assert "Non-browser helper blocks stay available" not in guidance


@pytest.mark.asyncio
async def test_the_listed_code_summary_says_it_drives_the_browser(ctx: MagicMock) -> None:
    """The shared MCP summary calls `code` a data-transformation block, which is the steer this stack
    is changing; a code-capable turn must not be told code is unrelated to browser work."""
    for capability in (ALL_BLOCK_FAMILIES, CODE_BLOCKS_ONLY):
        ctx.authoring_capability = capability
        result = {
            "ok": True,
            "data": {"block_types": {"code": "Run Python code for data transformation"}, "count": 1},
        }

        out = await _get_block_schema_post_hook(result, raw={}, ctx=ctx)

        assert out["data"]["block_types"]["code"] == CODE_BLOCK_SUMMARY
        assert "Playwright" in out["data"]["block_types"]["code"]


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
async def test_task_v3_pure_schema_exposes_real_task_and_catalog() -> None:
    ctx = _task_v3_pure_ctx()

    task_result = await _get_block_schema_pre_hook({"block_type": "task"}, ctx)
    assert task_result is not None
    assert task_result["ok"] is True
    assert task_result["data"]["block_type"] == "task"
    engine = task_result["data"]["schema"]["properties"]["engine"]
    assert "const" not in engine and "default" not in engine
    assert "engine" not in task_result["data"]["schema"].get("required", [])
    assert "skyvern-3.0" in engine["description"] and "keeps" in engine["description"]
    assert any("keeps" in line for line in task_result["data"]["agent_block_guidance"])

    listed = await _get_block_schema_post_hook(
        {"ok": True, "data": {"block_types": {"navigation": "Navigate", "code": "Code"}, "count": 2}},
        raw={},
        ctx=ctx,
    )
    assert set(_AGENT_FAMILY_BLOCK_TYPES).issubset(listed["data"]["block_types"])
    assert "code" not in listed["data"]["block_types"]
    assert "task_v2" not in listed["data"]["block_types"]

    navigation = await _get_block_schema_post_hook(
        {
            "ok": True,
            "data": {
                "block_type": "navigation",
                "schema": {"properties": {"engine": {"$ref": "#/$defs/WorkflowBlockEngine"}}},
            },
        },
        raw={},
        ctx=ctx,
    )
    navigation_engine = navigation["data"]["schema"]["properties"]["engine"]
    assert navigation_engine["type"] == "string" and "const" not in navigation_engine
    assert "keeps" in navigation_engine["description"]
    assert "engine" not in navigation["data"]["schema"].get("required", [])


@pytest.mark.parametrize("block_type", sorted(_AGENT_FAMILY_BLOCK_TYPES))
def test_task_v3_pure_accepts_all_supported_task_types_with_exact_engine(block_type: str) -> None:
    block = {"block_type": block_type, "label": f"{block_type}_block", "engine": "skyvern-3.0"}

    assert _agent_only_violations(_yaml(block)) == []


def test_task_v3_pure_contract_matches_runtime_supported_type_set() -> None:
    assert _AGENT_FAMILY_BLOCK_TYPES == frozenset(block_type.value for block_type in _TASK_V3_SUPPORTED_BLOCK_TYPES)


def test_task_v3_pure_keeps_engine_less_workflow_vocabulary_available() -> None:
    submitted = _yaml(
        {"block_type": "goto_url", "label": "open", "url": "https://example.com"},
        {"block_type": "wait", "label": "wait", "wait_sec": 1},
        {"block_type": "http_request", "label": "request", "url": "https://example.com/api"},
        {"block_type": "google_sheets_read", "label": "sheet", "spreadsheet_id": "sheet"},
        {"block_type": "human_interaction", "label": "review", "recipients": ["reviewer@example.com"]},
    )

    assert _agent_only_violations(submitted) == []


@pytest.mark.parametrize("engine", ["skyvern-1.0", "skyvern-2.0", "openai-cua"])
def test_task_v3_pure_rejects_non_v3_engines(engine: str) -> None:
    block = {"block_type": "navigation", "label": "navigate", "navigation_goal": "Go", "engine": engine}

    violations = _agent_only_violations(_yaml(block))

    assert [violation.code.value for violation in violations] == ["engine_not_skyvern_v3"]


@pytest.mark.parametrize("block_type", ["code", "task_v2"])
def test_task_v3_pure_rejects_unavailable_executor_blocks(block_type: str) -> None:
    violations = _agent_only_violations(_yaml({"block_type": block_type, "label": "unsafe"}))

    assert [violation.code.value for violation in violations] == ["block_type_unavailable"]


def test_task_v3_pure_rejects_nested_legacy_task_and_unsupported_validation() -> None:
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_over_parameter_key": "items",
            "loop_blocks": [
                {"block_type": "task", "label": "nested", "engine": "skyvern-1.0"},
                {
                    "block_type": "validation",
                    "label": "download_validation",
                    "engine": "skyvern-3.0",
                    "complete_on_download": True,
                },
            ],
        }
    )

    violations = _agent_only_violations(submitted)

    assert [(violation.label, violation.code.value) for violation in violations] == [
        ("nested", "engine_not_skyvern_v3"),
        ("download_validation", "unsupported_v3_combination"),
    ]


@pytest.mark.parametrize(
    ("reference", "refused"),
    [
        ("extract_rows", False),
        ("{{ extract_rows_output.extracted_information.rows }}", False),
        ("sites", False),
        ("rows_from_page", True),
        ("the rows currently visible on the page", True),
    ],
)
def test_a_loop_over_data_the_run_holds_is_not_a_synthetic_task(reference: str, refused: bool) -> None:
    """The run only synthesizes an extraction task when the reference resolves to nothing it holds, so an
    earlier block's output or a workflow input passes under every capability and free text does not."""
    submitted = yaml.safe_dump(
        {
            "title": "wf",
            "workflow_definition": {
                "parameters": [{"parameter_type": "workflow", "key": "sites", "workflow_parameter_type": "json"}],
                "blocks": [
                    {"block_type": "extraction", "label": "extract_rows", "data_extraction_goal": "Rows"},
                    {
                        "block_type": "for_loop",
                        "label": "each_row",
                        "loop_variable_reference": reference,
                        "loop_blocks": [{"block_type": "wait", "label": "pause", "wait_sec": 1}],
                    },
                ],
            },
        },
        sort_keys=False,
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, None)

    assert (validation.reject is not None) is refused


def test_task_v3_pure_control_flow_allows_parameter_and_jinja_but_rejects_prompt_paths() -> None:
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "parameter_loop",
            "loop_over_parameter_key": "items",
            "loop_variable_reference": "{{items}}",
            "loop_blocks": [{"block_type": "wait", "label": "wait", "wait_sec": 1}],
        },
        {
            "block_type": "for_loop",
            "label": "prompt_loop",
            "loop_over_parameter_key": "items",
            "loop_variable_reference": "the rows currently visible on the page",
            "loop_blocks": [{"block_type": "wait", "label": "wait_again", "wait_sec": 1}],
        },
        {
            "block_type": "while_loop",
            "label": "jinja_loop",
            "condition": {"criteria_type": "jinja2_template", "expression": "{{ has_more }}"},
            "loop_blocks": [{"block_type": "wait", "label": "wait_more", "wait_sec": 1}],
        },
        {
            "block_type": "conditional",
            "label": "prompt_branch",
            "branch_conditions": [
                {
                    "criteria": {"criteria_type": "prompt", "expression": "Is the page complete?"},
                    "next_block_label": "wait",
                }
            ],
        },
    )

    violations = _agent_only_violations(submitted)

    assert [(violation.label, violation.code.value) for violation in violations] == [
        ("prompt_loop", "synthetic_task_control_flow"),
        ("prompt_branch", "synthetic_task_control_flow"),
    ]


def test_task_v3_pure_control_flow_honors_default_jinja_criteria_type() -> None:
    submitted = _yaml(
        {
            "block_type": "while_loop",
            "label": "default_jinja_loop",
            "condition": {"expression": "{{ has_more }}"},
            "loop_blocks": [{"block_type": "wait", "label": "wait", "wait_sec": 1}],
        },
        {
            "block_type": "conditional",
            "label": "default_jinja_branch",
            "branch_conditions": [
                {
                    "criteria": {"expression": "{{ should_continue }}"},
                    "next_block_label": "wait",
                }
            ],
        },
    )

    assert _agent_only_violations(submitted) == []


@pytest.mark.asyncio
async def test_task_v3_pure_validate_block_uses_the_same_structural_contract() -> None:
    ctx = _task_v3_pure_ctx()

    rejected = await _validate_block_pre_hook(
        {
            "block_json": json.dumps(
                {"block_type": "navigation", "label": "navigate", "navigation_goal": "Go", "engine": "skyvern-1.0"}
            )
        },
        ctx,
    )
    accepted = await _validate_block_pre_hook(
        {
            "block_json": json.dumps(
                {
                    "block_type": "navigation",
                    "label": "navigate",
                    "navigation_goal": "Go",
                    "engine": "skyvern-3.0",
                }
            )
        },
        ctx,
    )

    assert rejected is not None
    assert rejected["data"]["violations"][0]["code"] == "engine_not_skyvern_v3"
    assert accepted is None


@pytest.mark.asyncio
async def test_task_v3_pure_update_rejects_raw_foreign_engine_before_conversion() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "navigate", "navigation_goal": "Go", "engine": "skyvern-1.0"}
    )
    ctx = _task_v3_pure_ctx()

    with patch("skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml") as process:
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert result["block_id"] == "banned_blocks"
    assert result["data"]["violations"][0]["code"] == "engine_not_skyvern_v3"
    process.assert_not_called()


@pytest.mark.asyncio
async def test_update_workflow_converts_the_engine_pinned_draft_when_the_engine_was_omitted() -> None:
    submitted = _yaml({"block_type": "navigation", "label": "navigate", "navigation_goal": "Go"})
    ctx = _task_v3_pure_ctx()

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            side_effect=RuntimeError("stop here"),
        ) as process,
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app"),
        pytest.raises(RuntimeError),
    ):
        await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert _engines_by_label(process.call_args.kwargs["workflow_yaml"]) == {"navigate": "skyvern-3.0"}


@pytest.mark.asyncio
async def test_task_v3_pure_update_preserves_submitted_v3_engine_bytes() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "navigate", "navigation_goal": "Go", "engine": "skyvern-3.0"}
    )
    ctx = _task_v3_pure_ctx()
    fake_workflow = MagicMock()
    for attr in (
        "title",
        "description",
        "workflow_definition",
        "proxy_location",
        "webhook_callback_url",
        "persist_browser_session",
        "model",
        "max_screenshot_scrolls",
        "extra_http_headers",
        "run_with",
        "ai_fallback",
        "cache_key",
        "run_sequentially",
        "sequential_key",
    ):
        setattr(fake_workflow, attr, None)

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            return_value=fake_workflow,
        ) as process,
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(return_value=None)
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is True
    assert process.await_args.kwargs["workflow_yaml"] == submitted
    assert ctx.workflow_yaml == submitted


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

    assert "unavailable while only code may be authored" in out["data"]["code_only_note"]
    assert "validate_block is only for allowed non-browser helper blocks" in " ".join(out["data"]["code_only_guidance"])
    assert "Do not persist navigation/action/login" not in " ".join(out["data"]["code_only_guidance"])


def test_code_schema_guidance_advertises_only_the_authorized_file_attachment_helper() -> None:
    guidance = " ".join(_code_only_browser_schema_guidance())

    assert "attach_authorized_file(page, <file_parameter>, <observed_selector>)" in guidance
    assert "pass await info.value to the same helper" in guidance
    assert "set_input_files" not in guidance


def test_code_schema_guidance_advertises_clear_browser_data_instead_of_browser_settings_pages() -> None:
    guidance = " ".join(_code_only_browser_schema_guidance())

    assert "await clear_browser_data(page)" in guidance
    assert "chrome://" in guidance
    assert "clear_cookies" not in guidance


def test_code_only_schema_guidance_exposes_credential_runtime_without_otp_procedure() -> None:
    guidance = "\n".join(_code_only_browser_schema_guidance())

    assert "<key>.username" in guidance
    assert "<key>.password" in guidance
    assert "await <key>.otp()" in guidance
    assert "await <key>.magic_link(page)" in guidance
    assert "solve_captcha(page)" in guidance
    assert "Do not read `email_inbox`" not in guidance
    assert "authenticated-page anchor" not in guidance
    assert "Transient disappearance of the OTP field" not in guidance


def test_code_only_schema_guidance_exposes_secret_credential_runtime_accessor() -> None:
    lines = _code_only_browser_schema_guidance()
    secret_line = next(line for line in lines if line.startswith("A `secret` credential"))
    password_line = next(line for line in lines if line.startswith("For saved credentials"))

    assert "<key>.secret_value" in secret_line
    assert "otp()" not in secret_line
    assert "magic_link" not in secret_line
    assert "fill_credential_field" not in secret_line
    assert "<key>.secret_value" not in password_line
    assert "fill_credential_field" in password_line
    for accessors in CREDENTIAL_CODE_ACCESSORS.values():
        for accessor in (*accessors.fields, accessors.otp, accessors.magic_link):
            assert accessor is None or accessor in "\n".join(lines)


def test_code_only_schema_guidance_states_the_cold_run_starting_condition() -> None:
    guidance = "\n".join(_code_only_browser_schema_guidance())
    entry = next(line for line in _code_only_browser_schema_guidance() if line.startswith("A saved run executes"))

    assert "without the interactions performed while scouting" in guidance
    for prescription in ("modal", "dialog", "consent", "cookie", "banner", "overlay", "dismiss", "wait", "click", "if"):
        # Inflections included: a bare-stem match passes "the block waits", the prescriptive mood
        # this guard exists to reject.
        assert re.search(rf"\b{prescription}(s|es|ed|ing)?\b", entry.lower()) is None
    assert "#" not in entry


@pytest.mark.asyncio
async def test_unified_validate_block_pre_hook_admits_a_pinned_agent_block_and_rejects_task_v2(
    ctx: MagicMock,
) -> None:
    pinned = '{"block_type": "task", "label": "x", "engine": "skyvern-3.0"}'
    assert await _validate_block_pre_hook({"block_json": pinned}, ctx) is None

    assert await _validate_block_pre_hook({"block_json": '{"block_type": "task", "label": "x"}'}, ctx) is None

    foreign = await _validate_block_pre_hook(
        {"block_json": '{"block_type": "task", "label": "x", "engine": "skyvern-1.0"}'}, ctx
    )
    assert foreign is not None
    assert "skyvern-3.0" in foreign["error"]

    legacy = await _validate_block_pre_hook({"block_json": '{"block_type": "task_v2", "label": "x"}'}, ctx)
    assert legacy is not None
    assert "may not author" in legacy["error"]


@pytest.mark.parametrize("block_type", _CODE_ONLY_UNAVAILABLE + (" LOGIN ", "BROWSER_TASK"))
@pytest.mark.asyncio
async def test_code_only_validate_block_pre_hook_rejects_unavailable_types(
    block_type: str, code_only_ctx: MagicMock
) -> None:
    result = await _validate_block_pre_hook(
        {"block_json": f'{{"block_type": "{block_type}", "label": "candidate"}}'},
        code_only_ctx,
    )

    assert result is not None
    assert result["ok"] is False
    assert "may not author" in result["error"]


@pytest.mark.asyncio
async def test_code_only_validate_block_pre_hook_allows_code_validation(code_only_ctx: MagicMock) -> None:
    result = await _validate_block_pre_hook(
        {"block_json": '{"block_type": "code", "label": "candidate", "code": "return {}"}'},
        code_only_ctx,
    )

    assert result is None


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


_NAV_BLOCK = (
    '{"block_type": "navigation", "label": "x", "url": "https://e.com", '
    '"navigation_goal": "g", "engine": "skyvern-3.0"}'
)


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
    block = {
        "block_type": "navigation",
        "label": "x",
        "url": "https://e.com",
        "navigation_goal": "g",
        "engine": "skyvern-3.0",
    }
    params: dict = {"block": block}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert json.loads(params["block_json"]) == block
    assert "block" not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_strips_stray_alias_without_clobbering_block_json(ctx: MagicMock) -> None:
    params = {"block_json": _NAV_BLOCK, "block": '{"block_type": "extraction", "label": "y", "engine": "skyvern-3.0"}'}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert params["block_json"] == _NAV_BLOCK
    assert "block" not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_normalizes_code_alias_without_refusing(code_only_ctx: MagicMock) -> None:
    params = {"block": '{"block_type": "code", "label": "x", "code": "pass"}'}

    result = await _validate_block_pre_hook(params, code_only_ctx)

    assert result is None
    assert params["block_json"]
    assert "block" not in params


@pytest.mark.asyncio
async def test_validate_block_pre_hook_alias_allows_helper_under_code_only(code_only_ctx: MagicMock) -> None:
    params = {"block": '{"block_type": "conditional", "label": "x"}'}

    result = await _validate_block_pre_hook(params, code_only_ctx)

    assert result is None
    assert params["block_json"] == '{"block_type": "conditional", "label": "x"}'


# ---------- Post-emission: YAML-level detector ----------


def test_raw_yaml_proxy_location_reports_absent_value() -> None:
    assert _raw_yaml_proxy_location(_yaml({"block_type": "navigation", "label": "n"})) == (False, None)


def test_raw_yaml_proxy_location_reports_explicit_values() -> None:
    assert _raw_yaml_proxy_location("title: wf\nproxy_location: US\n") == (True, "US")
    assert _raw_yaml_proxy_location("title: wf\nproxy_location: null\n") == (True, None)


def test_proxy_location_trace_value_serializes_enum_values() -> None:
    assert _proxy_location_trace_value(ProxyLocation.RESIDENTIAL) == "RESIDENTIAL"


# ---------- Flat shapes ----------


@pytest.mark.asyncio
async def test_update_workflow_rejects_new_task_block_and_emits_span() -> None:
    submitted = _yaml(
        {"block_type": "task", "label": "fill_contact_form", "navigation_goal": "do", "engine": "skyvern-1.0"}
    )
    ctx = _ctx(prior_yaml=None)

    with patch("skyvern.forge.sdk.copilot.tools.banned_blocks._record_banned_block_reject_span") as mock_span:
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert "may not author" in result["error"]
    assert "fill_contact_form" in result["error"]
    for alternative in ("navigation", "extraction", "validation", "login"):
        assert alternative in result["error"]

    # Dedicated span fired with source_tool + items for logfire trend analysis.
    mock_span.assert_called_once_with("_update_workflow", [("fill_contact_form", "task")])


@pytest.mark.asyncio
async def test_update_workflow_preserves_legacy_task_block_under_unchanged_label() -> None:
    """Copilot edit of a legacy workflow that already carries a ``task`` block
    must not fail the reject. The helper sees the task label in prior YAML
    and treats its re-emission as legacy preservation, not a new addition."""
    prior = _yaml({"block_type": "task", "label": "legacy_task", "navigation_goal": "old"})
    # New YAML preserves the legacy task block AND adds an allowed-type block.
    submitted = _yaml(
        {"block_type": "task", "label": "legacy_task", "navigation_goal": "old"},
        {"block_type": "navigation", "label": "new_nav", "navigation_goal": "new", "engine": "skyvern-3.0"},
    )
    ctx = _ctx(prior_yaml=prior)

    fake_workflow = MagicMock()
    fake_workflow.title = "t"
    fake_workflow.description = "d"
    fake_workflow.workflow_definition = MagicMock()
    fake_workflow.proxy_location = None
    fake_workflow.webhook_callback_url = None
    fake_workflow.persist_browser_session = False
    fake_workflow.model = None
    fake_workflow.max_screenshot_scrolls = None
    fake_workflow.extra_http_headers = None
    fake_workflow.run_with = None
    fake_workflow.ai_fallback = None
    fake_workflow.cache_key = None
    fake_workflow.run_sequentially = None
    fake_workflow.sequential_key = None

    with (
        patch("skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml", return_value=fake_workflow),
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update._record_workflow_proxy_location_span"
        ) as mock_proxy_span,
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(return_value=None)
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is True
    mock_proxy_span.assert_called_once_with(submitted, fake_workflow)
    # The new YAML was accepted and assigned to ctx as the current workflow state.
    assert ctx.workflow_yaml == submitted


@pytest.mark.asyncio
async def test_update_workflow_allows_all_allowed_block_types() -> None:
    """Baseline success path: only allowed block types, no prior — passes through."""
    submitted = _yaml(
        {"block_type": "navigation", "label": "n", "navigation_goal": "x", "engine": "skyvern-3.0"},
        {"block_type": "validation", "label": "v", "complete_criterion": "c", "engine": "skyvern-3.0"},
    )
    ctx = _ctx(prior_yaml=None)

    fake_workflow = MagicMock()
    for attr in (
        "title",
        "description",
        "workflow_definition",
        "proxy_location",
        "webhook_callback_url",
        "persist_browser_session",
        "model",
        "max_screenshot_scrolls",
        "extra_http_headers",
        "run_with",
        "ai_fallback",
        "cache_key",
        "run_sequentially",
        "sequential_key",
    ):
        setattr(fake_workflow, attr, None)

    with (
        patch("skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml", return_value=fake_workflow),
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(return_value=None)
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_code_only_update_workflow_rejects_new_browser_block_with_policy_text() -> None:
    submitted = _yaml({"block_type": "login", "label": "login_step"})
    ctx = _code_only_ctx(prior_yaml=None)

    with patch("skyvern.forge.sdk.copilot.tools.banned_blocks._record_banned_block_reject_span") as mock_span:
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert "may not author" in result["error"]
    assert "credential-typed code" in result["error"]
    mock_span.assert_called_once_with("_update_workflow", [("login_step", "login")])


# ---------- Cross-layer sync guard ----------


@pytest.mark.asyncio
async def test_recipient_less_human_interaction_is_not_refused_at_author_time(
    ctx: MagicMock, code_only_ctx: MagicMock
) -> None:
    """Author-time refusal is a closed set of three hard blocks (workflow-copilot decision 0022);
    ``human_interaction`` is not in it under either authoring policy, even with no recipient."""
    block_json = json.dumps({"block_type": "human_interaction", "label": "approve_step", "recipients": []})

    for policy_ctx in (ctx, code_only_ctx):
        assert await _validate_block_pre_hook({"block_json": block_json}, policy_ctx) is None

    assert "human_interaction" not in _COPILOT_BANNED_BLOCK_TYPES
    assert "human_interaction" not in _CODE_BLOCKS_ONLY_BANNED_BLOCK_TYPES


# ---------- Diff-aware authoring validator ----------


_LEGACY_AGENT_WORKFLOW = _yaml(
    {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old", "engine": "skyvern-1.0"},
    {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
)


def _accepted(capability: object, submitted: str, prior: str | None) -> object:
    ctx = _ctx(prior_yaml=prior)
    ctx.authoring_capability = capability
    return reject_authoring_violations(ctx, submitted, "test")


def test_untouched_legacy_engine_blocks_pass_and_are_reported_as_fact() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old", "engine": "skyvern-1.0"},
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
        {"block_type": "validation", "label": "new_check", "complete_criterion": "c", "engine": "skyvern-3.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is None
    assert validation.legacy_engine_blocks == ("legacy_extract", "legacy_nav")


def test_rewriting_a_legacy_block_engine_off_v3_rejects_it() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old", "engine": "skyvern-2.0"},
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is not None
    assert "legacy_nav" in validation.reject.error
    assert "skyvern-3.0" in validation.reject.error


def test_renaming_a_legacy_block_is_not_a_re_authoring() -> None:
    """A rename asks for a new label, not new content. Refusing it pushes the model to migrate a
    working block's engine to recover, which is a change the user never asked for."""
    submitted = _yaml(
        {"block_type": "navigation", "label": "sign_in_step", "navigation_goal": "old", "engine": "skyvern-1.0"},
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is None


def test_replacing_a_legacy_block_with_an_unrelated_one_of_the_same_type_is_not_a_rename() -> None:
    """Rename detection may only match on the block's content. Type and engine alone would let a
    rewrite delete legacy block A and add unrelated block B of the same type without the engine pin."""
    submitted = _yaml(
        {"block_type": "navigation", "label": "search_orders", "navigation_goal": "new", "engine": "skyvern-1.0"},
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is not None
    assert "search_orders" in validation.reject.error
    assert "skyvern-3.0" in validation.reject.error


def test_a_deleted_legacy_engine_block_is_not_reported_as_still_present() -> None:
    submitted = _yaml(
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is None
    assert validation.legacy_engine_blocks == ("legacy_extract",)


def test_editing_only_a_goal_leaves_a_legacy_engine_block_grandfathered() -> None:
    """The differ inspects exactly the fields the validator reads, so a goal-text edit on a legacy
    block is not a re-authoring and does not force its engine migration."""
    submitted = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "new", "engine": "skyvern-1.0"},
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is None
    assert validation.legacy_engine_blocks == ("legacy_extract", "legacy_nav")


def test_rewriting_a_legacy_code_block_is_refused_without_code_access() -> None:
    """Grandfathering carries an untouched code block past a capability that bans it; it does not
    license writing new code into one. An anchored code edit touches no field the pin reads."""
    prior = _yaml({"block_type": "code", "label": "scrape", "code": "return {'a': 1}", "prompt": "Read a"})
    submitted = _yaml({"block_type": "code", "label": "scrape", "code": "return {'b': 2}", "prompt": "Read a"})

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, prior)

    assert validation.reject is not None
    assert "scrape" in validation.reject.error


@pytest.mark.parametrize("resent_code", ["return {'a': 1}", "return {'a': 1}\n"])
def test_an_untouched_legacy_code_block_still_passes_without_code_access(resent_code: str) -> None:
    """A `code: |` resubmission of the same code carries the trailing newline a `|-` draft does not."""
    prior = _yaml({"block_type": "code", "label": "scrape", "code": "return {'a': 1}", "prompt": "Read a"})
    submitted = _yaml(
        {"block_type": "code", "label": "scrape", "code": resent_code, "prompt": "Read a"},
        {"block_type": "navigation", "label": "new_nav", "navigation_goal": "Go", "engine": "skyvern-3.0"},
    )

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, prior)

    assert validation.reject is None


def test_server_added_fields_on_an_untouched_banned_block_are_not_an_edit() -> None:
    """The prior YAML is the canonicalized draft, so a resubmission that omits what the server added
    contradicts nothing and is not a rewrite."""
    prior = _yaml(
        {
            "block_type": "code",
            "label": "scrape",
            "code": "return {'a': 1}",
            "parameter_keys": ["site_url"],
            "cache_key": "server-assigned",
        }
    )
    submitted = _yaml({"block_type": "code", "label": "scrape", "code": "return {'a': 1}"})

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, prior)

    assert validation.reject is None


def test_rewriting_an_agent_block_goal_is_refused_when_only_code_may_be_authored() -> None:
    prior = _yaml({"block_type": "navigation", "label": "step", "navigation_goal": "old", "engine": "skyvern-3.0"})
    submitted = _yaml({"block_type": "navigation", "label": "step", "navigation_goal": "new", "engine": "skyvern-3.0"})

    validation = _accepted(CODE_BLOCKS_ONLY, submitted, prior)

    assert validation.reject is not None


def test_resubmitting_the_prior_draft_unchanged_changes_nothing() -> None:
    validation = _accepted(AGENT_BLOCKS_ONLY, _LEGACY_AGENT_WORKFLOW, _LEGACY_AGENT_WORKFLOW)

    assert validation.reject is None
    assert validation.workflow_yaml == _LEGACY_AGENT_WORKFLOW
    assert _changed_blocks(_LEGACY_AGENT_WORKFLOW, _LEGACY_AGENT_WORKFLOW) == []


def _engines_by_label(workflow_yaml: str) -> dict[str, object]:
    blocks = yaml.safe_load(workflow_yaml)["workflow_definition"]["blocks"]
    flat: list[dict] = []
    for block in blocks:
        flat.append(block)
        flat.extend(block.get("loop_blocks") or [])
    return {block["label"]: block.get("engine") for block in flat}


def test_an_agent_block_that_names_no_engine_is_pinned_to_v3_at_the_write_seam() -> None:
    """Copilot-authored agent blocks only ever run on skyvern-3.0, so an omitted engine is filled in
    rather than refused: a refusal here only ever produced a resubmission with the same pin."""
    submitted = _yaml(
        {"block_type": "navigation", "label": "navigate", "navigation_goal": "Go"},
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_over_parameter_key": "items",
            "loop_blocks": [{"block_type": "extraction", "label": "nested", "data_extraction_goal": "Get"}],
        },
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, None)

    assert validation.reject is None
    assert _engines_by_label(validation.workflow_yaml) == {
        "navigate": "skyvern-3.0",
        "loop": None,
        "nested": "skyvern-3.0",
    }


def test_an_agent_block_naming_another_engine_is_still_refused() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "navigate", "navigation_goal": "Go", "engine": "skyvern-1.0"}
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, None)

    assert validation.reject is not None
    assert "skyvern-3.0" in validation.reject.error


def test_an_untouched_engine_less_legacy_block_is_not_pinned() -> None:
    """Filling the engine on a block the turn never touched is the unasked engine migration the
    differ exists to prevent."""
    prior = _yaml({"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old"})
    submitted = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old"},
        {"block_type": "extraction", "label": "new_extract", "data_extraction_goal": "Get"},
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, prior)

    assert validation.reject is None
    assert _engines_by_label(validation.workflow_yaml) == {"legacy_nav": None, "new_extract": "skyvern-3.0"}
    assert validation.legacy_engine_blocks == ("legacy_nav",)


def test_a_legacy_engine_a_whole_document_write_omits_is_restored_not_upgraded() -> None:
    """Leaving the field out is how a whole-document write carries a block it did not touch. Reading
    that as a migration request moves a block the user never asked about onto another engine."""
    prior = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old", "engine": "skyvern-1.0"}
    )
    submitted = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old"},
        {"block_type": "extraction", "label": "new_extract", "data_extraction_goal": "Get"},
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, prior)

    assert validation.reject is None
    assert _engines_by_label(validation.workflow_yaml) == {
        "legacy_nav": "skyvern-1.0",
        "new_extract": "skyvern-3.0",
    }


def test_parameter_binding_on_an_untouched_block_is_not_a_change() -> None:
    prior = _yaml(
        {
            "block_type": "navigation",
            "label": "nav",
            "navigation_goal": "go",
            "engine": "skyvern-1.0",
            "parameter_keys": ["site_url"],
            "cache_key": "server-assigned",
        }
    )
    resubmitted = _yaml({"block_type": "navigation", "label": "nav", "navigation_goal": "go", "engine": "skyvern-1.0"})

    assert _changed_blocks(resubmitted, prior) == []


def test_a_type_change_under_the_same_label_is_rejected_without_code_access() -> None:
    prior = _yaml({"block_type": "navigation", "label": "step", "navigation_goal": "go", "engine": "skyvern-3.0"})
    submitted = _yaml({"block_type": "code", "label": "step", "code": "return {}"})

    validation = _accepted(AGENT_BLOCKS_ONLY, submitted, prior)

    assert validation.reject is not None
    assert "skyvern-3.0" in validation.reject.error


def test_mixed_code_and_agent_blocks_are_accepted_when_both_families_are_authorable() -> None:
    submitted = _yaml(
        {"block_type": "code", "label": "open_site", "code": "return {}", "prompt": "g", "code_artifact_metadata": {}},
        {
            "block_type": "for_loop",
            "label": "per_site",
            "loop_over_parameter_key": "sites",
            "loop_blocks": [
                {
                    "block_type": "extraction",
                    "label": "find_email",
                    "data_extraction_goal": "support email",
                    "engine": "skyvern-3.0",
                }
            ],
        },
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, None)

    assert validation.reject is None
    assert validation.findings == ()


def test_a_new_code_block_without_a_goal_is_a_finding_not_a_reject() -> None:
    submitted = _yaml({"block_type": "code", "label": "open_site", "code": "return {}"})

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, None)

    assert validation.reject is None
    assert "open_site" in validation.findings[0]
    assert "`prompt`" in validation.findings[0]


def test_a_nested_prompt_criteria_conditional_is_rejected() -> None:
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_over_parameter_key": "items",
            "loop_blocks": [
                {
                    "block_type": "conditional",
                    "label": "branch",
                    "branch_conditions": [{"criteria": {"criteria_type": "prompt"}}],
                }
            ],
        }
    )

    validation = _accepted(ALL_BLOCK_FAMILIES, submitted, None)

    assert validation.reject is not None
    assert "branch" in validation.reject.error


def test_authoring_turn_summary_names_what_the_turn_added() -> None:
    final = _yaml(
        {"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old", "engine": "skyvern-1.0"},
        {"block_type": "extraction", "label": "legacy_extract", "data_extraction_goal": "old", "engine": "skyvern-1.0"},
        {"block_type": "code", "label": "fetch", "code": "return {}"},
    )

    assert authoring_turn_summary(_LEGACY_AGENT_WORKFLOW, final) == {
        "introduced_code_blocks": ["fetch"],
        "introduced_agent_blocks": {},
        "block_type_changes": {},
        "mid_turn_family_switches": {},
        "switches_after_failed_test": [],
        "same_family_rewrites_after_failed_test": {},
    }


def test_repeated_same_family_rewrites_after_failed_tests_are_counted() -> None:
    """Under-switching is a block rewritten in its own family after each failed test; the type never
    changes, so only a count of those rewrites makes it visible."""
    ctx = _ctx(prior_yaml=None)
    ctx.authored_block_families = {}
    ctx.recorded_build_test_outcome_history = []
    first = _yaml({"block_type": "code", "label": "read_balance", "code": "return {'a': 1}"})
    reject_authoring_violations(ctx, first, "test")
    prior = first
    for attempt in range(2):
        ctx.recorded_build_test_outcome_history.append(
            {"verdict": "repairable_failure", "block_labels": ["read_balance"]}
        )
        rewrite = _yaml({"block_type": "code", "label": "read_balance", "code": f"return {{'b': {attempt}}}"})
        reject_authoring_violations(ctx, rewrite, "test", prior_workflow_yaml=prior)
        prior = rewrite

    summary = authoring_turn_summary(None, prior, authored_block_families=ctx.authored_block_families)

    assert summary["same_family_rewrites_after_failed_test"] == {"read_balance": 2}
    assert summary["mid_turn_family_switches"] == {}


def test_a_block_that_changes_family_between_writes_in_one_turn_is_recorded_with_whether_a_test_failed_first() -> None:
    """Start-versus-end diffing cannot see a block introduced as code, failed, and rewritten as an
    agent block in the same turn; the persist seam records the family on every accepted write."""
    ctx = _ctx(prior_yaml=None)
    ctx.authored_block_families = {}
    ctx.recorded_build_test_outcome_history = []
    first = _yaml({"block_type": "code", "label": "read_balance", "code": "return {}"})
    reject_authoring_violations(ctx, first, "test")
    ctx.recorded_build_test_outcome_history.append({"verdict": "repairable_failure", "block_labels": ["read_balance"]})
    second = _yaml({"block_type": "extraction", "label": "read_balance", "data_extraction_goal": "balance"})
    reject_authoring_violations(ctx, second, "test", prior_workflow_yaml=first)
    third = _yaml(
        {"block_type": "extraction", "label": "read_balance", "data_extraction_goal": "balance"},
        {"block_type": "code", "label": "total", "code": "return {}"},
    )
    reject_authoring_violations(ctx, third, "test", prior_workflow_yaml=second)

    summary = authoring_turn_summary(None, third, authored_block_families=ctx.authored_block_families)

    assert summary["mid_turn_family_switches"] == {"read_balance": "code->extraction"}
    assert summary["switches_after_failed_test"] == ["read_balance"]
    assert summary["introduced_agent_blocks"] == {"read_balance": "extraction"}


# ---------- Rendered tool surface ----------


@pytest.mark.parametrize(
    ("capability", "carries_guidance"),
    [(ALL_BLOCK_FAMILIES, True), (AGENT_BLOCKS_ONLY, False), (CODE_BLOCKS_ONLY, False)],
)
def test_authoring_guidance_reaches_the_three_write_tools_only_under_both_families(
    capability: object, carries_guidance: bool
) -> None:
    tools = {
        tool.name: tool
        for tool in copilot_native_tools(
            supports_question_tool=True,
            browser_code_available=True,
            authoring_capability=capability,
        )
    }

    for name in ("add_block", "update_workflow", "update_and_run_blocks"):
        assert (AUTHORING_FAMILY_GUIDANCE in tools[name].description) is carries_guidance
    assert AUTHORING_FAMILY_GUIDANCE not in tools["edit_block"].description


@pytest.mark.parametrize("capability", [ALL_BLOCK_FAMILIES, AGENT_BLOCKS_ONLY, CODE_BLOCKS_ONLY])
def test_read_the_schema_first_reaches_the_write_tools_under_every_capability(capability: object) -> None:
    """The deleted code-mode prompt carried this instruction always-in-context, and the runtime facts it
    used to state now only come back from get_block_schema. A single-family turn still has to be told to
    ask for them."""
    tools = {
        tool.name: tool
        for tool in copilot_native_tools(
            supports_question_tool=True,
            browser_code_available=True,
            authoring_capability=capability,
        )
    }

    for name in ("add_block", "update_workflow", "update_and_run_blocks"):
        assert SCHEMA_FIRST_GUIDANCE in tools[name].description


def test_agent_blocks_only_keeps_direct_browser_scouting_without_the_code_tool() -> None:
    names = {
        tool.name
        for tool in copilot_native_tools(
            supports_question_tool=True,
            browser_code_available=False,
            authoring_capability=AGENT_BLOCKS_ONLY,
        )
    }

    assert "run_browser_code" not in names
    assert {"inspect_page_for_composition", "inspect_locator_matches"} <= names


@pytest.mark.parametrize("capability", [ALL_BLOCK_FAMILIES, AGENT_BLOCKS_ONLY, CODE_BLOCKS_ONLY])
@pytest.mark.asyncio
async def test_every_authoring_surface_agrees_with_the_two_booleans(capability: AuthoringCapability) -> None:
    ctx = _ctx()
    ctx.authoring_capability = capability

    code_schema = await _get_block_schema_pre_hook({"block_type": "code"}, ctx)
    task_schema = await _get_block_schema_pre_hook({"block_type": "task"}, ctx)
    code_validate = await _validate_block_pre_hook(
        {"block_json": '{"block_type": "code", "label": "c", "code": "return {}"}'}, ctx
    )
    code_write = reject_authoring_violations(
        ctx, _yaml({"block_type": "code", "label": "c", "code": "return {}"}), "test"
    )

    assert ("code" not in _banned_block_types_for_capability(capability)) is capability.code_blocks
    assert (code_schema is None) is capability.code_blocks
    assert (code_validate is None) is capability.code_blocks
    assert (code_write.reject is None) is capability.code_blocks
    assert (task_schema["ok"] is True) is capability.agent_blocks
    if code_write.reject is not None:
        assert "`skyvern-3.0`" in code_write.reject.error


def test_banned_sets_shrink_as_families_are_added() -> None:
    assert _banned_block_types_for_capability(ALL_BLOCK_FAMILIES) == {"task_v2"}
    assert "code" in _banned_block_types_for_capability(AGENT_BLOCKS_ONLY)
    assert "code" not in _banned_block_types_for_capability(CODE_BLOCKS_ONLY)
    assert _AGENT_FAMILY_BLOCK_TYPES <= _banned_block_types_for_capability(CODE_BLOCKS_ONLY)


# ---------- Code authoring is granted, never assumed ----------


def test_an_unstated_policy_authors_no_code_at_every_resolution_site() -> None:
    assert authoring_capability_from_policy(None) == AGENT_BLOCKS_ONLY
    assert _normalized_authoring_capability(None) == AGENT_BLOCKS_ONLY
    assert _copilot_authoring_capability(None) == AGENT_BLOCKS_ONLY
    assert _copilot_authoring_capability(SimpleNamespace()) == AGENT_BLOCKS_ONLY
    assert CopilotConfig().authoring_capability == AGENT_BLOCKS_ONLY
    assert (
        AgentContext(
            organization_id="o_test",
            workflow_id="w_test",
            workflow_permanent_id="wpid_test",
            workflow_yaml="",
            browser_session_id=None,
            stream=None,  # type: ignore[arg-type]
        ).authoring_capability
        == AGENT_BLOCKS_ONLY
    )


def test_a_default_context_rejects_a_code_block_naming_the_agent_alternative() -> None:
    ctx = AgentContext(
        organization_id="o_test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml="",
        browser_session_id=None,
        stream=None,  # type: ignore[arg-type]
    )

    validation = reject_authoring_violations(
        ctx, _yaml({"block_type": "code", "label": "step", "code": "return {}"}), "test"
    )

    assert validation.reject is not None
    assert "`skyvern-3.0`" in validation.reject.error


def test_a_carrier_holding_a_non_policy_value_authors_no_code() -> None:
    assert _copilot_authoring_capability(SimpleNamespace(block_authoring_policy=object())) == AGENT_BLOCKS_ONLY
    assert _copilot_authoring_capability(MagicMock(spec=AgentContext)) == AGENT_BLOCKS_ONLY


def test_a_policy_string_nobody_recognises_authors_no_code() -> None:
    for unrecognised in ("", "standrad", "code_only", "CODE_ONLY_BROWSER"):
        assert authoring_capability_from_policy(unrecognised) == AGENT_BLOCKS_ONLY
        assert _copilot_authoring_capability(SimpleNamespace(block_authoring_policy=unrecognised)) == AGENT_BLOCKS_ONLY
