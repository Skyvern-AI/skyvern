"""Tests for the copilot-v2 policy that bans ``task`` / ``task_v2`` (and, under
the code-only-browser policy, the wider browser block family) at every copilot
write surface (SKY-9174).

Two layers are covered here:

* Pre-emission — the ``SchemaOverlay`` pre / post hooks and ``validate_block``
  hook (Part C.1) block the types at the schema-lookup surface.
* Post-emission — the LLM can bypass the schema surface by writing YAML
  directly, so ``_detect_new_banned_blocks`` + ``_update_workflow`` /
  ``REPLACE_WORKFLOW`` (Part F) close the bypass with a YAML-level reject keyed
  by block label, so legacy workflows with pre-existing ``task`` blocks can
  still be edited by the copilot.

Both layers import ``_COPILOT_BANNED_BLOCK_TYPES`` from the same module; the
cross-layer sync-guard test at the end asserts neither symbol is ripped out.
"""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.tools import (
    _COPILOT_BANNED_BLOCK_TYPES,
    _banned_block_reject_message,
    _detect_new_banned_blocks,
    _get_block_schema_post_hook,
    _get_block_schema_pre_hook,
    _proxy_location_trace_value,
    _raw_yaml_proxy_location,
    _update_workflow,
)
from skyvern.forge.sdk.copilot.tools.banned_blocks import (
    _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES,
    CopilotBlockPolicyStatus,
    _code_only_browser_authoring_prompt,
)
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _validate_block_pre_hook
from skyvern.schemas.runs import ProxyLocation

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


def _yaml(*blocks: dict) -> str:
    return yaml.safe_dump(
        {"title": "wf", "workflow_definition": {"blocks": list(blocks)}},
        sort_keys=False,
    )


def _ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.block_authoring_policy = BlockAuthoringPolicy.STANDARD
    ctx.workflow_yaml = prior_yaml
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    ctx.code_authoring_guardrail_reject_count = 0
    ctx.recorded_build_test_outcome_history = []
    return ctx


def _code_only_ctx(prior_yaml: str | None = None) -> MagicMock:
    ctx = _ctx(prior_yaml=prior_yaml)
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    return ctx


@pytest.fixture
def ctx() -> MagicMock:
    return _ctx()


@pytest.fixture
def code_only_ctx() -> MagicMock:
    return _code_only_ctx()


# ---------- Pre-emission: SchemaOverlay hooks ----------


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


def test_code_only_authoring_prompt_does_not_recommend_blocked_page_evaluate() -> None:
    prompt = _code_only_browser_authoring_prompt()

    assert "`evaluate`" not in prompt
    assert "locator" in prompt
    assert "MCP/scout evidence" in prompt


def test_code_only_authoring_prompt_requires_idempotent_credential_login() -> None:
    prompt = _code_only_browser_authoring_prompt()

    assert "Credentialed login code must be idempotent" in prompt
    assert "already-authenticated page anchor" in prompt
    assert "only fill username/password" in prompt
    assert "login fields are visible" in prompt


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
async def test_validate_block_pre_hook_inspects_misnamed_arg_without_mutating(alias: str, ctx: MagicMock) -> None:
    """SKY-11133: the model calls validate_block with the block under a shorter
    key (e.g. `block`). The pre-hook inspects a normalized copy for policy while
    shared FastMCP middleware owns the actual repair."""
    params = {alias: _NAV_BLOCK}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert params == {alias: _NAV_BLOCK}


@pytest.mark.asyncio
async def test_validate_block_pre_hook_inspects_dict_alias_without_mutating(ctx: MagicMock) -> None:
    block = {"block_type": "navigation", "label": "x", "url": "https://e.com", "navigation_goal": "g"}
    params: dict = {"block": block}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert params == {"block": block}


@pytest.mark.asyncio
async def test_validate_block_pre_hook_preserves_conflicting_alias(ctx: MagicMock) -> None:
    params = {"block_json": _NAV_BLOCK, "block": '{"block_type": "extraction", "label": "y"}'}

    result = await _validate_block_pre_hook(params, ctx)

    assert result is None
    assert params == {"block_json": _NAV_BLOCK, "block": '{"block_type": "extraction", "label": "y"}'}


@pytest.mark.asyncio
async def test_validate_block_pre_hook_normalizes_alias_before_code_only_gate(code_only_ctx: MagicMock) -> None:
    """Normalization must run before the code-only policy gate so a `code` block
    passed under the wrong key is still rejected (not crashed)."""
    params = {"block": '{"block_type": "code", "label": "x", "code": "pass"}'}

    result = await _validate_block_pre_hook(params, code_only_ctx)

    assert result is not None
    assert result["ok"] is False
    assert "validate real code blocks through update_and_run_blocks" in result["error"]
    assert params == {"block": '{"block_type": "code", "label": "x", "code": "pass"}'}


