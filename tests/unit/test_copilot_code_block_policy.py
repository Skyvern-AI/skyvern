"""Tests for the opt-in Workflow Copilot code-only browser authoring policy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from skyvern.config import settings
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.build_phase import BuildPhase, _phase_blocker_signal
from skyvern.forge.sdk.copilot.code_block_preflight import preflight_code_block
from skyvern.forge.sdk.copilot.config import (
    BlockAuthoringPolicy,
    CopilotConfig,
    block_authoring_policy_from_code_only_mode,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import _hide_code_only_tool_after_target_evidence
from skyvern.forge.sdk.copilot.tools import (
    _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES,
    _click_pre_hook,
    _code_block_security_validation_error,
    _code_only_pre_run_results_error,
    _detect_new_banned_blocks,
    _evaluate_post_hook,
    _get_block_schema_post_hook,
    _get_block_schema_pre_hook,
    _inspect_page_for_composition_impl,
    _navigate_post_hook,
    _normalize_code_only_workflow_yaml_envelope,
    _update_workflow,
    _validate_block_pre_hook,
    _workflow_yaml_conflict_marker_error,
    _workflow_yaml_truncation_placeholder_error,
)


def _ctx(policy: BlockAuthoringPolicy = BlockAuthoringPolicy.CODE_ONLY_BROWSER) -> MagicMock:
    ctx = MagicMock()
    ctx.block_authoring_policy = policy
    ctx.workflow_yaml = None
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    ctx.workflow_persisted = False
    ctx.update_workflow_called = False
    ctx.code_only_initial_browser_exploration_count = 0
    ctx.code_only_suspicious_success_browser_repair_count = 0
    ctx.code_only_suspicious_success_update_repair_count = 0
    ctx.code_only_code_schema_seen = False
    ctx.code_only_target_page_evidence_seen = False
    ctx.pending_reconciliation_run_id = None
    ctx.last_run_blocks_workflow_run_id = None
    ctx.last_successful_run_blocks_workflow_run_id = None
    ctx.observed_browser_urls = []
    return ctx


def _copilot_ctx(policy: BlockAuthoringPolicy = BlockAuthoringPolicy.CODE_ONLY_BROWSER) -> CopilotContext:
    return CopilotContext(
        organization_id="o_test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
        block_authoring_policy=policy,
    )


def _yaml(*blocks: dict) -> str:
    return yaml.safe_dump(
        {"title": "wf", "workflow_definition": {"blocks": list(blocks)}},
        sort_keys=False,
    )


def _fake_workflow() -> MagicMock:
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
    return fake_workflow


def test_copilot_config_defaults_to_standard_policy() -> None:
    assert CopilotConfig().block_authoring_policy == BlockAuthoringPolicy.STANDARD


def test_code_only_settings_helper_selects_policy() -> None:
    assert block_authoring_policy_from_code_only_mode(True) == BlockAuthoringPolicy.CODE_ONLY_BROWSER
    assert block_authoring_policy_from_code_only_mode(False) == BlockAuthoringPolicy.STANDARD


def test_base_agent_function_honors_code_only_browser_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)

    config = AgentFunction().get_copilot_config()

    assert config is not None
    assert config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER


def test_base_agent_function_request_config_uses_env_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run() -> None:
        monkeypatch.setattr(settings, "WORKFLOW_COPILOT_CODE_BLOCK_MODE", True)

        config = await AgentFunction().get_copilot_config_for_request("o_test")

        assert config is not None
        assert config.block_authoring_policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER

    asyncio.run(_run())


def test_code_only_policy_prompt_reaches_agent_system_prompt() -> None:
    prompt = agent_module._build_system_prompt(
        tool_usage_guide="",
        config=CopilotConfig(block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER),
    )
    compact_prompt = " ".join(prompt.split())

    assert "ACTIVE BLOCK AUTHORING POLICY: CODE-ONLY BROWSER MODE" in prompt
    assert "supersedes any earlier workflow-block guidance" in prompt
    assert "Durable browser behavior MUST be authored as `code` blocks" in prompt
    assert "Use MCP browser tools only as build-time exploration evidence" in prompt
    assert "Do not collapse a whole browser workflow into one code block" in prompt
    assert "click, type, select, scroll, and press keys as needed" in compact_prompt
    assert "Do not use workflow/block lifecycle tools while exploring" in compact_prompt
    assert "`validate_block` is disabled in this mode" in compact_prompt
    assert "Once you know the target URL, necessary selectors, form values" in compact_prompt
    assert "valid Python identifiers are available as local variables" in compact_prompt
    assert "Do not call `update_and_run_blocks` with only" in compact_prompt
    assert "tested end-to-end immediately" in compact_prompt
    assert "skip single-block validation" in compact_prompt
    assert "validating only `open_*`" in compact_prompt
    assert "omits a requested downstream action" in compact_prompt
    assert "All local variables become the block output" in compact_prompt
    assert "Playwright locators" in compact_prompt
    assert "helper functions/classes" in compact_prompt
    assert "Never include placeholder text such as `[... truncated ...]`" in compact_prompt
    assert "direct `page.goto` SSL/cert failure" in compact_prompt
    assert "avoid AI-intent targeting for page mutations" in compact_prompt
    assert "Prefer direct stable URLs for runtime code" in compact_prompt
    assert "Preserve the form/control state" in compact_prompt
    assert "distinct controls for search mode and filters" in compact_prompt
    assert "pre-submit page" in compact_prompt
    assert "Do not spend workflow runtime clicking through a marketing/home page" in compact_prompt
    assert "use that final observed URL exactly" in compact_prompt
    assert "first `open_*` block must `page.goto`" in compact_prompt
    assert "do not author a homepage-navigation block" in compact_prompt
    assert "Avoid unbounded or default-timeout `networkidle` waits" in compact_prompt
    assert "no_wait_after=True" in compact_prompt
    assert "wait briefly for the control to become" in compact_prompt
    assert "locator" + ".evaluate" in compact_prompt  # nosemgrep: incomplete-url-substring-sanitization
    assert "Wait for visible actionable controls" in compact_prompt
    assert "Execution context" in compact_prompt
    assert "page.title()" in compact_prompt
    assert "Do not default to scanning every frame" in compact_prompt
    assert "Do not write a generic helper framework" in compact_prompt
    assert "Do not click global navigation" in compact_prompt
    assert "Do not write `import`" in compact_prompt
    assert "call them directly without importing modules" in compact_prompt
    assert "shape checks (`isinstance`)" in compact_prompt
    assert "match/search/findall/finditer/fullmatch/sub/compile/split/escape" in compact_prompt
    assert "intentionally hidden" in compact_prompt
    assert "non-browser helper blocks to bypass browser interaction" in compact_prompt
    assert "Split repeated" in compact_prompt
    assert "separate objects" in compact_prompt
    assert "After a workflow run satisfies" in compact_prompt
    assert "full ordered workflow label chain" in compact_prompt
    assert "locator.check()" in compact_prompt
    assert "el.checked = true" in compact_prompt


def test_code_only_normalizes_top_level_blocks_yaml_envelope() -> None:
    ctx = _ctx()
    workflow_yaml = _yaml({"block_type": "code", "label": "open_registry", "code": "opened = True"})
    loaded = yaml.safe_load(workflow_yaml)
    shorthand_yaml = yaml.safe_dump(
        {
            "title": loaded["title"],
            "parameters": [{"key": "person_name", "type": "string"}],
            "blocks": loaded["workflow_definition"]["blocks"],
        },
        sort_keys=False,
    )

    normalized = _normalize_code_only_workflow_yaml_envelope(ctx, shorthand_yaml)

    normalized_loaded = yaml.safe_load(normalized)
    assert "blocks" not in normalized_loaded
    assert normalized_loaded["workflow_definition"]["blocks"][0]["label"] == "open_registry"
    assert normalized_loaded["workflow_definition"]["parameters"][0]["key"] == "person_name"


def test_code_only_normalizes_misindented_envelope_keys() -> None:
    ctx = _ctx()
    workflow_yaml = "\n".join(
        [
            "title: Public Record Lookup",
            '  description: "Find and extract public record details."',
            "  workflow_definition:",
            "    blocks:",
            "      - block_type: code",
            "        label: open_registry",
            "        code: |",
            "          extracted = {'description': 'kept inside code literal'}",
            "",
        ]
    )

    normalized = _normalize_code_only_workflow_yaml_envelope(ctx, workflow_yaml)
    normalized_loaded = yaml.safe_load(normalized)

    assert normalized_loaded["description"] == "Find and extract public record details."
    assert normalized_loaded["workflow_definition"]["blocks"][0]["label"] == "open_registry"
    assert "kept inside code literal" in normalized_loaded["workflow_definition"]["blocks"][0]["code"]


def test_standard_policy_does_not_normalize_top_level_blocks_yaml_envelope() -> None:
    ctx = _ctx(BlockAuthoringPolicy.STANDARD)
    shorthand_yaml = yaml.safe_dump(
        {
            "title": "wf",
            "blocks": [{"block_type": "code", "label": "open_registry", "code": "opened = True"}],
        },
        sort_keys=False,
    )

    assert _normalize_code_only_workflow_yaml_envelope(ctx, shorthand_yaml) == shorthand_yaml


def test_code_only_schema_pre_hook_rejects_browser_blocks() -> None:
    async def _run() -> None:
        ctx = _ctx()
        for block_type in (
            "browser_task",
            "navigation",
            "action",
            "login",
            "extraction",
            "file_download",
            "file_upload",
            "goto_url",
            "validation",
            "print_page",
            "task",
            "task_v2",
        ):
            result = await _get_block_schema_pre_hook({"block_type": block_type}, ctx)

            assert result is not None
            assert result["ok"] is False
            assert "focused `code` blocks" in result["error"]

    asyncio.run(_run())


def test_standard_schema_pre_hook_still_allows_navigation() -> None:
    async def _run() -> None:
        ctx = _ctx(BlockAuthoringPolicy.STANDARD)

        assert await _get_block_schema_pre_hook({"block_type": "navigation"}, ctx) is None

    asyncio.run(_run())


def test_code_only_validate_block_pre_hook_rejects_dummy_probe_validation() -> None:
    async def _run() -> None:
        ctx = _ctx()

        result = await _validate_block_pre_hook({"block": {"block_type": "code", "label": "dummy"}}, ctx)

        assert result is not None
        assert result["ok"] is False
        assert "CODE-ONLY BLOCK VALIDATION DISABLED" in result["error"]
        assert "dummy" in result["error"]
        assert "update_and_run_blocks" in result["error"]

    asyncio.run(_run())


def test_code_only_get_run_results_is_unavailable_before_real_run() -> None:
    ctx = _ctx()

    result = _code_only_pre_run_results_error(ctx)

    assert result is not None
    assert result["ok"] is False
    assert "CODE-ONLY EXPLORATION PHASE" in result["error"]
    assert "before a real workflow run exists" in result["error"]
    assert "MCP browser tools" in result["error"]


def test_code_only_get_run_results_allows_existing_run_context() -> None:
    ctx = _ctx()
    ctx.last_run_blocks_workflow_run_id = "wr_existing"

    assert _code_only_pre_run_results_error(ctx) is None


def test_code_only_mcp_tool_list_hides_validate_block_but_keeps_browser_tools() -> None:
    ctx = _ctx()

    assert _hide_code_only_tool_after_target_evidence(ctx, "validate_block") is True
    assert _hide_code_only_tool_after_target_evidence(ctx, "click") is False
    assert _hide_code_only_tool_after_target_evidence(ctx, "type_text") is False
    assert _hide_code_only_tool_after_target_evidence(ctx, "evaluate") is False


def test_code_only_phase_gate_allows_browser_exploration_before_composition() -> None:
    ctx = _ctx()
    ctx.build_phase = BuildPhase.DISCOVERING

    assert _phase_blocker_signal(ctx, "navigate_browser") is None
    assert _phase_blocker_signal(ctx, "evaluate") is None


def test_standard_phase_gate_blocks_browser_exploration_before_composition() -> None:
    ctx = _ctx(BlockAuthoringPolicy.STANDARD)
    ctx.build_phase = BuildPhase.DISCOVERING

    result = _phase_blocker_signal(ctx, "navigate_browser")

    assert result is not None
    assert result.internal_reason_code == "build_phase_browser_blocked_pre_compose"


def test_code_only_click_pre_hook_allows_css_has_selector() -> None:
    async def _run() -> None:
        ctx = _ctx()

        result = await _click_pre_hook({"selector": "form:has(input[name='email']) button"}, ctx)

        assert result is None

    asyncio.run(_run())


def test_code_only_click_pre_hook_rejects_jquery_only_selector() -> None:
    async def _run() -> None:
        ctx = _ctx()

        result = await _click_pre_hook({"selector": "button:contains('Download')"}, ctx)

        assert result is not None
        assert result["ok"] is False
        assert "jQuery pseudo-selectors" in result["error"]

    asyncio.run(_run())


def test_code_only_successful_navigation_records_url_and_advances_to_composition() -> None:
    async def _run() -> None:
        ctx = _ctx()
        ctx.build_phase = BuildPhase.DISCOVERING

        result = await _navigate_post_hook({"ok": True, "data": {"url": "https://example.com/search"}}, {}, ctx)

        assert result["url"] == "https://example.com/search"
        assert ctx.observed_browser_urls == ["https://example.com/search"]
        assert ctx.build_phase == BuildPhase.COMPOSING

    asyncio.run(_run())


def test_code_schema_post_hook_adds_observed_url_guidance() -> None:
    async def _run() -> None:
        ctx = _ctx()
        ctx.observed_browser_urls = [
            "https://example.com/search",
            "https://example.com/search?name=alex",
        ]
        result = await _get_block_schema_post_hook(
            {"ok": True, "data": {"block_type": "code", "schema": {"properties": {"unused": {}}}}},
            {},
            ctx,
        )

        data = result["data"]
        assert ctx.code_only_code_schema_seen is True
        assert data["schema"]["required"] == ["block_type", "label", "code"]
        assert data["code_only_observed_urls"] == [
            "https://example.com/search",
            "https://example.com/search?name=alex",
        ]
        assert data["code_only_runtime_entrypoint_url_hint"] == "https://example.com/search"

    asyncio.run(_run())


def test_code_only_evaluate_post_hook_compacts_and_marks_target_evidence() -> None:
    async def _run() -> None:
        ctx = _copilot_ctx()
        long_body = "Result row " * 100

        with patch(
            "skyvern.forge.sdk.copilot.tools._maybe_run_completion_verification_from_page_observation",
            new=AsyncMock(),
        ):
            result = await _evaluate_post_hook(
                {
                    "ok": True,
                    "data": {
                        "result": {
                            "url": "https://example.com/search",
                            "title": "Search",
                            "inputs": [{"name": "q"}],
                            "body": long_body,
                        },
                        "sdk_equivalent": "omitted",
                    },
                },
                {},
                ctx,
            )

        data = result["data"]
        assert ctx.code_only_target_page_evidence_seen is True
        assert data["url"] == "https://example.com/search"
        assert "sdk_equivalent" not in data
        assert data["result"]["body"].endswith("[truncated 500 chars]")

    asyncio.run(_run())


def test_code_only_evaluate_post_hook_does_not_treat_homepage_inputs_as_target_evidence() -> None:
    async def _run() -> None:
        ctx = _copilot_ctx()

        with patch(
            "skyvern.forge.sdk.copilot.tools._maybe_run_completion_verification_from_page_observation",
            new=AsyncMock(),
        ):
            await _evaluate_post_hook(
                {
                    "ok": True,
                    "data": {
                        "url": "https://example.com/",
                        "inputs": [{"name": "q"}],
                    },
                },
                {},
                ctx,
            )

        assert ctx.code_only_target_page_evidence_seen is False

    asyncio.run(_run())


def test_code_only_blocks_composition_inspection_before_first_draft() -> None:
    async def _run() -> None:
        ctx = _copilot_ctx()

        result = await _inspect_page_for_composition_impl(ctx, "https://example.com/search")

        assert result["ok"] is False
        assert "CODE-ONLY MCP EXPLORATION ONLY" in result["error"]

    asyncio.run(_run())


def test_detect_new_banned_blocks_uses_code_only_set() -> None:
    submitted = _yaml(
        {"block_type": "navigation", "label": "open_site"},
        {"block_type": "browser_task", "label": "legacy_alias"},
        {"block_type": "code", "label": "search_with_code", "code": "result = 1"},
    )

    result = _detect_new_banned_blocks(
        submitted,
        prior_workflow_yaml=None,
        banned_types=_COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES,
    )

    assert sorted(result) == [
        ("legacy_alias", "navigation"),
        ("open_site", "navigation"),
    ]


def test_detect_new_banned_blocks_preserves_existing_banned_labels_in_code_only_mode() -> None:
    prior = _yaml({"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "old"})
    submitted = _yaml({"block_type": "navigation", "label": "legacy_nav", "navigation_goal": "edited"})

    assert (
        _detect_new_banned_blocks(
            submitted,
            prior_workflow_yaml=prior,
            banned_types=_COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES,
        )
        == []
    )


def test_update_workflow_rejects_new_navigation_in_code_only_mode() -> None:
    async def _run() -> None:
        submitted = _yaml({"block_type": "navigation", "label": "open_site", "navigation_goal": "open"})
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "open_site" in result["error"]
        assert "focused `code` blocks" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_truncated_yaml_placeholder() -> None:
    async def _run() -> None:
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": "title: wf\n[... truncated due length]"}, ctx)

        assert result["ok"] is False
        assert "truncation placeholder" in result["error"]
        assert "complete workflow YAML" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_conflict_marker_yaml() -> None:
    async def _run() -> None:
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": "title: wf\n<<<<<<< nope\nworkflow_definition: {}"}, ctx)

        assert result["ok"] is False
        assert "unresolved conflict markers" in result["error"]
        assert "complete, valid workflow YAML" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_unsafe_code_before_run() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "search_with_regex",
                "code": "import re\nmatch = re.search('x', 'x')",
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "search_with_regex" in result["error"]
        assert "sandbox-blocked Python" in result["error"]
        assert "Do not write import statements" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_invalid_python_syntax_before_run() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "extract_records",
                "code": "if True print('missing colon')",
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "extract_records" in result["error"]
        assert "invalid Python syntax" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_locals_helper_before_run() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "extract_records",
                "code": "output = dict(locals())",
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "extract_records" in result["error"]
        assert "locals()" in result["error"]
        assert "not available in the code-block sandbox" in result["error"]

    asyncio.run(_run())


def test_workflow_yaml_integrity_gates_are_scoped_to_code_only_policy() -> None:
    truncated = "title: wf\n[... truncated due length]"
    conflicted = "title: wf\n<<<<<<< nope\nworkflow_definition: {}"

    assert _workflow_yaml_truncation_placeholder_error(_ctx(), truncated) is not None
    assert _workflow_yaml_conflict_marker_error(_ctx(), conflicted) is not None
    assert _workflow_yaml_truncation_placeholder_error(_ctx(BlockAuthoringPolicy.STANDARD), truncated) is None
    assert _workflow_yaml_conflict_marker_error(_ctx(BlockAuthoringPolicy.STANDARD), conflicted) is None


def test_code_block_security_validation_is_gated_to_code_only_policy() -> None:
    locals_block = _yaml({"block_type": "code", "label": "extract_records", "code": "output = dict(locals())"})
    wizard_block = _yaml(
        {
            "block_type": "code",
            "label": "advance_intake",
            "code": "await page.locator('#intake button[data-next-step=\"2\"]').click(timeout=5000)\n",
        }
    )

    for submitted in (locals_block, wizard_block):
        assert _code_block_security_validation_error(_ctx(), submitted) is not None
        assert _code_block_security_validation_error(_ctx(BlockAuthoringPolicy.STANDARD), submitted) is None


def test_update_workflow_rejects_mixed_css_and_text_selector_group() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "search_registry",
                "code": (
                    "await page.locator('#registryTable, text=Showing, text=No matching records')"
                    ".first.wait_for(timeout=10000)\n"
                ),
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "search_registry" in result["error"]
        assert "Playwright text selectors" in result["error"]
        assert "#registryTable, text=Showing, text=No matching records" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_playwright_js_locator_property_call() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "search_product",
                "code": (
                    "search_button = page.locator('button[type=submit]').first()\nawait search_button.click(timeout=5000)\n"
                ),
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "search_product" in result["error"]
        assert "static preflight" in result["error"]
        assert "PLAYWRIGHT_API_MISMATCH" in result["error"]
        assert "Locator as a function" in result["error"]

    asyncio.run(_run())


def test_code_block_preflight_accepts_playwright_python_locator_property() -> None:
    diagnostics = preflight_code_block(
        "search_button = page.locator('button[type=submit]').first\nawait search_button.click(timeout=5000)\n"
    )

    assert diagnostics == []


def test_code_block_preflight_rejects_playwright_js_locator_property_call() -> None:
    diagnostics = preflight_code_block(
        "search_button = page.locator('button[type=submit]').first()\nawait search_button.click(timeout=5000)\n"
    )

    assert diagnostics
    assert diagnostics[0].code == "PLAYWRIGHT_API_MISMATCH"
    assert "Locator as a function" in diagnostics[0].message


def test_code_block_preflight_allows_transient_locator_cleanup_to_none() -> None:
    diagnostics = preflight_code_block(
        "search_button = page.locator('button[type=submit]').first\n"
        "await search_button.click(timeout=5000)\n"
        "search_button = None\n"
    )

    assert diagnostics == []


def test_code_block_preflight_rejects_page_evaluate_extra_positional_args() -> None:
    diagnostics = preflight_code_block("result = await page.evaluate('(args) => args.a + args.b', 1, 2)\n")

    assert diagnostics
    assert diagnostics[0].code == "PLAYWRIGHT_API_MISMATCH"
    assert "evaluate" in diagnostics[0].message
    assert "one serialized arg" in diagnostics[0].message


def test_code_block_preflight_rejects_invalid_regex_literal() -> None:
    diagnostics = preflight_code_block("match = re.search(r'Confirmation ID\\s+(*', body_text)\n")

    assert diagnostics
    assert diagnostics[0].code == "INVALID_REGEX_LITERAL"
    assert "invalid regex literal" in diagnostics[0].message


def test_code_block_preflight_rejects_raw_wizard_step_button_selector() -> None:
    diagnostics = preflight_code_block(
        "await page.locator('#intake button[data-next-step=\"2\"]').click(timeout=5000)\n"
    )

    assert diagnostics
    assert diagnostics[0].code == "AMBIGUOUS_WIZARD_STEP_SELECTOR"
    assert "wizard step button" in diagnostics[0].message


def test_update_workflow_rejects_page_evaluate_extra_positional_args() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "fill_intake",
                "code": "result = await page.evaluate('(args) => args.a + args.b', 1, 2)\n",
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "fill_intake" in result["error"]
        assert "PLAYWRIGHT_API_MISMATCH" in result["error"]
        assert "evaluate" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_invalid_regex_literal() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "extract_confirmation",
                "code": "match = re.search(r'Confirmation ID\\s+(*', body_text)\n",
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "extract_confirmation" in result["error"]
        assert "INVALID_REGEX_LITERAL" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_raw_wizard_step_button_selector() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "enter_quote_details",
                "code": "await page.locator('#intake button[data-next-step=\"2\"]').click(timeout=5000)\n",
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "enter_quote_details" in result["error"]
        assert "AMBIGUOUS_WIZARD_STEP_SELECTOR" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_prefix_url_navigation_guard_in_code_only_mode() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "open_registry",
                "code": (
                    "target_url = 'https://example.com/search'\n"
                    "if not page.url.startswith(target_url):\n"
                    "    await page.goto(target_url)\n"
                ),
            }
        )
        ctx = _ctx()

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "open_registry" in result["error"]
        assert "page.url" + ".startswith" in result["error"]  # nosemgrep: incomplete-url-substring-sanitization
        assert "stale browser state" in result["error"]

    asyncio.run(_run())


def test_update_workflow_rejects_homepage_runtime_start_when_entrypoint_hint_known() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "open_public_record_search",
                "code": (
                    "target_url = 'https://example.com/'\n"
                    "await page.goto(target_url, wait_until='domcontentloaded', timeout=20000)\n"
                ),
            },
            {
                "block_type": "code",
                "label": "search_registry",
                "code": "searched = True",
            },
        )
        ctx = _ctx()
        ctx.observed_browser_urls = [
            "https://example.com/",
            "https://example.com/registry/search",
            "https://example.com/registry/search?submitted=1",
        ]

        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is False
        assert "open_public_record_search" in result["error"]
        assert "durable form/search entrypoint" in result["error"]
        assert "https://example.com/registry/search" in result["error"]

    asyncio.run(_run())


def test_update_workflow_allows_code_blocks_in_code_only_mode() -> None:
    async def _run() -> None:
        submitted = _yaml(
            {
                "block_type": "code",
                "label": "open_site_with_code",
                "code": "await page.goto(target_url)\nstatus = 'opened'",
                "parameter_keys": ["target_url"],
            }
        )
        ctx = _ctx()

        with (
            patch("skyvern.forge.sdk.copilot.tools._process_workflow_yaml", return_value=_fake_workflow()),
            patch("skyvern.forge.sdk.copilot.tools.app") as mock_app,
        ):
            mock_app.WORKFLOW_SERVICE.get_workflow = AsyncMock(return_value=None)
            mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
            result = await _update_workflow({"workflow_yaml": submitted}, ctx)

        assert result["ok"] is True

    asyncio.run(_run())