@pytest.mark.asyncio
async def test_validate_block_pre_hook_alias_allows_helper_under_code_only(code_only_ctx: MagicMock) -> None:
    params = {"block": '{"block_type": "conditional", "label": "x"}'}

    result = await _validate_block_pre_hook(params, code_only_ctx)

    assert result is None
    assert params == {"block": '{"block_type": "conditional", "label": "x"}'}


# ---------- Post-emission: YAML-level detector ----------


def test_raw_yaml_proxy_location_reports_absent_value() -> None:
    assert _raw_yaml_proxy_location(_yaml({"block_type": "navigation", "label": "n"})) == (False, None)


def test_raw_yaml_proxy_location_reports_explicit_values() -> None:
    assert _raw_yaml_proxy_location("title: wf\nproxy_location: US\n") == (True, "US")
    assert _raw_yaml_proxy_location("title: wf\nproxy_location: null\n") == (True, None)


def test_proxy_location_trace_value_serializes_enum_values() -> None:
    assert _proxy_location_trace_value(ProxyLocation.RESIDENTIAL) == "RESIDENTIAL"


# ---------- Flat shapes ----------


def test_top_level_task_block_is_detected_on_first_authoring() -> None:
    submitted = _yaml({"block_type": "task", "label": "fill_contact_form", "navigation_goal": "do thing"})
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert result == [("fill_contact_form", "task")]


def test_top_level_task_v2_block_is_detected() -> None:
    submitted = _yaml({"block_type": "task_v2", "label": "legacy_taskv2", "prompt": "do it"})
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert result == [("legacy_taskv2", "task_v2")]


def test_case_and_whitespace_insensitive() -> None:
    submitted = _yaml(
        {"block_type": "TASK", "label": "a"},
        {"block_type": " task_v2 ", "label": "b"},
        {"block_type": "Task", "label": "c"},
    )
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert sorted(result) == [("a", "task"), ("b", "task_v2"), ("c", "task")]


def test_mixed_task_and_navigation_only_reports_banned() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "nav_a", "navigation_goal": "ok"},
        {"block_type": "task", "label": "bad", "navigation_goal": "bad"},
        {"block_type": "extraction", "label": "ext_a"},
    )
    result = _detect_new_banned_blocks(submitted, prior_workflow_yaml=None)
    assert result == [("bad", "task")]


def test_only_allowed_types_returns_empty() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "n"},
        {"block_type": "extraction", "label": "e"},
        {"block_type": "validation", "label": "v"},
        {"block_type": "login", "label": "lg"},
        {"block_type": "goto_url", "label": "g"},
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == []


# ---------- Malformed ----------


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("title: 'unterminated", id="unterminated-yaml"),
        pytest.param("title: wf\n", id="missing-workflow-definition"),
        pytest.param("title: wf\nworkflow_definition:\n  blocks: not-a-list\n", id="blocks-not-a-list"),
    ],
)
def test_malformed_input_is_graceful_no_op(malformed: str) -> None:
    assert _detect_new_banned_blocks(malformed, prior_workflow_yaml=None) == []


def test_block_entry_not_a_dict_is_skipped() -> None:
    # A bare string where a block dict is expected — should be skipped, not crash.
    weird = textwrap.dedent(
        """\
        title: wf
        workflow_definition:
          blocks:
            - "not a block"
            - block_type: task
              label: real_banned
        """
    )
    assert _detect_new_banned_blocks(weird, prior_workflow_yaml=None) == [("real_banned", "task")]


# ---------- Legacy preservation (RISK-1) ----------


def test_preserved_legacy_task_block_under_same_label_does_not_reject() -> None:
    prior = _yaml({"block_type": "task", "label": "legacy_task", "navigation_goal": "old"})
    submitted = _yaml({"block_type": "task", "label": "legacy_task", "navigation_goal": "old edited"})
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == []


def test_new_task_block_alongside_preserved_legacy_reports_only_the_new_one() -> None:
    prior = _yaml({"block_type": "task", "label": "legacy_task"})
    submitted = _yaml(
        {"block_type": "task", "label": "legacy_task"},
        {"block_type": "task", "label": "fill_contact_form"},
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("fill_contact_form", "task")]


def test_renamed_legacy_task_block_is_treated_as_new() -> None:
    """Edge case: copilot re-emits a legacy task block under a different label.
    The detector has no way to know this is a rename, so it's reported as new.
    Acceptable: the copilot can recover by re-using the prior label."""
    prior = _yaml({"block_type": "task", "label": "old_name"})
    submitted = _yaml({"block_type": "task", "label": "new_name"})
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("new_name", "task")]


def test_prior_contains_allowed_types_submitted_adds_task_rejects() -> None:
    prior = _yaml({"block_type": "navigation", "label": "nav"})
    submitted = _yaml(
        {"block_type": "navigation", "label": "nav"},
        {"block_type": "task", "label": "bad_new"},
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("bad_new", "task")]


def test_legacy_task_v2_preservation() -> None:
    prior = _yaml({"block_type": "task_v2", "label": "legacy_v2"})
    submitted = _yaml({"block_type": "task_v2", "label": "legacy_v2"})
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == []


# ---------- Nested (COMP-1) ----------


def test_task_block_inside_for_loop_is_detected() -> None:
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [
                {"block_type": "navigation", "label": "inner_nav"},
                {"block_type": "task", "label": "inner_bad"},
            ],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == [("inner_bad", "task")]


def test_nested_preservation_does_not_reject() -> None:
    prior = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [{"block_type": "task", "label": "nested_legacy"}],
        }
    )
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [{"block_type": "task", "label": "nested_legacy"}],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == []


def test_nested_new_addition_is_detected() -> None:
    prior = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [{"block_type": "navigation", "label": "nav_inner"}],
        }
    )
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "loop",
            "loop_blocks": [
                {"block_type": "navigation", "label": "nav_inner"},
                {"block_type": "task", "label": "new_nested_bad"},
            ],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=prior) == [("new_nested_bad", "task")]


def test_deeply_nested_for_loop_is_walked() -> None:
    """for_loop nested inside another for_loop — recursion must reach the innermost level."""
    submitted = _yaml(
        {
            "block_type": "for_loop",
            "label": "outer",
            "loop_blocks": [
                {
                    "block_type": "for_loop",
                    "label": "inner",
                    "loop_blocks": [{"block_type": "task", "label": "deeply_nested_bad"}],
                }
            ],
        }
    )
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == [("deeply_nested_bad", "task")]


# ---------- Missing label — should not crash ----------


def test_block_without_label_is_skipped() -> None:
    """A banned block missing the ``label`` key can't be identified for
    preservation matching; skip it rather than crash. The YAML validator
    downstream will surface the missing-label error on its own."""
    submitted = _yaml({"block_type": "task", "navigation_goal": "no label"})
    # No label → not collectible; result is empty (downstream Pydantic reject
    # will surface the malformed block).
    assert _detect_new_banned_blocks(submitted, prior_workflow_yaml=None) == []


# ---------- Integration-shape tests: _update_workflow end-to-end ----------
#
# These exercise the reject path at the tool-helper boundary, confirming the
# detection + error tool-result shape + dedicated OTEL span. The success path
# (YAML with only allowed types, or with preserved legacy task labels) is also
# covered — we patch ``_process_workflow_yaml`` and the workflow-service write
# so the test does not need a DB.


@pytest.mark.asyncio
async def test_update_workflow_rejects_new_task_block_and_emits_span() -> None:
    submitted = _yaml({"block_type": "task", "label": "fill_contact_form", "navigation_goal": "do"})
    ctx = _ctx(prior_yaml=None)

    with patch("skyvern.forge.sdk.copilot.tools.workflow_update._record_banned_block_reject_span") as mock_span:
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
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
        {"block_type": "navigation", "label": "new_nav", "navigation_goal": "new"},
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
        {"block_type": "navigation", "label": "n", "navigation_goal": "x"},
        {"block_type": "validation", "label": "v", "complete_criterion": "c"},
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


def test_code_only_reject_message_groups_per_type_capability_text() -> None:
    ctx = _code_only_ctx()
    message = _banned_block_reject_message(
        [("login_step", "login"), ("download_step", "file_download"), ("open_step", "navigation")],
        ctx,
    )

    assert "not available in the workflow copilot" in message
    assert "login_step" in message
    assert "download_step" in message
    assert "open_step" in message
    assert "credential-typed code" in message
    assert "download registration" in message
    assert "focused `code` blocks" in message


@pytest.mark.asyncio
async def test_code_only_update_workflow_rejects_new_browser_block_with_policy_text() -> None:
    submitted = _yaml({"block_type": "login", "label": "login_step"})
    ctx = _code_only_ctx(prior_yaml=None)

    with patch("skyvern.forge.sdk.copilot.tools.workflow_update._record_banned_block_reject_span") as mock_span:
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is False
    assert "not available in the workflow copilot" in result["error"]
    assert "credential-typed code" in result["error"]
    mock_span.assert_called_once_with("_update_workflow", [("login_step", "login")])


# ---------- Cross-layer sync guard ----------


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
